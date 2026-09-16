"""Chunk-plan arithmetic and AEAD chunk sealing."""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from app.core import crypto
from app.services.upload import ChunkPlan


class TestChunkPlan:
    def test_exact_multiple_divides_evenly(self):
        plan = ChunkPlan(size_bytes=4 * 1024 * 1024, chunk_size=1024 * 1024)
        assert plan.total_chunks == 4
        assert [plan.size_of(i) for i in range(4)] == [1024 * 1024] * 4

    def test_remainder_goes_to_last_chunk(self):
        plan = ChunkPlan(size_bytes=3 * 1024 * 1024 + 7, chunk_size=1024 * 1024)
        assert plan.total_chunks == 4
        assert plan.size_of(3) == 7
        assert sum(plan.size_of(i) for i in range(4)) == 3 * 1024 * 1024 + 7

    def test_zero_byte_file_gets_one_chunk(self):
        """A zero-byte file must still have a chunk, preserving the
        one-node-at-least-one-message invariant so nothing special-cases empty."""
        plan = ChunkPlan(size_bytes=0, chunk_size=1024 * 1024)
        assert plan.total_chunks == 1
        assert plan.size_of(0) == 0

    def test_single_byte_file(self):
        plan = ChunkPlan(size_bytes=1, chunk_size=1024 * 1024)
        assert plan.total_chunks == 1
        assert plan.size_of(0) == 1

    def test_one_byte_over_boundary(self):
        plan = ChunkPlan(size_bytes=1024 * 1024 + 1, chunk_size=1024 * 1024)
        assert plan.total_chunks == 2
        assert plan.size_of(0) == 1024 * 1024
        assert plan.size_of(1) == 1

    def test_offsets_are_contiguous(self):
        plan = ChunkPlan(size_bytes=5 * 1024 * 1024 + 123, chunk_size=1024 * 1024)
        for i in range(plan.total_chunks):
            assert plan.offset_of(i) == i * plan.chunk_size

    def test_out_of_range_index_raises(self):
        plan = ChunkPlan(size_bytes=100, chunk_size=1024 * 1024)
        with pytest.raises(IndexError):
            plan.size_of(1)
        with pytest.raises(IndexError):
            plan.size_of(-1)

    @pytest.mark.parametrize(
        "size,chunk",
        [
            (0, 1024 * 1024),
            (1, 1024 * 1024),
            (1024 * 1024, 1024 * 1024),
            (1024 * 1024 + 1, 1024 * 1024),
            (64 * 1024 * 1024 + 999999, 64 * 1024 * 1024),
        ],
    )
    def test_plan_sums_to_file_size(self, size, chunk):
        plan = ChunkPlan(size_bytes=size, chunk_size=chunk)
        plan.validate()
        assert sum(plan.size_of(i) for i in range(plan.total_chunks)) == size


class TestChunkAEAD:
    def setup_method(self):
        self.key = os.urandom(32)
        self.node_id = uuid.uuid4().bytes
        self.aad = crypto.build_chunk_aad(self.node_id, 2, 10)

    def test_roundtrip(self):
        plaintext = os.urandom(4096)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        assert crypto.open_chunk(
            self.key, sealed.ciphertext, iv=sealed.iv, tag=sealed.tag, aad=self.aad
        ) == plaintext

    def test_stored_blob_is_exactly_tag_longer(self):
        plaintext = os.urandom(1000)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        assert sealed.wire_size == len(plaintext) + 16

    def test_ciphertext_does_not_contain_plaintext(self):
        plaintext = b"A" * 4096
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        assert plaintext not in sealed.wire

    def test_tampered_ciphertext_rejected(self):
        plaintext = os.urandom(512)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        corrupted = bytearray(sealed.ciphertext)
        corrupted[10] ^= 0x01
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.open_chunk(
                self.key, bytes(corrupted), iv=sealed.iv, tag=sealed.tag, aad=self.aad
            )

    def test_tampered_tag_rejected(self):
        plaintext = os.urandom(512)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        bad_tag = bytearray(sealed.tag)
        bad_tag[0] ^= 0xFF
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.open_chunk(
                self.key, sealed.ciphertext, iv=sealed.iv, tag=bytes(bad_tag), aad=self.aad
            )

    def test_wrong_chunk_index_rejected(self):
        """The core anti-reordering guarantee: a valid chunk presented at the
        wrong position must fail, not decrypt into plausible garbage."""
        plaintext = os.urandom(512)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        wrong_aad = crypto.build_chunk_aad(self.node_id, 5, 10)
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.open_chunk(
                self.key, sealed.ciphertext, iv=sealed.iv, tag=sealed.tag, aad=wrong_aad
            )

    def test_wrong_total_chunks_rejected(self):
        """Guards against a truncated file being accepted: the declared chunk
        count is authenticated, so dropping trailing chunks is detectable."""
        plaintext = os.urandom(512)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        wrong_aad = crypto.build_chunk_aad(self.node_id, 2, 9)
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.open_chunk(
                self.key, sealed.ciphertext, iv=sealed.iv, tag=sealed.tag, aad=wrong_aad
            )

    def test_wrong_node_id_rejected(self):
        """Cross-file substitution: a chunk from another file must not decrypt."""
        plaintext = os.urandom(512)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        other_file_aad = crypto.build_chunk_aad(uuid.uuid4().bytes, 2, 10)
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.open_chunk(
                self.key, sealed.ciphertext, iv=sealed.iv, tag=sealed.tag, aad=other_file_aad
            )

    def test_wrong_key_rejected(self):
        plaintext = os.urandom(512)
        sealed = crypto.seal_chunk(self.key, plaintext, aad=self.aad)
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.open_chunk(
                os.urandom(32), sealed.ciphertext, iv=sealed.iv, tag=sealed.tag, aad=self.aad
            )

    def test_ivs_are_unique_across_many_chunks(self):
        """IV reuse under one key is catastrophic for GCM, so uniqueness is
        asserted rather than assumed."""
        ivs = {
            crypto.seal_chunk(self.key, b"x" * 64, aad=self.aad).iv for _ in range(2000)
        }
        assert len(ivs) == 2000

    def test_iv_length_enforced(self):
        with pytest.raises(crypto.CryptoError):
            crypto.seal_chunk(self.key, b"data", aad=self.aad, iv=b"short")

    def test_empty_chunk_roundtrips(self):
        """The zero-byte-file case must still seal and authenticate."""
        sealed = crypto.seal_chunk(self.key, b"", aad=self.aad)
        assert sealed.wire_size == 16
        assert crypto.open_chunk(
            self.key, sealed.ciphertext, iv=sealed.iv, tag=sealed.tag, aad=self.aad
        ) == b""


class TestKeyHierarchy:
    def test_dek_wrap_roundtrip(self):
        kek = os.urandom(32)
        dek = crypto.generate_dek()
        uid = uuid.uuid4().bytes
        wrapped = crypto.wrap_dek(dek, kek, user_id_bytes=uid)
        assert crypto.unwrap_dek(wrapped, kek, user_id_bytes=uid) == dek

    def test_wrapped_dek_is_bound_to_user(self):
        """A wrapped key must not be transplantable between users by editing a
        row; the user id is authenticated as associated data."""
        kek = os.urandom(32)
        dek = crypto.generate_dek()
        wrapped = crypto.wrap_dek(dek, kek, user_id_bytes=uuid.uuid4().bytes)
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.unwrap_dek(wrapped, kek, user_id_bytes=uuid.uuid4().bytes)

    def test_file_keys_differ_per_file(self):
        dek = crypto.generate_dek()
        a = crypto.derive_file_key(dek, node_id_bytes=uuid.uuid4().bytes)
        b = crypto.derive_file_key(dek, node_id_bytes=uuid.uuid4().bytes)
        assert a != b
        assert len(a) == 32

    def test_file_key_is_deterministic(self):
        dek = crypto.generate_dek()
        nid = uuid.uuid4().bytes
        assert crypto.derive_file_key(dek, node_id_bytes=nid) == crypto.derive_file_key(
            dek, node_id_bytes=nid
        )

    def test_thumbnail_key_domain_separated_from_file_key(self):
        """Previews must not share a key with file data."""
        dek = crypto.generate_dek()
        nid = uuid.uuid4().bytes
        assert crypto.derive_thumbnail_key(dek, node_id_bytes=nid) != crypto.derive_file_key(
            dek, node_id_bytes=nid
        )


class TestSessionProtection:
    def test_session_roundtrip(self):
        kek = os.urandom(32)
        session = "1BVtsOK4" + "Z" * 300
        blob = crypto.encrypt_session_string(session, kek, label="pool-1")
        assert crypto.decrypt_session_string(blob, kek, label="pool-1") == session

    def test_session_bound_to_label(self):
        """Prevents a session blob being swapped between pool rows."""
        kek = os.urandom(32)
        blob = crypto.encrypt_session_string("session-data", kek, label="pool-1")
        with pytest.raises(crypto.AuthenticationFailed):
            crypto.decrypt_session_string(blob, kek, label="pool-2")

    def test_session_not_readable_in_plaintext(self):
        kek = os.urandom(32)
        session = "SUPERSECRETSESSIONSTRING"
        blob = crypto.encrypt_session_string(session, kek, label="p")
        assert session.encode() not in blob


class TestPasswordHashing:
    def test_verify_correct_password(self):
        h = crypto.hash_password("correct horse battery staple")
        assert crypto.verify_password("correct horse battery staple", h)

    def test_reject_wrong_password(self):
        h = crypto.hash_password("right")
        assert not crypto.verify_password("wrong", h)

    def test_salt_makes_hashes_unique(self):
        assert crypto.hash_password("same") != crypto.hash_password("same")

    def test_malformed_hash_does_not_crash(self):
        assert not crypto.verify_password("x", "not-a-real-hash")
        assert not crypto.verify_password("x", "")

    def test_unicode_password(self):
        pw = "رمز عبور قوی 🔐"
        assert crypto.verify_password(pw, crypto.hash_password(pw))


class TestContentDigests:
    def test_chunk_hasher_matches_hashlib(self):
        data = os.urandom(100_000)
        hasher = crypto.ChunkHasher()
        for i in range(0, len(data), 8192):
            hasher.update(data[i : i + 8192])
        assert hasher.hexdigest() == hashlib.sha256(data).hexdigest()
        assert hasher.total_bytes == len(data)

    def test_keyed_hmac_differs_from_plain_hash(self):
        data = b"predictable content"
        assert crypto.keyed_content_hmac(b"k" * 32, data) != crypto.sha256_bytes(data)

    def test_constant_time_equals(self):
        assert crypto.constant_time_equals("abc", "abc")
        assert not crypto.constant_time_equals("abc", "abd")

    def test_split_wire(self):
        wire = os.urandom(64)
        ct, tag = crypto.split_wire(wire)
        assert len(tag) == 16
        assert ct + tag == wire

    def test_split_wire_rejects_short_input(self):
        with pytest.raises(crypto.CryptoError):
            crypto.split_wire(b"tooshort")