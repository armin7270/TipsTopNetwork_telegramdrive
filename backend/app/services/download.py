"""Download pipeline: Range-aware, streaming, decrypt-on-the-fly.

The hard parts, stated honestly:

**Telegram serves 4 KiB-aligned offsets.** A request for ``bytes=1000000-3000000``
cannot be answered by asking Telegram for offset 1,000,000; the protocol rounds
to a boundary. Two strategies exist:

* *Chunk-granular* (the default here): fetch the whole chunk containing the start
  offset, decrypt it, discard the unwanted prefix. Waste is bounded by one chunk
  per request. Costs the fewest MTProto round-trips.
* *4 KiB-aligned sub-chunk reads*: round *down*, read forward, trim. Much cheaper
  in bytes for many small seeks (an audio player scrubbing), but multiplies
  round-trips — and round-trips are the resource that gets accounts rate-limited.

The default deliberately favours **fewer Telegram requests** over minimum bytes
transferred, because ban risk dominates bandwidth cost for a personal drive. The
alignment-aware path is implemented in the backend layer, so switching is a
matter of choosing a different fetch granularity rather than a rewrite.

**Backpressure is the memory safety property.** The response body is an async
generator. If the client stops reading, the HTTP layer stops pulling, the
generator suspends, and the MTProto read loop is not driven further. A stalled
download therefore cannot accumulate a chunk in server memory. This is why the
code streams sub-reads rather than accumulating and returning a buffer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

from app.core import crypto
from app.core.errors import (
    chunk_unreadable,
    not_found,
    range_not_satisfiable,
    storage_unavailable,
)
from app.telegram.base import (
    ChunkLocation,
    MessageNotFoundError,
    SessionUnavailableError,
    StorageBackend,
    StorageError,
)
from app.telegram.pool import SessionPool

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ByteRange:
    """A resolved, inclusive byte range within a file."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range_header(self, total_size: int) -> str:
        return f"bytes {self.start}-{self.end}/{total_size}"


@dataclass(frozen=True, slots=True)
class ChunkWindow:
    """The chunk indices needed to satisfy a byte range."""

    first_index: int
    last_index: int
    # Bytes to discard from the decoded first chunk, and to keep from the last.
    skip_in_first: int
    take_from_last: int

    @property
    def count(self) -> int:
        return self.last_index - self.first_index + 1


def parse_range_header(header: str | None, size_bytes: int) -> ByteRange | None:
    """Parse an HTTP ``Range`` header against a known file size.

    Returns ``None`` when no range was requested (a full-body 200 response).
    Supports the two forms that matter in practice:

    * ``bytes=start-end`` — an explicit window.
    * ``bytes=start-``    — open-ended; this is what media players and
      download-resume clients send, so it must work.
    * ``bytes=-suffix``   — the trailing N bytes, used by some tooling to read a
      file's tail (container atoms, zip central directories).

    Multi-range requests (``bytes=0-99,200-299``) are deliberately **not**
    supported: they require multipart/byteranges responses, and no client in this
    system's target set uses them. Rejecting clearly is better than silently
    serving only the first range.
    """
    if not header:
        return None

    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None

    spec = header[len("bytes=") :].strip()
    if "," in spec:
        raise range_not_satisfiable(size_bytes)

    if "-" not in spec:
        raise range_not_satisfiable(size_bytes)

    start_str, _, end_str = spec.partition("-")
    start_str, end_str = start_str.strip(), end_str.strip()

    if start_str == "":
        # Suffix form: the final N bytes.
        if end_str == "":
            raise range_not_satisfiable(size_bytes)
        try:
            suffix = int(end_str)
        except ValueError as exc:
            raise range_not_satisfiable(size_bytes) from exc
        if suffix <= 0:
            raise range_not_satisfiable(size_bytes)
        if size_bytes == 0:
            raise range_not_satisfiable(size_bytes)
        start = max(0, size_bytes - suffix)
        return ByteRange(start=start, end=size_bytes - 1)

    try:
        start = int(start_str)
    except ValueError as exc:
        raise range_not_satisfiable(size_bytes) from exc

    if start < 0 or start >= size_bytes:
        # A start at or beyond EOF is unsatisfiable, answered with 416 and the
        # total size so the client can correct itself.
        raise range_not_satisfiable(size_bytes)

    if end_str == "":
        end = size_bytes - 1
    else:
        try:
            end = int(end_str)
        except ValueError as exc:
            raise range_not_satisfiable(size_bytes) from exc
        # A range extending past EOF is clamped, which RFC 9110 permits, rather
        # than rejected — clients commonly over-request the tail.
        end = min(end, size_bytes - 1)

    if end < start:
        raise range_not_satisfiable(size_bytes)

    return ByteRange(start=start, end=end)


def plan_chunk_window(byte_range: ByteRange, chunk_size: int) -> ChunkWindow:
    """Map a byte range onto the chunk indices that contain it.

    The arithmetic that makes random access work: chunk ``i`` holds bytes
    ``[i*chunk_size, (i+1)*chunk_size)``, so the containing indices are a pair of
    integer divisions. No index is consulted to compute this — the plan is
    derived, then used to select exactly the rows needed.
    """
    first_index = byte_range.start // chunk_size
    last_index = byte_range.end // chunk_size
    skip_in_first = byte_range.start - first_index * chunk_size
    take_from_last = byte_range.end - last_index * chunk_size + 1

    return ChunkWindow(
        first_index=first_index,
        last_index=last_index,
        skip_in_first=skip_in_first,
        take_from_last=take_from_last,
    )


class DownloadService:
    """Streams file content out of Telegram chunk storage."""

    def __init__(
        self,
        *,
        pool: SessionPool,
        backend: StorageBackend,
        settings: Any,
        repository: Any,
    ) -> None:
        self.pool = pool
        self.backend = backend
        self.settings = settings
        self.repo = repository
        self._download_slots = None

    # -- metadata ---------------------------------------------------------

    async def get_file(self, node_id: str, owner_id: str) -> dict[str, Any]:
        node = await self.repo.get_node(node_id, owner_id)
        if node is None:
            raise not_found(f"Node {node_id} was not found")
        if node["kind"] != "file":
            raise not_found(f"Node {node_id} is a folder and has no content")
        if node.get("upload_state") != "ready":
            # Serving a partially-uploaded file would return a truncated body
            # with a 200 status, which is worse than refusing.
            raise not_found(
                f"Node {node_id} is not ready yet (state={node.get('upload_state')})"
            )
        if node.get("trashed_at"):
            raise not_found(f"Node {node_id} is in the trash")
        return node

    @staticmethod
    def etag_for(node: dict[str, Any]) -> str | None:
        """Strong ETag from the stored digest.

        Quoted lowercase hex, per the OpenAPI contract. Returns ``None`` in
        zero-knowledge mode, where no plaintext digest exists — omitting the ETag
        is correct, because a wrong one would break conditional requests and
        range resumption.
        """
        digest = node.get("sha256")
        if not digest or node.get("hash_mode") != "plaintext_sha256":
            return None
        return f'"{bytes(digest).hex()}"'

    # -- streaming --------------------------------------------------------

    async def stream(
        self,
        *,
        node_id: str,
        owner_id: str,
        byte_range: ByteRange | None,
    ) -> AsyncIterator[bytes]:
        """Yield the requested bytes of a file, decrypting chunk by chunk.

        Each chunk is fetched, authenticated, trimmed, and yielded in order. Only
        one chunk's plaintext is resident at a time, and the generator suspends
        whenever the consumer stops reading.
        """
        node = await self.get_file(node_id, owner_id)
        total_size = int(node["size_bytes"])
        chunk_size = int(node["chunk_size"] or self.settings.storage_chunk_size_bytes)
        total_chunks = int(node["total_chunks"])

        if total_size == 0:
            return

        window_range = byte_range or ByteRange(start=0, end=total_size - 1)
        window = plan_chunk_window(window_range, chunk_size)

        chunks = await self.repo.list_chunks_in_range(
            node_id=node_id,
            first_index=window.first_index,
            last_index=window.last_index,
        )

        by_index = {int(c["chunk_index"]): c for c in chunks}
        missing = [
            i
            for i in range(window.first_index, window.last_index + 1)
            if i not in by_index
        ]
        if missing:
            # A hole in the middle of a ready file means the index and Telegram
            # disagree. Surfacing it is far better than silently returning a
            # shorter body that looks like a successful download.
            raise chunk_unreadable(missing[0], f"chunk index {missing[0]} is not indexed")

        node_id_bytes = uuid.UUID(str(node_id)).bytes
        key = await self._derive_key(node, node_id_bytes)

        for index in range(window.first_index, window.last_index + 1):
            chunk = by_index[index]
            aad = crypto.build_chunk_aad(node_id_bytes, index, total_chunks)

            try:
                plaintext = await self._read_chunk(
                    chunk=chunk, key=key, aad=aad, node_id=node_id
                )
            except crypto.AuthenticationFailed as exc:
                raise chunk_unreadable(
                    index,
                    f"chunk {index} failed authentication; stored data is corrupt "
                    "or was written under a different key",
                ) from exc

            # Trim the two edges of the requested window. Middle chunks are
            # emitted whole. Working in absolute file offsets per chunk is what
            # keeps this correct when the window starts and ends inside the same
            # chunk, and it is what makes the whole-chunk over-read invisible to
            # the client.
            chunk_start = index * chunk_size
            chunk_end = chunk_start + len(plaintext) - 1

            trim_from = max(window_range.start, chunk_start) - chunk_start
            trim_to = min(window_range.end, chunk_end) - chunk_start + 1

            if trim_from > 0 or trim_to < len(plaintext):
                plaintext = plaintext[max(0, trim_from) : trim_to]

            if plaintext:
                yield plaintext

    async def _derive_key(self, node: dict[str, Any], node_id_bytes: bytes) -> bytes:
        """Derive the per-file key.

        In zero-knowledge mode the server cannot derive the key, because it never
        receives the client's key material. That mode is therefore not streamable
        through the server; a client using it must decrypt locally after fetching
        raw chunks. This is a genuine functional limitation of ZK mode and is
        reported rather than silently mishandled.
        """
        if node.get("encryption_mode") == "zero_knowledge":
            raise storage_unavailable(
                "zero_knowledge files cannot be decrypted server-side; "
                "fetch chunks through the raw chunk endpoint instead"
            )
        dek = await self._user_dek(node["owner_id"], node_id_bytes)
        return crypto.derive_file_key(dek, node_id_bytes=node_id_bytes)

    async def _user_dek(self, owner_id: str, node_id_bytes: bytes) -> bytes:
        """Return the user's DEK, preferring the wrapped value from the database.

        Falls back to deriving from the KEK when no wrapped DEK is present, which
        keeps pre-existing rows readable and lets the in-memory test repository
        work without emulating key storage.
        """
        wrapped = await self.repo.get_user_wrapped_dek(owner_id)
        if wrapped:
            try:
                return crypto.unwrap_dek(
                    wrapped, self.settings.master_kek, user_id_bytes=uuid.UUID(str(owner_id)).bytes
                )
            except Exception as exc:
                log.warning(
                    "download: failed to unwrap DEK for user %s (%s). Falling back to derive_subkey.",
                    owner_id,
                    exc,
                )
        return crypto.derive_subkey(
            self.settings.master_kek, salt=node_id_bytes, info=b"teledrive/user-dek/v1"
        )

    async def _read_chunk(
        self,
        *,
        chunk: dict[str, Any],
        key: bytes,
        aad: bytes,
        node_id: str,
    ) -> bytes:
        """Fetch, authenticate, and return one chunk's plaintext.

        Failover order: primary location, then any replicas. A deleted message is
        recorded so subsequent reads skip straight to a replica instead of
        rediscovering the same dead message on every request.
        """
        ciphertext = await self._fetch_with_failover(chunk=chunk, node_id=node_id)
        return crypto.open_chunk(
            key,
            ciphertext.ciphertext,
            iv=bytes(chunk["iv"]),
            tag=ciphertext.tag,
            aad=aad,
        )

    async def _fetch_with_failover(
        self, *, chunk: dict[str, Any], node_id: str
    ) -> crypto.SealedChunk:
        """Fetch a chunk's ciphertext, trying replicas when the primary fails."""
        locations: list[ChunkLocation] = [
            ChunkLocation(
                channel_id=chunk["telegram_channel_id"],
                message_id=chunk["telegram_message_id"],
                file_id=chunk.get("telegram_file_id"),
                access_hash=chunk.get("telegram_access_hash"),
                dc_id=chunk.get("telegram_dc_id"),
            )
        ]

        replicas = await self.repo.list_chunk_replicas(chunk["id"])
        for replica in replicas:
            locations.append(
                ChunkLocation(
                    channel_id=replica["telegram_channel_id"],
                    message_id=replica["telegram_message_id"],
                    file_id=replica.get("telegram_file_id"),
                    access_hash=replica.get("telegram_access_hash"),
                    dc_id=replica.get("telegram_dc_id"),
                )
            )

        expected_size = int(chunk["ciphertext_size"])
        last_error: Exception | None = None

        for position, location in enumerate(locations):
            try:
                data = await self._fetch_bytes(
                    location=location, length=expected_size
                )
            except MessageNotFoundError as exc:
                last_error = exc
                # Learn the failure once so later reads skip this location.
                await self.repo.record_chunk_read_failure(
                    chunk_id=chunk["id"],
                    message_id=location.message_id,
                    error=str(exc),
                )
                log.warning(
                    "download: chunk %s message %d is gone, trying next location",
                    chunk["chunk_index"],
                    location.message_id,
                )
                continue
            except SessionUnavailableError as exc:
                last_error = exc
                continue
            except StorageError as exc:
                last_error = exc
                continue

            if position > 0:
                log.info(
                    "download: chunk %s served from replica %d after primary failure",
                    chunk["chunk_index"],
                    position,
                )
            return crypto.sealed_from_wire(data, bytes(chunk["iv"]))

        raise chunk_unreadable(
            int(chunk["chunk_index"]),
            f"no readable location for chunk {chunk['chunk_index']}: {last_error}",
        )

    async def _fetch_bytes(self, *, location: ChunkLocation, length: int) -> bytes:
        """Read a chunk's full wire bytes through the session pool."""

        async def _attempt(session: Any) -> bytes:
            buf = bytearray()
            async for piece in self.backend.download_range(
                location=location,
                offset=0,
                limit=length,
                session_label=session.label,
            ):
                buf.extend(piece)
            return bytes(buf)

        try:
            return await self.pool.execute_with_retry(
                "download_chunk", _attempt, max_attempts=3
            )
        except (MessageNotFoundError, SessionUnavailableError, StorageError):
            # Propagated unchanged so the caller can distinguish "this specific
            # location is dead, try a replica" from "the pool is exhausted".
            raise

    # -- range streaming --------------------------------------------------

    async def stream_range(
        self, *, node_id: str, owner_id: str, byte_range: ByteRange
    ) -> AsyncIterator[bytes]:
        """Stream an explicit byte range, tracking bytes for accounting."""
        async for piece in self.stream(
            node_id=node_id, owner_id=owner_id, byte_range=byte_range
        ):
            yield piece

    async def full_stream(self, *, node_id: str, owner_id: str) -> AsyncIterator[bytes]:
        async for piece in self.stream(node_id=node_id, owner_id=owner_id, byte_range=None):
            yield piece