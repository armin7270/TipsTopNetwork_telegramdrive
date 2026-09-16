"""Storage substrate abstraction.

The rest of the application must not know whether chunks live in a Telegram
channel or in a test double. This module defines the narrow contract that the
upload and download services depend on.

Why an abstraction at all, rather than calling Telethon directly from the
services:

* **Testability.** The entire upload/download pipeline — chunking, resumption,
  Range math, AEAD, failover — is exercised in the test suite against an
  in-memory backend. Without this seam, every test would need live Telegram
  credentials, which is neither safe nor reproducible.
* **FloodWait normalisation.** Telethon, Pyrogram, and a future TDLib bridge all
  express rate limiting differently. Translating them into one exception with a
  ``seconds`` attribute is what lets the session pool treat FloodWait as a
  scheduling signal instead of a generic error.
* **Backend portability.** Pyrogram offers a comparable ``iter_download``; a
  TDLib bridge would expose the same three operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


class StorageError(RuntimeError):
    """Base class for storage-substrate failures."""


class FloodWaitError(StorageError):
    """Telegram asked us to wait.

    This is deliberately **not** a generic error. It is the platform's
    backpressure signal and the single most important input to the session pool:
    it gates the offending session, trips its circuit breaker, and causes traffic
    to rotate to a different account.

    ``seconds`` is Telegram's instruction and must be honoured exactly. Waiting
    less than instructed escalates — the penalty typically grows, and repeated
    escalation is the documented path to an account ban.
    """

    def __init__(self, seconds: int, *, operation: str = "", session_label: str = "") -> None:
        super().__init__(
            f"FloodWait {seconds}s during {operation or 'operation'}"
            + (f" on session {session_label}" if session_label else "")
        )
        self.seconds = int(seconds)
        self.operation = operation
        self.session_label = session_label


class PermanentStorageError(StorageError):
    """A failure that will never succeed on retry.

    Raising this stops session rotation immediately and does not penalise the
    session, because the fault lies with the stored object or the request rather
    than with the account.
    """


class MessageNotFoundError(PermanentStorageError):
    """The referenced Telegram message no longer exists.

    Causes: out-of-band deletion, channel purge, or a revoked account that
    uploaded it. Treated as a failover trigger to a replica, never as a fatal
    error on its own.

    This is **permanent** as far as this location is concerned, and it says
    nothing about the health of the session that reported it. It therefore must
    not count against the sending account's circuit breaker — otherwise a
    deleted message would quarantine perfectly healthy accounts.
    """


class SessionUnavailableError(StorageError):
    """No healthy session could be acquired within the deadline."""


class StorageUnavailableError(StorageError):
    """The substrate failed for a reason not attributable to rate limiting."""


@dataclass(frozen=True, slots=True)
class StoredChunk:
    """Where a chunk physically landed.

    ``file_id``/``access_hash``/``dc_id`` are recorded because they allow a
    future fetch to skip a message lookup, and because they make it possible to
    migrate a chunk between accounts without re-uploading the plaintext.
    """

    channel_id: int
    message_id: int
    file_id: str | None = None
    access_hash: int | None = None
    dc_id: int | None = None
    file_size: int = 0


@dataclass(frozen=True, slots=True)
class ChunkLocation:
    """A read target: everything needed to fetch bytes back."""

    channel_id: int
    message_id: int
    file_id: str | None = None
    access_hash: int | None = None
    dc_id: int | None = None


@runtime_checkable
class StorageBackend(Protocol):
    """The three operations the application actually needs."""

    async def upload_chunk(
        self,
        *,
        channel_id: int,
        data: bytes,
        filename: str,
        session_label: str,
    ) -> StoredChunk:
        """Store ``data`` as one document message in ``channel_id``.

        ``data`` is already encrypted; this layer never sees plaintext.
        """
        ...

    def download_range(
        self,
        *,
        location: ChunkLocation,
        offset: int,
        limit: int,
        session_label: str,
    ) -> AsyncIterator[bytes]:
        """Yield bytes of a stored object starting at ``offset``.

        Implementations must stream: yielding as MTProto delivers data is what
        allows a slow client to apply backpressure all the way to the network
        read, instead of the server accumulating a whole chunk in memory.

        ``offset`` may be subject to protocol alignment (Telegram serves 4 KiB
        boundaries). Implementations that must round down are responsible for
        trimming before yielding, so callers always receive the exact requested
        window.
        """
        ...

    async def delete_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        session_label: str,
    ) -> bool:
        """Delete a message. Returns True when it is gone (idempotent)."""
        ...

    async def probe(self, *, session_label: str) -> bool:
        """Cheap liveness check used by ``GET /readyz``."""
        ...