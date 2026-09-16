"""Telethon-backed MTProto storage.

This is the real storage substrate. It deliberately uses **MTProto** rather than
the HTTP Bot API for three reasons that the architecture depends on:

1. **Range reads.** ``iter_download`` accepts ``offset`` and ``limit``, so a byte
   window can be fetched directly. The Bot API's ``getFile`` returns a whole file
   at a path, which cannot serve an HTTP ``Range`` request without downloading
   everything first.
2. **No artificial size ceiling on the client path.** The Bot API imposes strict
   upload/download limits; MTProto permits documents up to the protocol ceiling,
   which is what makes multi-gigabyte files possible.
3. **Account pool control.** Sessions are per-account, which is what allows
   rotating traffic across accounts when one starts FloodWaiting.

The one protocol detail that shapes the code: ``upload.getFile`` serves data on
**4 KiB-aligned offsets**. A request for an arbitrary offset must round down to
the boundary and discard the prefix. That trimming happens here so callers
receive exactly the window they asked for and never have to know about alignment.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from app.core.config import MTProto_ALIGNMENT, STREAM_READ_CHUNK
from app.telegram.base import (
    ChunkLocation,
    FloodWaitError,
    MessageNotFoundError,
    StorageUnavailableError,
    StoredChunk,
)

log = logging.getLogger(__name__)


class TelethonStore:
    """Concrete :class:`~app.telegram.base.StorageBackend` over Telethon.

    The per-account ``TelegramClient`` objects are owned by the session pool and
    registered here. Keeping clients outside this class is what lets the pool
    decide *which* account carries a request without this layer having any
    opinion about scheduling.
    """

    def __init__(self, *, request_timeout: float = 120.0) -> None:
        self.request_timeout = request_timeout
        self._clients: dict[str, Any] = {}
        self._entity_cache: dict[int, Any] = {}

    # -- client registry --------------------------------------------------

    def register_client(self, session_label: str, client: Any) -> None:
        """Attach a connected Telethon client owned by the session pool."""
        self._clients[session_label] = client

    def unregister_client(self, session_label: str) -> None:
        self._clients.pop(session_label, None)

    async def build_client(
        self,
        session_string: str,
        api_id: int | None = None,
        api_hash: str | None = None,
    ) -> Any:
        """Construct a Telethon client instance from a session string."""
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from app.core.config import get_settings

        settings = get_settings()
        _api_id = api_id or settings.telegram_api_id
        _api_hash = api_hash or settings.telegram_api_hash
        return TelegramClient(
            StringSession(session_string),
            api_id=_api_id,
            api_hash=_api_hash,
            timeout=self.request_timeout,
        )

    def _client_for(self, session_label: str) -> Any:
        client = self._clients.get(session_label)
        if client is None:
            raise StorageUnavailableError(
                f"no connected MTProto client for session {session_label!r}"
            )
        return client

    @staticmethod
    def _client_dc(client: Any) -> int | None:
        try:
            return int(client.session.dc_id)
        except Exception:  # noqa: BLE001
            return None

    # -- helpers ----------------------------------------------------------

    async def _resolve_entity(self, client: Any, channel_id: int) -> Any:
        """Resolve a channel id to a Telethon entity, with a small cache.

        ``get_entity`` costs a round-trip, and the storage pools are a tiny,
        stable set, so caching them removes a request from the hot path for every
        chunk transfer. Without this, a large upload would spend a meaningful
        fraction of its Telegram request budget on lookups — which is exactly the
        budget that triggers FloodWait.
        """
        cached = self._entity_cache.get(channel_id)
        if cached is not None:
            return cached

        try:
            entity = await client.get_entity(channel_id)
        except Exception:
            try:
                from telethon.tl.types import PeerChannel
                clean_id = abs(int(channel_id))
                if str(clean_id).startswith("100"):
                    clean_id = int(str(clean_id)[3:])
                entity = await client.get_entity(PeerChannel(clean_id))
            except Exception as exc:
                raise StorageUnavailableError(
                    f"دسترسی به کانال ذخیره‌سازی {channel_id} ممکن نیست ({exc}). "
                    "لطفاً مطمئن شوید ربات شما در این کانال عضو است و دسترسی ادمین (ارسال پیام) دارد."
                ) from exc

        self._entity_cache[channel_id] = entity
        return entity

    @staticmethod
    def _translate(exc: Exception, *, operation: str, session_label: str) -> Exception:
        """Normalise Telethon exceptions into the storage contract.

        FloodWait in particular must be recognised precisely: it is the one error
        that must never be retried blindly, and it carries the exact number of
        seconds the platform demands.
        """
        name = type(exc).__name__

        if name == "FloodWaitError" or "FloodWait" in name:
            seconds = int(getattr(exc, "seconds", 0) or 0)
            return FloodWaitError(seconds, operation=operation, session_label=session_label)

        if name in {"MessageIdInvalidError", "MessageDeleteForbiddenError"}:
            return MessageNotFoundError(str(exc))

        if name in {"ChannelPrivateError", "ChatForbiddenError", "ChannelInvalidError"}:
            return StorageUnavailableError(
                f"کانال ذخیره‌سازی تلگرام در دسترس نیست ({name}). "
                "لطفاً مطمئن شوید ربات را در این کانال عضو و ادمین کرده‌اید."
            )

        if name in {"ChatAdminRequiredError", "ChatWriteForbiddenError"}:
            return StorageUnavailableError(
                "ربات مجوز ارسال پیام در کانال تلگرام را ندارد. لطفاً در تنظیمات کانال به ربات دسترسی ادمین (ارسال پیام) بدهید."
            )

        if name in {
            "AuthKeyUnregisteredError",
            "UserDeactivatedBanError",
            "SessionRevokedError",
            "AuthKeyDuplicatedError",
        }:
            return StorageUnavailableError(
                f"session {session_label} is no longer authorised ({name}); "
                "quarantine this session and rotate to a healthy account"
            )

        return StorageUnavailableError(f"{operation} failed on {session_label}: {exc}")

    # -- StorageBackend ---------------------------------------------------

    async def upload_chunk(
        self,
        *,
        channel_id: int,
        data: bytes,
        filename: str,
        session_label: str,
    ) -> StoredChunk:
        """Upload one encrypted chunk as a document message."""
        from telethon.tl.types import DocumentAttributeFilename

        client = self._client_for(session_label)

        try:
            entity = await self._resolve_entity(client, channel_id)
            message = await client.send_file(
                entity,
                data,
                # Caption is metadata only and never contains secrets: it exists
                # so an operator can identify orphans by eye during recovery.
                caption=f"td:{filename}",
                attributes=[DocumentAttributeFilename(file_name=filename)],
                force_document=True,   # never let Telegram recompress or strip bytes
                silent=True,           # no notification churn across the pool
                supports_streaming=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(
                exc, operation="upload_chunk", session_label=session_label
            ) from exc

        document = getattr(message, "document", None)
        return StoredChunk(
            channel_id=channel_id,
            message_id=message.id,
            file_id=str(getattr(document, "id", "")) or None,
            access_hash=getattr(document, "access_hash", None),
            dc_id=self._client_dc(client),
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
        """Stream ``limit`` bytes of a stored object starting at ``offset``.

        This is an **async generator** (``async def`` containing ``yield``), so
        callers consume it with ``async for``. Being a generator rather than a
        coroutine returning a buffer is the whole point: it is what lets a slow
        HTTP client apply backpressure all the way back to the MTProto read loop,
        so a stalled download cannot accumulate a chunk in server memory.

        Alignment handling: the MTProto offset is rounded **down** to the 4 KiB
        boundary and the unwanted prefix is discarded before the first yield, so
        the caller's byte math stays exact while the protocol requirement is
        satisfied. The extra bytes read are bounded by 4 KiB, which is negligible
        next to a chunk-sized read.
        """
        client = self._client_for(session_label)

        try:
            entity = await self._resolve_entity(client, location.channel_id)
            message = await client.get_messages(entity, ids=location.message_id)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(
                exc, operation="resolve_message", session_label=session_label
            ) from exc

        if message is None or getattr(message, "document", None) is None:
            raise MessageNotFoundError(
                f"message {location.message_id} in channel {location.channel_id} "
                "has no document (deleted or wrong id)"
            )

        media = message.document
        aligned_offset = (offset // MTProto_ALIGNMENT) * MTProto_ALIGNMENT
        prefix_skip = offset - aligned_offset
        # Read the alignment padding plus the caller's window.
        read_limit = prefix_skip + limit

        skipped = 0
        produced = 0
        try:
            # ``iter_download`` yields at most ``request_size`` per step, which
            # is what keeps memory bounded under a slow reader.
            async for piece in client.iter_download(
                media,
                offset=aligned_offset,
                limit=read_limit,
                request_size=STREAM_READ_CHUNK,
            ):
                if not piece:
                    continue

                if skipped < prefix_skip:
                    drop = min(prefix_skip - skipped, len(piece))
                    skipped += drop
                    piece = piece[drop:]
                    if not piece:
                        continue

                remaining = limit - produced
                if remaining <= 0:
                    break
                if len(piece) > remaining:
                    piece = piece[:remaining]

                produced += len(piece)
                yield piece

                if produced >= limit:
                    break
        except Exception as exc:  # noqa: BLE001
            translated = self._translate(
                exc, operation="download_range", session_label=session_label
            )
            raise translated from exc

    async def delete_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        session_label: str,
    ) -> bool:
        """Delete one stored chunk message.

        Returns True when the message is gone. A missing message counts as
        success: garbage collection must be idempotent, and re-running it after a
        partial failure must not error on already-deleted rows.
        """
        client = self._client_for(session_label)
        try:
            entity = await self._resolve_entity(client, channel_id)
            await client.delete_messages(entity, [message_id])
            return True
        except Exception as exc:  # noqa: BLE001
            translated = self._translate(
                exc, operation="delete_message", session_label=session_label
            )
            if isinstance(translated, MessageNotFoundError):
                return True
            raise translated from exc

    async def probe(self, *, session_label: str) -> bool:
        """Liveness check for ``GET /readyz``.

        ``get_me`` is used because it is cheap and proves the session is both
        connected and still authorised — a session string can remain
        syntactically valid while the account has been revoked.
        """
        try:
            client = self._client_for(session_label)
            await client.get_me()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("mtproto probe failed on %s: %s", session_label, exc)
            return False