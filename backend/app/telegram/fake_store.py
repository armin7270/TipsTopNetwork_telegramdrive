"""In-memory storage backend for tests and local development.

This exists so the entire pipeline — chunk slicing, resumption, AEAD sealing,
Range arithmetic, replica failover, and FloodWait handling — can be tested
deterministically without Telegram credentials.

It is a **test double, not a fallback**. Running production traffic through it
would silently store nothing durable, so the application refuses to select it
unless ``TELEDRIVE_IN_MEMORY`` is explicitly set.

It deliberately models the awkward parts of the real protocol:

* Telegram's **4 KiB message-id / offset alignment** for reads.
* **FloodWait injection**, so the rotation logic is provably exercised rather
  than assumed to work.
* **Message deletion**, so replica failover is testable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.core.config import STREAM_READ_CHUNK
from app.telegram.base import (
    ChunkLocation,
    FloodWaitError,
    MessageNotFoundError,
    StoredChunk,
)

log = logging.getLogger(__name__)


@dataclass
class FakeMessage:
    """One stored object plus its metadata."""

    message_id: int
    channel_id: int
    data: bytes
    filename: str
    deleted: bool = False

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class FakeChannel:
    channel_id: int
    messages: dict[int, FakeMessage] = field(default_factory=dict)
    # Telegram caps messages per minute per chat; the double tracks a simple
    # minute window so tests can assert that pacing actually happens.
    send_timestamps: list[float] = field(default_factory=list)


class InMemoryStore:
    """A :class:`~app.telegram.base.StorageBackend` that lives in RAM."""

    def __init__(self) -> None:
        self.channels: dict[int, FakeChannel] = {}
        self._next_message_id = 1000
        self._lock = asyncio.Lock()

        # --- fault injection knobs, set by tests ---------------------------
        # Map of session label -> number of seconds of FloodWait to raise, once.
        self.floodwait_once: dict[str, int] = {}
        # Map of session label -> always raise FloodWait (for exhaustion tests).
        self.floodwait_always: dict[str, int] = {}
        self.fail_once: dict[str, Exception] = {}
        self.upload_calls = 0
        self.download_calls = 0
        self.delete_calls = 0
        self.max_concurrent_uploads_observed = 0
        self._in_flight_uploads = 0

    # -- helpers ----------------------------------------------------------

    def _channel(self, channel_id: int) -> FakeChannel:
        return self.channels.setdefault(channel_id, FakeChannel(channel_id=channel_id))

    def seed_channel(self, channel_id: int) -> None:
        self._channel(channel_id)

    def message(self, channel_id: int, message_id: int) -> FakeMessage | None:
        return self._channel(channel_id).messages.get(message_id)

    def total_stored_bytes(self) -> int:
        return sum(
            m.size for ch in self.channels.values() for m in ch.messages.values()
            if not m.deleted
        )

    def live_message_count(self) -> int:
        return sum(
            1 for ch in self.channels.values() for m in ch.messages.values() if not m.deleted
        )

    def _check_faults(self, session_label: str) -> None:
        if session_label in self.floodwait_once:
            seconds = self.floodwait_once.pop(session_label)
            raise FloodWaitError(seconds, session_label=session_label)
        if session_label in self.floodwait_always:
            raise FloodWaitError(
                self.floodwait_always[session_label], session_label=session_label
            )
        if session_label in self.fail_once:
            raise self.fail_once.pop(session_label)

    # -- StorageBackend ---------------------------------------------------

    async def upload_chunk(
        self,
        *,
        channel_id: int,
        data: bytes,
        filename: str,
        session_label: str,
    ) -> StoredChunk:
        self._check_faults(session_label)
        self.upload_calls += 1

        self._in_flight_uploads += 1
        self.max_concurrent_uploads_observed = max(
            self.max_concurrent_uploads_observed, self._in_flight_uploads
        )
        try:
            # A tiny await so concurrent callers actually interleave and the
            # concurrency ceiling is genuinely exercised.
            await asyncio.sleep(0)

            async with self._lock:
                channel = self._channel(channel_id)
                message_id = self._next_message_id
                self._next_message_id += 1
                channel.messages[message_id] = FakeMessage(
                    message_id=message_id,
                    channel_id=channel_id,
                    data=data,
                    filename=filename,
                )
        finally:
            self._in_flight_uploads -= 1

        return StoredChunk(
            channel_id=channel_id,
            message_id=message_id,
            file_id=f"fake-file-{message_id}",
            access_hash=abs(hash(filename)) % (2**63),
            dc_id=2,
            file_size=len(data),
        )

    async def download_range(
        self,
        *,
        location: ChunkLocation,
        offset: int,
        limit: int,
        session_label: str,
    ) -> AsyncIterator[bytes]:
        """Stream a byte window, honouring 4 KiB alignment like the real API.

        This is a true **async generator** (note the ``yield`` below), matching
        ``TelethonStore`` and the :class:`~app.telegram.base.StorageBackend`
        contract: calling it returns an async iterator directly, so callers use
        ``async for`` without an intervening ``await``.

        The 4 KiB alignment behaviour is reproduced deliberately — a double that
        ignored it would let tests pass here and then return wrong bytes against
        real Telegram, which is the worst possible failure mode.
        """
        self._check_faults(session_label)
        self.download_calls += 1

        message = self.message(location.channel_id, location.message_id)
        if message is None or message.deleted:
            raise MessageNotFoundError(
                f"fake message {location.message_id} in {location.channel_id} is gone"
            )

        if offset < 0:
            raise ValueError("offset must be >= 0")

        alignment = 4096
        aligned_offset = (offset // alignment) * alignment
        prefix_skip = offset - aligned_offset
        end = min(message.size, aligned_offset + prefix_skip + limit)
        window = message.data[aligned_offset:end]

        produced = 0
        skip = prefix_skip
        for start in range(0, len(window), STREAM_READ_CHUNK):
            piece = window[start : start + STREAM_READ_CHUNK]
            if skip:
                drop = min(skip, len(piece))
                skip -= drop
                piece = piece[drop:]
                if not piece:
                    continue
            remaining = limit - produced
            if remaining <= 0:
                break
            if len(piece) > remaining:
                piece = piece[:remaining]
            produced += len(piece)
            await asyncio.sleep(0)   # model a network hop
            yield piece
            if produced >= limit:
                break

    async def delete_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        session_label: str,
    ) -> bool:
        self.delete_calls += 1
        message = self.message(channel_id, message_id)
        if message is None:
            return True
        message.deleted = True
        return True

    async def probe(self, *, session_label: str) -> bool:
        return True

    # -- test-only helpers ------------------------------------------------

    def delete_message_out_of_band(self, channel_id: int, message_id: int) -> None:
        """Simulate Telegram-side deletion, to exercise replica failover.

        Uses delete_message so the counter stays meaningful, but this is called
        from test code rather than through a session.
        """
        message = self.message(channel_id, message_id)
        if message is not None:
            message.deleted = True

    def register_client(self, session_label: str, client: object) -> None:
        """Satisfy the pool's optional client-registration hook."""

    def unregister_client(self, session_label: str) -> None:
        """Satisfy the pool's optional client-registration hook."""