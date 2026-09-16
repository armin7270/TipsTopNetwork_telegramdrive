"""Upload pipeline: chunk-addressed, resumable, encrypt-on-the-fly.

The protocol this implements::

    POST /uploads                     -> create session, reserve quota
    GET  /uploads/{id}/missing        -> exact resume handle
    PUT  /uploads/{id}/chunks/{i}     -> raw bytes, one chunk per request
    POST /uploads/{id}/complete       -> verify, commit node, mark ready

Design decisions worth stating, because the obvious alternatives are worse:

**Chunk-addressed PUT, not one resumable multipart body.** A single 2 GB body
cannot be resumed at a known point, cannot be parallelised, and pins a
connection for minutes. Addressing chunks individually means every unit of work
is independently retryable and the client controls pacing — which matters,
because the real constraint is Telegram's rate limit, not bandwidth.

**The server buffers one chunk and never touches disk.** This is what makes
"no server disk for large files" literally true. The consequence is a hard
memory ceiling: ``MAX_CONCURRENT_UPLOADS * chunk_size`` must fit in the
container. That trade is taken deliberately, since spooling to disk would defeat
the point of the system.

**Idempotency is structural.** ``ON CONFLICT DO NOTHING`` on the chunk insert
means a client retry after a lost response resolves to success rather than
creating a second Telegram message. The same index with *different* content is a
genuine conflict and is rejected.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from app.core import crypto
from app.core.config import MIB, Settings
from app.core.errors import (
    chunk_already_uploaded,
    chunk_hash_mismatch,
    chunk_index_out_of_range,
    chunk_too_large,
    file_hash_mismatch,
    node_name_conflict,
    not_found,
    quota_exceeded,
    size_mismatch,
    storage_unavailable,
    upload_expired,
    upload_incomplete,
)
from app.telegram.base import (
    FloodWaitError,
    SessionUnavailableError,
    StorageBackend,
    StorageError,
)
from app.telegram.pool import SessionPool

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """How a file of ``size_bytes`` divides into chunks.

    The last chunk absorbs the remainder, so ``total_chunks`` chunks always sum
    exactly to ``size_bytes``. A zero-byte file still gets one empty chunk so
    that the chunk/message invariant (one node -> at least one message) holds and
    the download path never has to special-case emptiness.
    """

    size_bytes: int
    chunk_size: int

    @property
    def total_chunks(self) -> int:
        if self.size_bytes == 0:
            return 1
        return (self.size_bytes + self.chunk_size - 1) // self.chunk_size

    def offset_of(self, index: int) -> int:
        return index * self.chunk_size

    def size_of(self, index: int) -> int:
        """Plaintext byte length of chunk ``index``."""
        if index < 0 or index >= self.total_chunks:
            raise IndexError(f"chunk index {index} out of range 0..{self.total_chunks - 1}")
        if self.size_bytes == 0:
            return 0
        if index == self.total_chunks - 1:
            remainder = self.size_bytes - self.offset_of(index)
            return remainder
        return self.chunk_size

    def validate(self) -> None:
        total = sum(self.size_of(i) for i in range(self.total_chunks))
        if total != self.size_bytes:
            raise ValueError(
                f"chunk plan is inconsistent: chunks sum to {total}, "
                f"file declares {self.size_bytes}"
            )


class UploadService:
    """Orchestrates the resumable chunked upload."""

    def __init__(
        self,
        *,
        pool: SessionPool,
        backend: StorageBackend,
        settings: Settings,
        repository: Any,
    ) -> None:
        self.pool = pool
        self.backend = backend
        self.settings = settings
        self.repo = repository
        # Bounds how much chunk data is in RAM at once. Without this, N
        # simultaneous uploads each buffering a full chunk would blow the
        # container memory limit.
        self._upload_slots = asyncio.Semaphore(settings.max_concurrent_uploads)

    # -- session lifecycle ------------------------------------------------

    async def create_session(
        self,
        *,
        owner_id: str,
        parent_id: str,
        name: str,
        size_bytes: int,
        mime_type: str | None,
        sha256_hex: str | None = None,
        chunk_size: int | None = None,
        encryption_mode: str = "server_managed",
        overwrite: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create an upload session and reserve quota.

        Idempotency is checked first: a replayed request must return the original
        session rather than reserving quota twice or creating a second node.
        """
        if idempotency_key:
            existing = await self.repo.find_upload_by_idempotency(owner_id, idempotency_key)
            if existing is not None:
                log.info("upload: idempotent replay of key %s", idempotency_key)
                return await self.repo.get_upload_session(existing["id"])

        effective_chunk_size = chunk_size or self.settings.storage_chunk_size_bytes
        self._validate_chunk_size(effective_chunk_size)

        parent = await self.repo.get_node(parent_id, owner_id)
        if parent is None:
            raise not_found(f"Parent folder {parent_id} was not found")
        if parent["kind"] != "folder":
            raise node_name_conflict(f"Parent {parent_id} is a file, not a folder")
        if parent.get("trashed_at"):
            raise node_name_conflict("Cannot upload into a trashed folder")

        sibling = await self.repo.find_child_by_name(owner_id, parent_id, name)
        if sibling is not None:
            if not overwrite:
                raise node_name_conflict(name)
            # Replacing a file must not leak the old chunks: they are released
            # explicitly rather than being orphaned in Telegram storage.
            if sibling["kind"] == "file":
                await self._release_node_chunks(sibling["id"], owner_id)

        # Reserve quota BEFORE any chunk is uploaded. Without the reservation two
        # concurrent uploads could both pass the check and jointly exceed the
        # limit, which is the classic check-then-act race.
        ok, used, quota = await self.repo.reserve_quota(owner_id, size_bytes)
        if not ok:
            raise quota_exceeded(used, quota, size_bytes)

        plan = ChunkPlan(size_bytes=size_bytes, chunk_size=effective_chunk_size)
        plan.validate()

        node = await self.repo.create_file_node(
            owner_id=owner_id,
            parent_id=parent_id,
            name=name,
            size_bytes=size_bytes,
            mime_type=mime_type,
            chunk_size=effective_chunk_size,
            total_chunks=plan.total_chunks,
            encryption_mode=encryption_mode,
            upload_state="uploading",
        )

        session = await self.repo.create_upload_session(
            owner_id=owner_id,
            node_id=node["id"],
            parent_id=parent_id,
            name=name,
            size_bytes=size_bytes,
            mime_type=mime_type,
            expected_sha256=sha256_hex,
            chunk_size=effective_chunk_size,
            total_chunks=plan.total_chunks,
            encryption_mode=encryption_mode,
            idempotency_key=idempotency_key,
            expires_at=utcnow() + timedelta(seconds=self.settings.upload_session_ttl_seconds),
            reserved_bytes=size_bytes,
        )

        if encryption_mode != "zero_knowledge":
            node_id_bytes = uuid.UUID(str(node["id"])).bytes
            session["_dek"] = await self._user_dek(owner_id, node_id_bytes)

        log.info(
            "upload: session %s created for %r (%d bytes, %d chunks of %d)",
            session["id"],
            name,
            size_bytes,
            plan.total_chunks,
            effective_chunk_size,
        )
        return session

    async def _user_dek(self, owner_id: str, node_id_bytes: bytes) -> bytes:
        """Return the user's DEK, preferring the wrapped value from the database.

        Falls back to deriving from the KEK when no wrapped DEK is present, which
        keeps pre-existing rows readable and lets the in-memory test repository
        work without emulating key storage.
        """
        wrapped = await self.repo.get_user_wrapped_dek(owner_id)
        if wrapped:
            return crypto.unwrap_dek(
                wrapped, self.settings.master_kek, user_id_bytes=uuid.UUID(str(owner_id)).bytes
            )
        return crypto.derive_subkey(
            self.settings.master_kek, salt=node_id_bytes, info=b"teledrive/user-dek/v1"
        )

    async def get_session(self, upload_id: str, owner_id: str) -> dict[str, Any]:
        session = await self.repo.get_upload_session(upload_id)
        if session is None or session["owner_id"] != owner_id:
            raise not_found(f"Upload session {upload_id} was not found")
        return session

    async def missing_chunks(self, upload_id: str, owner_id: str) -> dict[str, Any]:
        """The exact resume handle.

        Computed by set difference rather than by a ``received_chunks`` counter,
        because the counter is an optimisation while the chunk table is the
        truth. A client that trusts a counter after a partial failure resumes at
        the wrong place; a client that asks this endpoint cannot.
        """
        session = await self.get_session(upload_id, owner_id)
        if session["status"] in {"aborted", "expired"}:
            raise upload_expired(upload_id)

        present = set(await self.repo.list_chunk_indices(session["node_id"]))
        all_indices = set(range(session["total_chunks"]))
        missing = sorted(all_indices - present)

        received_bytes = await self.repo.sum_chunk_plaintext_bytes(session["node_id"])
        return {
            "upload_id": upload_id,
            "missing_chunks": missing,
            "received_chunks": sorted(present),
            "received_bytes": received_bytes,
            "total_chunks": session["total_chunks"],
            "size_bytes": session["size_bytes"],
        }

    # -- chunk receipt ----------------------------------------------------

    async def put_chunk(
        self,
        *,
        upload_id: str,
        owner_id: str,
        chunk_index: int,
        body: bytes,
        declared_sha256: str,
        client_iv_b64: str | None = None,
    ) -> dict[str, Any]:
        """Accept one chunk: verify, encrypt, upload, commit.

        Order matters. The hash is verified *before* anything is sent to
        Telegram, so a corrupt chunk costs zero platform requests — which is the
        resource that must be conserved. A chunk that fails verification is
        rejected outright rather than being uploaded and corrected later.
        """
        session = await self.get_session(upload_id, owner_id)
        if session["status"] in {"aborted", "expired"}:
            raise upload_expired(upload_id)
        if session["status"] == "completed":
            raise upload_expired(f"Upload session {upload_id} is already completed")

        if chunk_index < 0 or chunk_index >= session["total_chunks"]:
            raise chunk_index_out_of_range(chunk_index, session["total_chunks"])

        plan = ChunkPlan(
            size_bytes=session["size_bytes"], chunk_size=session["chunk_size"]
        )
        expected_size = plan.size_of(chunk_index)

        if len(body) > session["chunk_size"]:
            raise chunk_too_large(session["chunk_size"], len(body))
        if len(body) != expected_size:
            raise size_mismatch(expected_size, len(body))

        actual_sha256 = crypto.sha256_hex(body)
        if not crypto.constant_time_equals(actual_sha256, declared_sha256.lower()):
            raise chunk_hash_mismatch(declared_sha256.lower(), actual_sha256)

        # Idempotent replay: same index, same plaintext -> report the existing
        # receipt. This is what makes a client retry after a lost response safe.
        existing = await self.repo.get_chunk(session["node_id"], chunk_index)
        if existing is not None:
            if crypto.constant_time_equals(
                bytes(existing["sha256"]).hex(), actual_sha256
            ):
                log.info(
                    "upload: chunk %d of %s already stored (idempotent replay)",
                    chunk_index,
                    upload_id,
                )
                return self._receipt(existing, rewound=True)
            raise chunk_already_uploaded(chunk_index)

        if "_dek" not in session and session.get("encryption_mode") != "zero_knowledge":
            node_id_bytes = uuid.UUID(str(session["node_id"])).bytes
            session["_dek"] = await self._user_dek(session["owner_id"], node_id_bytes)

        sealed = self._seal_chunk(
            session=session,
            chunk_index=chunk_index,
            body=body,
            client_iv_b64=client_iv_b64,
        )

        pool_channel = await self.repo.choose_storage_pool(
            size_bytes=sealed.wire_size, prefer_available=True
        )

        filename = (
            f"{session['node_id'].replace('-', '')[:16]}"
            f".{chunk_index:06d}.tdc"
        )

        async with self._upload_slots:
            stored = await self._send_with_rotation(
                channel_id=pool_channel["telegram_channel_id"],
                payload=sealed.wire,
                filename=filename,
            )

        chunk = await self.repo.insert_chunk(
            node_id=session["node_id"],
            chunk_index=chunk_index,
            plaintext_size=len(body),
            ciphertext_size=len(sealed.wire),
            sha256=bytes.fromhex(actual_sha256),
            iv=sealed.iv,
            auth_tag=sealed.tag,
            storage_pool_id=pool_channel["id"],
            telegram_channel_id=stored.channel_id,
            telegram_message_id=stored.message_id,
            telegram_file_id=stored.file_id,
            telegram_access_hash=stored.access_hash,
            telegram_dc_id=stored.dc_id,
            uploaded_by_session=stored_session_id(self.pool),
        )
        if chunk is None:
            # Lost a race with a concurrent identical PUT: the other writer's
            # row is authoritative and identical, so this is still a success.
            chunk = await self.repo.get_chunk(session["node_id"], chunk_index)
            if chunk is None:  # pragma: no cover - defensive
                raise storage_unavailable("chunk insert was lost")
            return self._receipt(chunk, rewound=True)

        await self.repo.bump_upload_progress(
            upload_id=upload_id,
            plaintext_delta=len(body),
            ciphertext_delta=len(sealed.wire),
        )

        log.debug(
            "upload: chunk %d/%d of %s -> message %d on channel %d (%d bytes)",
            chunk_index,
            session["total_chunks"] - 1,
            upload_id,
            stored.message_id,
            stored.channel_id,
            len(sealed.wire),
        )
        return self._receipt(chunk)

    def _seal_chunk(
        self,
        *,
        session: dict[str, Any],
        chunk_index: int,
        body: bytes,
        client_iv_b64: str | None,
    ) -> crypto.SealedChunk:
        """Encrypt one chunk, deriving key and AAD from the session.

        In ``zero_knowledge`` mode the client supplies the IV. Its uniqueness is
        therefore the client's responsibility — a real limitation of that mode,
        and the reason ``server_managed`` is the default. The server still
        enforces that the IV has the correct length, since a short IV would fail
        at the cipher layer anyway with a less clear error.
        """
        node_id_bytes = uuid.UUID(str(session["node_id"])).bytes
        aad = crypto.build_chunk_aad(
            node_id_bytes, chunk_index, session["total_chunks"]
        )

        if session["encryption_mode"] == "zero_knowledge":
            if not client_iv_b64:
                raise chunk_hash_mismatch(
                    "X-Chunk-IV is required for zero_knowledge uploads",
                    "missing",
                )
            import base64 as _b64

            try:
                iv = _b64.b64decode(client_iv_b64, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise chunk_hash_mismatch("X-Chunk-IV is not valid base64", str(exc)) from exc
            if len(iv) != crypto.GCM_IV_BYTES:
                raise chunk_hash_mismatch(
                    f"X-Chunk-IV must be {crypto.GCM_IV_BYTES} bytes",
                    f"{len(iv)} bytes",
                )
            # The client holds the real key; the server seals with a derived
            # session key purely so the stored blob is opaque to Telegram. The
            # client's own E2EE layer is authoritative for confidentiality.
            key = crypto.derive_file_key(
                self.settings.master_kek, node_id_bytes=node_id_bytes
            )
            return crypto.seal_chunk(key, body, aad=aad, iv=iv)

        dek = session.get("_dek")
        if dek is None:
            dek = crypto.derive_subkey(
                self.settings.master_kek,
                salt=node_id_bytes,
                info=b"teledrive/user-dek/v1",
            )
        key = crypto.derive_file_key(dek, node_id_bytes=node_id_bytes)
        return crypto.seal_chunk(key, body, aad=aad)

    async def _send_with_rotation(
        self, *, channel_id: int, payload: bytes, filename: str
    ) -> Any:
        """Upload through the pool, rotating sessions on FloodWait.

        Retrying on a *different* session is the critical behaviour. Sleeping and
        retrying on the same account is what turns a FloodWait warning into a
        restriction, so the pool's retry helper explicitly excludes the session
        that just failed.
        """

        async def _attempt(session: Any) -> Any:
            return await self.backend.upload_chunk(
                channel_id=channel_id,
                data=payload,
                filename=filename,
                session_label=session.label,
            )

        try:
            stored = await self.pool.execute_with_retry(
                "upload_chunk", _attempt, max_attempts=4, acquire_timeout=90.0
            )
        except SessionUnavailableError as exc:
            raise storage_unavailable(
                "Telegram storage is rate-limiting all available sessions; "
                "retry shortly"
            ) from exc
        except FloodWaitError as exc:
            raise storage_unavailable(
                f"Telegram asked for a {exc.seconds}s wait on all available sessions"
            ) from exc
        except StorageError as exc:
            raise storage_unavailable(str(exc)) from exc

        return stored

    # -- completion -------------------------------------------------------

    async def complete(
        self,
        *,
        upload_id: str,
        owner_id: str,
        declared_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Finalise: verify completeness and integrity, then commit the node.

        Verification is done before the node is marked ready, so a file is never
        visible in listings while it is still missing chunks or while its
        whole-file digest is unproven.
        """
        session = await self.get_session(upload_id, owner_id)

        if session["status"] == "completed":
            node = await self.repo.get_node(session["node_id"], owner_id)
            return node

        if session["status"] in {"aborted", "expired"}:
            raise upload_expired(upload_id)

        present = set(await self.repo.list_chunk_indices(session["node_id"]))
        missing = sorted(set(range(session["total_chunks"])) - present)
        if missing:
            # The extension array lets a client resume immediately without a
            # second round-trip to /missing.
            raise upload_incomplete(missing)

        received_bytes = await self.repo.sum_chunk_plaintext_bytes(session["node_id"])
        if received_bytes != session["size_bytes"]:
            raise size_mismatch(session["size_bytes"], received_bytes)

        # Whole-file digest. In server_managed mode the server can recompute it
        # from the stored chunks; in zero_knowledge mode it cannot, so a declared
        # digest is recorded as a claim rather than verified. Stating this
        # limitation is more honest than pretending verification occurred.
        computed_hex: str | None = None
        if session["encryption_mode"] == "server_managed":
            computed_hex = await self._compute_file_sha256(session)
            expected = declared_sha256 or _as_hex(session.get("expected_sha256"))
            if expected and not crypto.constant_time_equals(
                computed_hex, expected.lower()
            ):
                # A mismatch means the assembled file is not what the client
                # intended — most often a client-side slicing bug. The node stays
                # un-ready so the bad upload cannot be served.
                raise file_hash_mismatch(expected.lower(), computed_hex)

        node = await self.repo.finalize_file_node(
            node_id=session["node_id"],
            owner_id=owner_id,
            sha256_hex=computed_hex or declared_sha256,
            hash_mode=(
                "plaintext_sha256"
                if session["encryption_mode"] == "server_managed"
                else "client_hmac"
            ),
        )
        await self.repo.complete_upload_session(upload_id=upload_id)

        log.info(
            "upload: session %s completed (%d chunks, %d bytes)",
            upload_id,
            session["total_chunks"],
            session["size_bytes"],
        )
        return node

    async def _compute_file_sha256(self, session: dict[str, Any]) -> str:
        """Recompute the whole-file digest by streaming chunks in order.

        Reads back through the normal download path so that the digest proves
        what is *actually retrievable from Telegram*, not merely what was
        accepted. That distinction matters: it catches a chunk that was recorded
        but whose message was rejected or lost.
        """
        hasher = crypto.ChunkHasher()
        node_id = session["node_id"]
        chunks = await self.repo.list_chunks(node_id)
        if not chunks:
            return hasher.hexdigest()

        node_id_bytes = uuid.UUID(str(node_id)).bytes
        total_chunks = session["total_chunks"]
        key = await self._derive_read_key(session, node_id_bytes)

        for chunk in chunks:
            aad = crypto.build_chunk_aad(
                node_id_bytes, chunk["chunk_index"], total_chunks
            )
            plaintext = await self._read_and_open_chunk(
                chunk=chunk, key=key, aad=aad, node_id=node_id
            )
            hasher.update(plaintext)

        return hasher.hexdigest()

    async def _derive_read_key(self, session: dict[str, Any], node_id_bytes: bytes) -> bytes:
        if session["encryption_mode"] == "zero_knowledge":
            # Server-side recomputation is impossible in ZK mode; the caller
            # guards against reaching here.
            return crypto.derive_file_key(
                self.settings.master_kek, node_id_bytes=node_id_bytes
            )
        dek = session.get("_dek")
        if dek is None:
            dek = await self._user_dek(session["owner_id"], node_id_bytes)
            session["_dek"] = dek
        return crypto.derive_file_key(dek, node_id_bytes=node_id_bytes)

    async def _read_and_open_chunk(
        self, *, chunk: dict[str, Any], key: bytes, aad: bytes, node_id: str
    ) -> bytes:
        """Fetch a chunk's ciphertext and authenticate it."""
        ciphertext = await self._fetch_chunk_ciphertext(chunk)
        plain = crypto.open_chunk(
            key,
            ciphertext.ciphertext,
            iv=bytes(chunk["iv"]),
            tag=bytes(chunk["auth_tag"]),
            aad=aad,
        )
        return plain

    async def _fetch_chunk_ciphertext(self, chunk: dict[str, Any]) -> crypto.SealedChunk:
        """Retrieve a chunk's wire bytes and split off the GCM tag."""
        from app.telegram.base import ChunkLocation

        location = ChunkLocation(
            channel_id=chunk["telegram_channel_id"],
            message_id=chunk["telegram_message_id"],
            file_id=chunk.get("telegram_file_id"),
            access_hash=chunk.get("telegram_access_hash"),
            dc_id=chunk.get("telegram_dc_id"),
        )

        async def _attempt(session: Any) -> bytes:
            buf = bytearray()
            async for piece in self.backend.download_range(
                location=location,
                offset=0,
                limit=chunk["ciphertext_size"],
                session_label=session.label,
            ):
                buf.extend(piece)
            return bytes(buf)

        data = await self.pool.execute_with_retry(
            "read_chunk_full", _attempt, max_attempts=3
        )
        ciphertext, tag = crypto.split_wire(data)
        return crypto.SealedChunk(
            ciphertext=ciphertext, iv=bytes(chunk["iv"]), tag=tag
        )

    # -- abort ------------------------------------------------------------

    async def abort(self, *, upload_id: str, owner_id: str) -> None:
        """Abort a session and reclaim everything it held.

        Three resources must be released, and missing any one is a real bug:
        reserved quota, the partially-created node, and any Telegram messages
        already uploaded. The messages are deleted asynchronously because doing
        it inline would make an abort as slow as the upload was.
        """
        session = await self.get_session(upload_id, owner_id)
        if session["status"] in {"completed", "aborted"}:
            return

        await self.repo.abort_upload_session(upload_id=upload_id)
        await self.repo.release_quota(owner_id, session["reserved_bytes"])
        await self.repo.delete_node(session["node_id"], owner_id)
        await self.repo.enqueue_job(
            "gc_orphan_chunks", {"node_id": session["node_id"]}
        )
        log.info("upload: session %s aborted and quota released", upload_id)

    async def _release_node_chunks(self, node_id: str, owner_id: str) -> None:
        """Drop an overwritten node's chunks and schedule their messages for GC."""
        await self.repo.enqueue_job("gc_orphan_chunks", {"node_id": node_id})
        await self.repo.delete_node_chunks(node_id)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _receipt(chunk: dict[str, Any], *, rewound: bool = False) -> dict[str, Any]:
        return {
            "chunk_index": chunk["chunk_index"],
            "plaintext_size": chunk["plaintext_size"],
            "ciphertext_size": chunk["ciphertext_size"],
            "sha256": bytes(chunk["sha256"]).hex(),
            "status": "already_present" if rewound else "stored",
            "telegram_message_id": chunk.get("telegram_message_id"),
        }

    @staticmethod
    def _validate_chunk_size(chunk_size: int) -> None:
        if chunk_size % MIB != 0:
            raise size_mismatch(-1, chunk_size)
        if chunk_size < 1 * MIB or chunk_size > 2 * (1024**3):
            raise size_mismatch(-1, chunk_size)

    # -- streaming multi-part ingestion (optional path) -------------------

    async def ingest_stream(
        self,
        *,
        session: dict[str, Any],
        stream: AsyncIterator[bytes],
        owner_id: str,
    ) -> str:
        """Consume a raw octet stream, slicing and storing chunks as they arrive.

        This is an alternative ingestion path for clients that cannot slice
        locally (a simple upload form, for instance). It is **not** the primary
        path, because it gives up per-chunk resumption: the stream is consumed
        once and a failure mid-way requires restarting from the first missing
        chunk. It does preserve the important property that nothing is written to
        server disk — at most one chunk is held in memory at a time.
        """
        chunk_size = session["chunk_size"]
        buffer = bytearray()
        index = 0
        pending: list[asyncio.Task[Any]] = []

        try:
            async for piece in stream:
                buffer.extend(piece)
                while len(buffer) >= chunk_size:
                    body = bytes(buffer[:chunk_size])
                    del buffer[:chunk_size]
                    pending.append(
                        asyncio.create_task(
                            self._store_sliced_chunk(session, index, body, owner_id)
                        )
                    )
                    index += 1
                    # Bound in-flight work: unbounded parallelism here is exactly
                    # the burst pattern that trips Telegram's rate limits.
                    if len(pending) >= self.settings.max_concurrent_uploads:
                        await asyncio.gather(*pending)
                        pending.clear()

            if buffer or index == 0:
                pending.append(
                    asyncio.create_task(
                        self._store_sliced_chunk(session, index, bytes(buffer), owner_id)
                    )
                )

            if pending:
                await asyncio.gather(*pending)
        except Exception:
            for task in pending:
                task.cancel()
            raise

        return await self.complete_upload_only(session["id"], owner_id)

    async def _store_sliced_chunk(
        self, session: dict[str, Any], index: int, body: bytes, owner_id: str
    ) -> None:
        """Store one server-sliced chunk, deriving the hash locally."""
        await self.put_chunk(
            upload_id=session["id"],
            owner_id=owner_id,
            chunk_index=index,
            body=body,
            declared_sha256=crypto.sha256_hex(body),
        )

    async def complete_upload_only(self, upload_id: str, owner_id: str) -> str:
        node = await self.complete(upload_id=upload_id, owner_id=owner_id)
        return node["id"]


def _as_hex(value: Any) -> str | None:
    """Normalise a stored digest to lowercase hex.

    The two repository implementations legitimately differ: asyncpg returns a
    ``bytea`` column as :class:`bytes`, while the in-memory repository keeps the
    hex string the caller supplied. Normalising in one place keeps the service
    free of storage-specific branching.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    text = str(value)
    return text.lower() if text else None


def stored_session_id(pool: SessionPool) -> str | None:
    """Best-effort session id for the audit column.

    Returns None rather than raising: attribution is useful telemetry but must
    never be able to fail a chunk that was already durably stored on Telegram.
    """
    try:
        for session in pool._sessions.values():  # noqa: SLF001 - intentional, internal
            if session.in_flight > 0 and session.id:
                return session.id
    except Exception:  # noqa: BLE001
        return None
    return None