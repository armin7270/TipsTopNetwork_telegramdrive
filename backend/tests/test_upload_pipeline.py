"""Upload pipeline: resumption, idempotency, quota, integrity, abort."""

from __future__ import annotations

import hashlib
import os

import pytest

from app.core import crypto
from app.core.errors import ProblemError
from tests.conftest import upload_bytes

MB = 1024 * 1024


class TestHappyPath:
    async def test_single_chunk_upload(self, upload_service, seeded, chunk_size):
        data = os.urandom(4096)
        node = await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="small.bin",
            data=data,
            chunk_size=chunk_size,
        )
        assert node["kind"] == "file"
        assert node["upload_state"] == "ready"
        assert node["size_bytes"] == 4096
        assert node["total_chunks"] == 1
        assert bytes(node["sha256"]).hex() == hashlib.sha256(data).hexdigest()

    async def test_multi_chunk_upload_creates_one_message_per_chunk(
        self, upload_service, seeded, chunk_size, store
    ):
        data = os.urandom(3 * chunk_size + 5000)
        await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="multi.bin",
            data=data,
            chunk_size=chunk_size,
        )
        assert store.live_message_count() == 4

    async def test_zero_byte_file(self, upload_service, seeded, chunk_size):
        node = await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="empty.txt",
            data=b"",
            chunk_size=chunk_size,
        )
        assert node["size_bytes"] == 0
        assert node["total_chunks"] == 1
        assert node["upload_state"] == "ready"

    async def test_chunks_are_stored_encrypted(self, upload_service, seeded, chunk_size, store):
        """The whole point of the design: Telegram must never see plaintext."""
        marker = b"TOPSECRETMARKER" * 100
        data = marker + os.urandom(chunk_size - len(marker))
        await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="secret.bin",
            data=data,
            chunk_size=chunk_size,
        )
        for channel in store.channels.values():
            for message in channel.messages.values():
                assert marker not in message.data

    async def test_stored_size_is_plaintext_plus_gcm_tag(
        self, upload_service, seeded, chunk_size, store
    ):
        data = os.urandom(2 * chunk_size)
        await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="sized.bin",
            data=data,
            chunk_size=chunk_size,
        )
        # Exactly 16 bytes of GCM tag per chunk, no other overhead.
        assert store.total_stored_bytes() == len(data) + 2 * 16

    async def test_chunks_spread_across_pools(self, upload_service, seeded, chunk_size, repo):
        """Sharding across channels is the primary defence against per-chat rate
        limits, so it is asserted rather than hoped for."""
        data = os.urandom(6 * chunk_size)
        node = await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="spread.bin",
            data=data,
            chunk_size=chunk_size,
        )
        channels = {c["telegram_channel_id"] for c in await repo.list_chunks(node["id"])}
        assert len(channels) == 2

    async def test_unique_iv_per_chunk(self, upload_service, seeded, chunk_size, repo):
        data = os.urandom(4 * chunk_size)
        node = await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="ivs.bin",
            data=data,
            chunk_size=chunk_size,
        )
        chunks = await repo.list_chunks(node["id"])
        ivs = {bytes(c["iv"]) for c in chunks}
        assert len(ivs) == len(chunks) == 4


class TestResumption:
    async def test_missing_chunks_reports_all_initially(
        self, upload_service, seeded, chunk_size
    ):
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="resume.bin",
            size_bytes=3 * chunk_size,
            mime_type="application/octet-stream",
            chunk_size=chunk_size,
        )
        missing = await upload_service.missing_chunks(session["id"], seeded["user"]["id"])
        assert missing["missing_chunks"] == [0, 1, 2]
        assert missing["received_bytes"] == 0

    async def test_missing_chunks_tracks_partial_progress(
        self, upload_service, seeded, chunk_size
    ):
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="partial.bin",
            size_bytes=3 * chunk_size,
            mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size)
        await upload_service.put_chunk(
            upload_id=session["id"],
            owner_id=seeded["user"]["id"],
            chunk_index=1,
            body=body,
            declared_sha256=hashlib.sha256(body).hexdigest(),
        )
        missing = await upload_service.missing_chunks(session["id"], seeded["user"]["id"])
        assert missing["missing_chunks"] == [0, 2]
        assert missing["received_bytes"] == chunk_size

    async def test_out_of_order_upload_completes_correctly(
        self, upload_service, download_service, seeded, chunk_size
    ):
        """Chunks uploaded in a scrambled order must reassemble exactly."""
        data = os.urandom(4 * chunk_size + 999)
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="scrambled.bin",
            size_bytes=len(data),
            mime_type=None,
            sha256_hex=hashlib.sha256(data).hexdigest(),
            chunk_size=chunk_size,
        )
        for index in [3, 0, 4, 1, 2]:
            start = index * chunk_size
            body = data[start : start + chunk_size]
            await upload_service.put_chunk(
                upload_id=session["id"],
                owner_id=seeded["user"]["id"],
                chunk_index=index,
                body=body,
                declared_sha256=hashlib.sha256(body).hexdigest(),
            )
        node = await upload_service.complete(
            upload_id=session["id"], owner_id=seeded["user"]["id"]
        )
        assert node["upload_state"] == "ready"


class TestIdempotency:
    async def test_same_chunk_twice_is_idempotent(
        self, upload_service, seeded, chunk_size, store
    ):
        """A client retry after a lost response must not create a second
        Telegram message."""
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="idem.bin",
            size_bytes=chunk_size,
            mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size)
        digest = hashlib.sha256(body).hexdigest()

        first = await upload_service.put_chunk(
            upload_id=session["id"], owner_id=seeded["user"]["id"],
            chunk_index=0, body=body, declared_sha256=digest,
        )
        second = await upload_service.put_chunk(
            upload_id=session["id"], owner_id=seeded["user"]["id"],
            chunk_index=0, body=body, declared_sha256=digest,
        )

        assert first["status"] == "stored"
        assert second["status"] == "already_present"
        assert second["telegram_message_id"] == first["telegram_message_id"]
        assert store.live_message_count() == 1

    async def test_same_index_different_content_conflicts(
        self, upload_service, seeded, chunk_size
    ):
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="conflict.bin",
            size_bytes=chunk_size,
            mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size)
        await upload_service.put_chunk(
            upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=0,
            body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
        )
        other = os.urandom(chunk_size)
        with pytest.raises(ProblemError) as exc:
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=0,
                body=other, declared_sha256=hashlib.sha256(other).hexdigest(),
            )
        assert exc.value.code == "chunk_already_uploaded"
        assert exc.value.status_code == 409

    async def test_create_session_idempotency_key_replay(
        self, upload_service, seeded, chunk_size
    ):
        key = "11111111-2222-3333-4444-555555555555"
        first = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="replay.bin", size_bytes=100, mime_type=None,
            chunk_size=chunk_size, idempotency_key=key,
        )
        second = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="replay.bin", size_bytes=100, mime_type=None,
            chunk_size=chunk_size, idempotency_key=key,
        )
        assert first["id"] == second["id"]


class TestValidation:
    async def test_wrong_chunk_size_rejected(self, upload_service, seeded, chunk_size):
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="bad.bin", size_bytes=2 * chunk_size, mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(100)
        with pytest.raises(ProblemError) as exc:
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=0,
                body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
            )
        # A short non-final chunk means the client sliced wrong.
        assert exc.value.code == "size_mismatch"

    async def test_oversized_body_rejected(self, upload_service, seeded, chunk_size):
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="big.bin", size_bytes=4 * chunk_size, mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size + 1)
        with pytest.raises(ProblemError) as exc:
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=0,
                body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert exc.value.code == "chunk_too_large"
        assert exc.value.status_code == 413

    async def test_hash_mismatch_rejected_before_any_upload(
        self, upload_service, seeded, chunk_size, store
    ):
        """A corrupt chunk must cost zero Telegram requests, since those
        requests are the scarce resource."""
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="hash.bin", size_bytes=chunk_size, mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size)
        with pytest.raises(ProblemError) as exc:
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=0,
                body=body, declared_sha256="0" * 64,
            )
        assert exc.value.code == "chunk_hash_mismatch"
        assert exc.value.status_code == 400
        assert store.live_message_count() == 0

    async def test_chunk_index_out_of_range(self, upload_service, seeded, chunk_size):
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="range.bin", size_bytes=chunk_size, mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size)
        with pytest.raises(ProblemError) as exc:
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=5,
                body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert exc.value.code == "chunk_index_out_of_range"
        assert exc.value.status_code == 416

    async def test_complete_with_missing_chunks(self, upload_service, seeded, chunk_size):
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="incomplete.bin", size_bytes=2 * chunk_size, mime_type=None,
            chunk_size=chunk_size,
        )
        body = os.urandom(chunk_size)
        await upload_service.put_chunk(
            upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=0,
            body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
        )
        with pytest.raises(ProblemError) as exc:
            await upload_service.complete(
                upload_id=session["id"], owner_id=seeded["user"]["id"]
            )
        assert exc.value.code == "upload_incomplete"
        # The missing list rides along so a client can resume without another
        # round-trip.
        assert exc.value.extensions["missing_chunks"] == [1]

    async def test_wrong_chunk_size_not_multiple_of_mib(self, upload_service, seeded):
        with pytest.raises(ProblemError):
            await upload_service.create_session(
                owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
                name="odd.bin", size_bytes=1000, mime_type=None,
                chunk_size=1000,   # not a multiple of 1 MiB
            )

    async def test_duplicate_name_conflicts(self, upload_service, seeded, chunk_size):
        await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="dup.bin", size_bytes=10, mime_type=None, chunk_size=chunk_size,
        )
        with pytest.raises(ProblemError) as exc:
            await upload_service.create_session(
                owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
                name="dup.bin", size_bytes=10, mime_type=None, chunk_size=chunk_size,
            )
        assert exc.value.code == "node_name_conflict"

    async def test_cannot_upload_into_trashed_folder(
        self, upload_service, seeded, repo, chunk_size
    ):
        await repo.trash_node(seeded["sub"]["id"], seeded["user"]["id"], 30)
        with pytest.raises(ProblemError) as exc:
            await upload_service.create_session(
                owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
                name="nope.bin", size_bytes=10, mime_type=None, chunk_size=chunk_size,
            )
        assert exc.value.code == "node_name_conflict"


class TestWholeFileIntegrity:
    async def test_wrong_declared_whole_file_hash_rejected(
        self, upload_service, seeded, chunk_size
    ):
        """Catches a client-side reassembly bug: every chunk is valid but the
        whole file is not what the client intended."""
        data = os.urandom(2 * chunk_size)
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="whole.bin", size_bytes=len(data), mime_type=None,
            chunk_size=chunk_size,
        )
        for index in range(2):
            body = data[index * chunk_size : (index + 1) * chunk_size]
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=seeded["user"]["id"],
                chunk_index=index, body=body,
                declared_sha256=hashlib.sha256(body).hexdigest(),
            )
        with pytest.raises(ProblemError) as exc:
            await upload_service.complete(
                upload_id=session["id"], owner_id=seeded["user"]["id"],
                declared_sha256="f" * 64,
            )
        assert exc.value.code == "file_hash_mismatch"

    async def test_node_stays_unready_after_hash_failure(
        self, upload_service, seeded, repo, chunk_size
    ):
        """A file that fails integrity must not be visible as ready, or a
        truncated download would be served as a success."""
        data = os.urandom(chunk_size)
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="unready.bin", size_bytes=len(data), mime_type=None,
            chunk_size=chunk_size,
        )
        body = data
        await upload_service.put_chunk(
            upload_id=session["id"], owner_id=seeded["user"]["id"], chunk_index=0,
            body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
        )
        with pytest.raises(ProblemError):
            await upload_service.complete(
                upload_id=session["id"], owner_id=seeded["user"]["id"],
                declared_sha256="a" * 64,
            )
        node = await repo.get_node(session["node_id"], seeded["user"]["id"])
        assert node["upload_state"] == "uploading"


class TestQuota:
    async def test_quota_reserved_at_session_creation(
        self, upload_service, seeded, repo, chunk_size
    ):
        user_id = seeded["user"]["id"]
        repo.users[user_id]["quota_bytes"] = 10 * MB

        session = await upload_service.create_session(
            owner_id=user_id, parent_id=seeded["sub"]["id"], name="q.bin",
            size_bytes=4 * MB, mime_type=None, chunk_size=chunk_size,
        )
        assert session["reserved_bytes"] == 4 * MB
        assert repo.users[user_id]["used_bytes"] == 4 * MB

    async def test_quota_exceeded_rejected(self, upload_service, seeded, repo, chunk_size):
        user_id = seeded["user"]["id"]
        repo.users[user_id]["quota_bytes"] = 2 * MB
        with pytest.raises(ProblemError) as exc:
            await upload_service.create_session(
                owner_id=user_id, parent_id=seeded["sub"]["id"], name="toobig.bin",
                size_bytes=5 * MB, mime_type=None, chunk_size=chunk_size,
            )
        assert exc.value.code == "quota_exceeded"
        assert exc.value.status_code == 507
        assert exc.value.extensions["quota_bytes"] == 2 * MB

    async def test_concurrent_reservations_cannot_both_win(
        self, upload_service, seeded, repo, chunk_size
    ):
        """The check-then-act race that reservation exists to prevent."""
        user_id = seeded["user"]["id"]
        repo.users[user_id]["quota_bytes"] = 3 * MB

        await upload_service.create_session(
            owner_id=user_id, parent_id=seeded["sub"]["id"], name="a.bin",
            size_bytes=2 * MB, mime_type=None, chunk_size=chunk_size,
        )
        with pytest.raises(ProblemError) as exc:
            await upload_service.create_session(
                owner_id=user_id, parent_id=seeded["sub"]["id"], name="b.bin",
                size_bytes=2 * MB, mime_type=None, chunk_size=chunk_size,
            )
        assert exc.value.code == "quota_exceeded"

    async def test_unlimited_quota_when_zero(self, upload_service, seeded, repo, chunk_size):
        user_id = seeded["user"]["id"]
        repo.users[user_id]["quota_bytes"] = 0
        session = await upload_service.create_session(
            owner_id=user_id, parent_id=seeded["sub"]["id"], name="unlimited.bin",
            size_bytes=500 * MB, mime_type=None, chunk_size=chunk_size,
        )
        assert session["reserved_bytes"] == 500 * MB


class TestAbort:
    async def test_abort_releases_quota(self, upload_service, seeded, repo, chunk_size):
        user_id = seeded["user"]["id"]
        repo.users[user_id]["quota_bytes"] = 10 * MB
        session = await upload_service.create_session(
            owner_id=user_id, parent_id=seeded["sub"]["id"], name="abort.bin",
            size_bytes=4 * MB, mime_type=None, chunk_size=chunk_size,
        )
        assert repo.users[user_id]["used_bytes"] == 4 * MB

        await upload_service.abort(upload_id=session["id"], owner_id=user_id)
        assert repo.users[user_id]["used_bytes"] == 0

    async def test_abort_removes_node_and_schedules_gc(
        self, upload_service, seeded, repo, chunk_size
    ):
        user_id = seeded["user"]["id"]
        session = await upload_service.create_session(
            owner_id=user_id, parent_id=seeded["sub"]["id"], name="abort2.bin",
            size_bytes=MB, mime_type=None, chunk_size=chunk_size,
        )
        node_id = session["node_id"]
        await upload_service.abort(upload_id=session["id"], owner_id=user_id)

        assert await repo.get_node(node_id, user_id) is None
        assert any(j["kind"] == "gc_orphan_chunks" for j in repo.jobs)

    async def test_chunk_after_abort_is_rejected(
        self, upload_service, seeded, chunk_size
    ):
        user_id = seeded["user"]["id"]
        session = await upload_service.create_session(
            owner_id=user_id, parent_id=seeded["sub"]["id"], name="late.bin",
            size_bytes=chunk_size, mime_type=None, chunk_size=chunk_size,
        )
        await upload_service.abort(upload_id=session["id"], owner_id=user_id)
        body = os.urandom(chunk_size)
        with pytest.raises(ProblemError) as exc:
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=user_id, chunk_index=0,
                body=body, declared_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert exc.value.code in {"upload_expired", "not_found"}


class TestStreamIngestion:
    async def test_ingest_stream_slices_and_stores(
        self, upload_service, seeded, repo, chunk_size
    ):
        """The non-resumable path for clients that cannot slice locally."""
        data = os.urandom(3 * chunk_size + 77)
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="streamed.bin", size_bytes=len(data), mime_type=None,
            chunk_size=chunk_size,
        )

        async def source():
            for i in range(0, len(data), 4096):
                yield data[i : i + 4096]

        node_id = await upload_service.ingest_stream(
            session=session, stream=source(), owner_id=seeded["user"]["id"]
        )
        node = await repo.get_node(node_id, seeded["user"]["id"])
        assert node["upload_state"] == "ready"
        assert node["size_bytes"] == len(data)

        chunks = await repo.list_chunks(node_id)
        assert len(chunks) == 4
        assert sum(c["plaintext_size"] for c in chunks) == len(data)


class TestOwnershipIsolation:
    async def test_cannot_upload_into_another_users_folder(
        self, upload_service, seeded, repo, chunk_size
    ):
        other = await repo.create_user(
            email="other@example.com", password_hash="x", display_name="Other"
        )
        with pytest.raises(ProblemError) as exc:
            await upload_service.create_session(
                owner_id=other["id"], parent_id=seeded["sub"]["id"],
                name="steal.bin", size_bytes=10, mime_type=None, chunk_size=chunk_size,
            )
        assert exc.value.code == "not_found"

    async def test_cannot_touch_another_users_session(
        self, upload_service, seeded, repo, chunk_size
    ):
        other = await repo.create_user(
            email="other2@example.com", password_hash="x", display_name="Other"
        )
        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="mine.bin", size_bytes=chunk_size, mime_type=None, chunk_size=chunk_size,
        )
        with pytest.raises(ProblemError) as exc:
            await upload_service.get_session(session["id"], other["id"])
        assert exc.value.code == "not_found"