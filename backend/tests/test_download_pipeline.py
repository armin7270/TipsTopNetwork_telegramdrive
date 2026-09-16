"""Download pipeline: byte-exact reassembly, Range arithmetic, failover."""

from __future__ import annotations

import hashlib
import os

import pytest

from app.core.errors import ProblemError
from app.services.download import ByteRange, plan_chunk_window, parse_range_header
from tests.conftest import collect, upload_bytes

MB = 1024 * 1024


class TestParseRangeHeader:
    def test_no_header_means_full_body(self):
        assert parse_range_header(None, 1000) is None
        assert parse_range_header("", 1000) is None

    def test_explicit_range(self):
        r = parse_range_header("bytes=0-99", 1000)
        assert (r.start, r.end, r.length) == (0, 99, 100)

    def test_open_ended_range(self):
        """What a resuming downloader or media player sends."""
        r = parse_range_header("bytes=500-", 1000)
        assert (r.start, r.end) == (500, 999)

    def test_suffix_range(self):
        """Used to read a file's tail without knowing its size."""
        r = parse_range_header("bytes=-100", 1000)
        assert (r.start, r.end) == (900, 999)

    def test_suffix_larger_than_file_clamps_to_whole_file(self):
        r = parse_range_header("bytes=-5000", 1000)
        assert (r.start, r.end) == (0, 999)

    def test_end_beyond_eof_is_clamped(self):
        """RFC 9110 permits clamping; clients commonly over-request the tail."""
        r = parse_range_header("bytes=900-100000", 1000)
        assert (r.start, r.end) == (900, 999)

    def test_single_byte_range(self):
        r = parse_range_header("bytes=0-0", 1000)
        assert r.length == 1

    def test_start_beyond_eof_is_unsatisfiable(self):
        with pytest.raises(ProblemError) as exc:
            parse_range_header("bytes=5000-6000", 1000)
        assert exc.value.status_code == 416
        assert exc.value.headers["Content-Range"] == "bytes */1000"

    def test_start_equal_to_size_is_unsatisfiable(self):
        with pytest.raises(ProblemError) as exc:
            parse_range_header("bytes=1000-", 1000)
        assert exc.value.status_code == 416

    def test_reversed_range_is_unsatisfiable(self):
        with pytest.raises(ProblemError):
            parse_range_header("bytes=500-100", 1000)

    def test_multipart_range_is_rejected_explicitly(self):
        """Multi-range needs multipart/byteranges. Rejecting clearly beats
        silently serving only the first range."""
        with pytest.raises(ProblemError) as exc:
            parse_range_header("bytes=0-99,200-299", 1000)
        assert exc.value.status_code == 416

    def test_non_bytes_unit_ignored(self):
        assert parse_range_header("items=0-10", 1000) is None

    def test_malformed_range_is_unsatisfiable(self):
        for bad in ["bytes=abc-def", "bytes=", "bytes=-", "bytes=10-abc"]:
            with pytest.raises(ProblemError):
                parse_range_header(bad, 1000)

    def test_range_on_empty_file_is_unsatisfiable(self):
        with pytest.raises(ProblemError):
            parse_range_header("bytes=0-10", 0)


class TestPlanChunkWindow:
    def test_range_inside_single_chunk(self):
        w = plan_chunk_window(ByteRange(100, 199), 1000)
        assert (w.first_index, w.last_index) == (0, 0)
        assert w.skip_in_first == 100
        assert w.take_from_last == 200

    def test_range_spanning_chunks(self):
        w = plan_chunk_window(ByteRange(500, 2500), 1000)
        assert (w.first_index, w.last_index) == (0, 2)
        assert w.count == 3
        assert w.skip_in_first == 500
        assert w.take_from_last == 501

    def test_range_aligned_to_chunk_boundary(self):
        w = plan_chunk_window(ByteRange(1000, 1999), 1000)
        assert (w.first_index, w.last_index) == (1, 1)
        assert w.skip_in_first == 0
        assert w.take_from_last == 1000

    def test_single_byte_at_chunk_start(self):
        w = plan_chunk_window(ByteRange(2000, 2000), 1000)
        assert (w.first_index, w.last_index) == (2, 2)
        assert w.skip_in_first == 0
        assert w.take_from_last == 1

    def test_whole_file(self):
        w = plan_chunk_window(ByteRange(0, 3999), 1000)
        assert (w.first_index, w.last_index) == (0, 3)
        assert w.count == 4


class TestStreamingRoundTrip:
    """Byte-exactness is the property that matters most: a cloud drive that
    returns subtly wrong bytes is worse than one that fails loudly."""

    @pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 65536])
    async def test_small_file_roundtrip(
        self, upload_service, download_service, seeded, chunk_size, size
    ):
        user = seeded["user"]["id"]
        data = os.urandom(size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name=f"rt-{size}.bin", data=data, chunk_size=chunk_size,
        )
        out = await collect(
            download_service.full_stream(node_id=node["id"], owner_id=user)
        )
        assert out == data
        assert hashlib.sha256(out).hexdigest() == hashlib.sha256(data).hexdigest()

    async def test_multi_chunk_roundtrip(
        self, upload_service, download_service, seeded, chunk_size
    ):
        user = seeded["user"]["id"]
        data = os.urandom(5 * chunk_size + 12345)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="big.bin", data=data, chunk_size=chunk_size,
        )
        out = await collect(
            download_service.full_stream(node_id=node["id"], owner_id=user)
        )
        assert out == data

    async def test_high_entropy_data_not_corrupted(
        self, upload_service, download_service, seeded, chunk_size
    ):
        """Encrypted-looking data is the realistic case and catches any
        accidental text-mode or delimiter-based handling."""
        user = seeded["user"]["id"]
        data = os.urandom(3 * chunk_size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="entropy.bin", data=data, chunk_size=chunk_size,
        )
        out = await collect(
            download_service.full_stream(node_id=node["id"], owner_id=user)
        )
        assert out == data


class TestRangeStreaming:
    """Range requests are what make video seeking and resumable downloads work.
    Each case verifies the returned slice is byte-exact, not merely the right
    length."""

    @pytest.fixture
    async def big_file(self, upload_service, seeded, chunk_size):
        user = seeded["user"]["id"]
        data = os.urandom(4 * chunk_size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="ranged.bin", data=data, chunk_size=chunk_size,
        )
        return user, node, data

    @pytest.mark.parametrize(
        "start,end",
        [
            (0, 0),
            (0, 100),
            (4095, 4097),                 # straddles the 4 KiB MTProto alignment
            (8191, 8193),                 # straddles the 8 KiB boundary
            (0, 4095),
            (4096, 8191),
            (0, 1024 * 1024 - 1),         # exactly one chunk
            (1024 * 1024, 2 * 1024 * 1024 - 1),   # second chunk exactly
            (1024 * 1024 - 10, 1024 * 1024 + 10),  # straddles a chunk boundary
            (500000, 2500000),            # spans three chunks
            (0, 4 * 1024 * 1024 - 1),     # whole file
        ],
    )
    async def test_range_is_byte_exact(
        self, download_service, big_file, start, end
    ):
        user, node, data = big_file
        out = await collect(
            download_service.stream_range(
                node_id=node["id"], owner_id=user,
                byte_range=ByteRange(start, end),
            )
        )
        assert out == data[start : end + 1]

    async def test_suffix_range_is_byte_exact(self, download_service, big_file, chunk_size):
        user, node, data = big_file
        r = parse_range_header("bytes=-1000", len(data))
        out = await collect(
            download_service.stream_range(node_id=node["id"], owner_id=user, byte_range=r)
        )
        assert out == data[-1000:]

    async def test_open_ended_range(self, download_service, big_file):
        user, node, data = big_file
        r = parse_range_header("bytes=1000000-", len(data))
        out = await collect(
            download_service.stream_range(node_id=node["id"], owner_id=user, byte_range=r)
        )
        assert out == data[1000000:]

    async def test_range_length_matches_content_range(
        self, download_service, big_file
    ):
        user, node, data = big_file
        r = ByteRange(123, 4567)
        out = await collect(
            download_service.stream_range(node_id=node["id"], owner_id=user, byte_range=r)
        )
        assert len(out) == r.length
        assert r.content_range_header(len(data)) == f"bytes 123-4567/{len(data)}"


class TestETag:
    async def test_etag_is_quoted_hex_sha256(
        self, upload_service, download_service, seeded, chunk_size
    ):
        user = seeded["user"]["id"]
        data = os.urandom(10_000)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="etag.bin", data=data, chunk_size=chunk_size,
        )
        etag = download_service.etag_for(node)
        assert etag == f'"{hashlib.sha256(data).hexdigest()}"'
        assert etag.startswith('"') and etag.endswith('"')


class TestAccessControl:
    async def test_cannot_download_another_users_file(
        self, upload_service, download_service, seeded, repo, chunk_size
    ):
        user = seeded["user"]["id"]
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="private.bin", data=os.urandom(500), chunk_size=chunk_size,
        )
        other = await repo.create_user(
            email="intruder@example.com", password_hash="x", display_name="Intruder"
        )
        with pytest.raises(ProblemError) as exc:
            await collect(
                download_service.full_stream(node_id=node["id"], owner_id=other["id"])
            )
        assert exc.value.code == "not_found"

    async def test_cannot_download_unready_file(
        self, upload_service, download_service, seeded, chunk_size
    ):
        """Serving a partial file with a 200 status would silently truncate."""
        user = seeded["user"]["id"]
        session = await upload_service.create_session(
            owner_id=user, parent_id=seeded["sub"]["id"], name="pending.bin",
            size_bytes=2 * chunk_size, mime_type=None, chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size)
        await upload_service.put_chunk(
            upload_id=session["id"], owner_id=user, chunk_index=0,
            body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
        )
        with pytest.raises(ProblemError) as exc:
            await collect(
                download_service.full_stream(
                    node_id=session["node_id"], owner_id=user
                )
            )
        assert exc.value.code == "not_found"

    async def test_cannot_download_trashed_file(
        self, upload_service, download_service, seeded, repo, chunk_size
    ):
        user = seeded["user"]["id"]
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="trashed.bin", data=os.urandom(100), chunk_size=chunk_size,
        )
        await repo.trash_node(node["id"], user, 30)
        with pytest.raises(ProblemError):
            await collect(download_service.full_stream(node_id=node["id"], owner_id=user))

    async def test_folder_has_no_content(
        self, download_service, seeded
    ):
        with pytest.raises(ProblemError) as exc:
            await collect(
                download_service.full_stream(
                    node_id=seeded["sub"]["id"], owner_id=seeded["user"]["id"]
                )
            )
        assert exc.value.code == "not_found"


class TestReplicaFailover:
    """Telegram messages can vanish (out-of-band deletion, channel purge,
    revoked account). A chunk must survive that if a replica exists."""

    async def test_failover_to_replica_when_primary_deleted(
        self, upload_service, download_service, seeded, repo, store, chunk_size
    ):
        user = seeded["user"]["id"]
        data = os.urandom(chunk_size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="failover.bin", data=data, chunk_size=chunk_size,
        )

        chunks = await repo.list_chunks(node["id"])
        primary = chunks[0]

        # The primary's stored bytes, relocated to a second message.
        original = store.message(
            primary["telegram_channel_id"], primary["telegram_message_id"]
        ).data
        stored = await store.upload_chunk(
            channel_id=-1002222222222,
            data=original,
            filename="replica.tdc",
            session_label="pool-b",
        )
        await repo.add_chunk_replica(
            chunk_id=primary["id"],
            storage_pool_id=primary["storage_pool_id"],
            telegram_channel_id=stored.channel_id,
            telegram_message_id=stored.message_id,
            telegram_file_id=stored.file_id,
            telegram_access_hash=stored.access_hash,
            telegram_dc_id=stored.dc_id,
        )

        # Now destroy the primary out of band.
        store.delete_message_out_of_band(
            primary["telegram_channel_id"], primary["telegram_message_id"]
        )

        out = await collect(download_service.full_stream(node_id=node["id"], owner_id=user))
        assert out == data, "read should have failed over to the replica"

    async def test_failure_is_recorded_once_for_later_reads(
        self, upload_service, download_service, seeded, repo, store, chunk_size
    ):
        """Learning a dead message once avoids a retry storm on every request."""
        user = seeded["user"]["id"]
        data = os.urandom(chunk_size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="dead.bin", data=data, chunk_size=chunk_size,
        )
        chunks = await repo.list_chunks(node["id"])
        primary = chunks[0]
        store.delete_message_out_of_band(
            primary["telegram_channel_id"], primary["telegram_message_id"]
        )

        with pytest.raises(ProblemError) as exc:
            await collect(download_service.full_stream(node_id=node["id"], owner_id=user))
        assert exc.value.code == "chunk_unreadable"
        assert len(repo.chunk_read_failures) == 1

    async def test_unreadable_chunk_raises_rather_than_truncating(
        self, upload_service, download_service, seeded, store, chunk_size
    ):
        user = seeded["user"]["id"]
        data = os.urandom(2 * chunk_size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="hole.bin", data=data, chunk_size=chunk_size,
        )
        chunk = await download_service.repo.get_chunk(node["id"], 0)
        store.delete_message_out_of_band(
            chunk["telegram_channel_id"], chunk["telegram_message_id"]
        )
        # A short body returned as success would be the worst outcome.
        with pytest.raises(ProblemError):
            await collect(download_service.full_stream(node_id=node["id"], owner_id=user))


class TestTamperDetection:
    async def test_corrupted_stored_chunk_fails_authentication(
        self, upload_service, download_service, seeded, repo, store, chunk_size
    ):
        """The end-to-end guarantee: if Telegram-side bytes are altered, the
        download must fail loudly rather than emit corrupted plaintext."""
        user = seeded["user"]["id"]
        data = os.urandom(chunk_size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="tamper.bin", data=data, chunk_size=chunk_size,
        )
        chunks = await repo.list_chunks(node["id"])
        message = store.message(
            chunks[0]["telegram_channel_id"], chunks[0]["telegram_message_id"]
        )
        corrupted = bytearray(message.data)
        corrupted[5] ^= 0xFF
        message.data = bytes(corrupted)

        with pytest.raises(ProblemError) as exc:
            await collect(download_service.full_stream(node_id=node["id"], owner_id=user))
        assert exc.value.code == "chunk_unreadable"

    async def test_swapped_chunk_messages_fail_authentication(
        self, upload_service, download_service, seeded, repo, store, chunk_size
    ):
        """Reordering chunks in storage must not produce a silently scrambled
        file — this is exactly what the AAD index binding defends against."""
        user = seeded["user"]["id"]
        data = os.urandom(2 * chunk_size)
        node = await upload_bytes(
            upload_service, owner_id=user, parent_id=seeded["sub"]["id"],
            name="swap.bin", data=data, chunk_size=chunk_size,
        )
        chunks = await repo.list_chunks(node["id"])
        first, second = chunks[0], chunks[1]
        msg_a = store.message(first["telegram_channel_id"], first["telegram_message_id"])
        msg_b = store.message(second["telegram_channel_id"], second["telegram_message_id"])
        msg_a.data, msg_b.data = msg_b.data, msg_a.data

        with pytest.raises(ProblemError) as exc:
            await collect(download_service.full_stream(node_id=node["id"], owner_id=user))
        assert exc.value.code == "chunk_unreadable"