from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.errors import forbidden, not_found, invalid_argument
from app.services.auth import Principal, current_user
from app.telegram.telethon_store import TelethonStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])

# Official public Telegram client credentials as reliable defaults
DEFAULT_API_ID = 6
DEFAULT_API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"

# In-memory storage for pending 2FA / code logins
_PENDING_LOGINS: dict[str, dict[str, Any]] = {}

class SendCodeRequest(BaseModel):
    api_id: int | None = Field(default=None, description="Telegram API ID from my.telegram.org")
    api_hash: str | None = Field(default=None, description="Telegram API Hash from my.telegram.org")
    phone: str = Field(..., min_length=5, description="Phone number with country code, e.g. +1234567890")

class VerifyCodeRequest(BaseModel):
    phone_code_hash: str = Field(..., min_length=5)
    code: str = Field(..., min_length=3)
    password: str | None = Field(default=None, description="2FA Cloud Password if enabled")

class BotLoginRequest(BaseModel):
    api_id: int | None = Field(default=None)
    api_hash: str | None = Field(default=None)
    bot_token: str = Field(..., min_length=10)

class StringSessionLoginRequest(BaseModel):
    api_id: int | None = Field(default=None)
    api_hash: str | None = Field(default=None)
    session_string: str = Field(..., min_length=20)

class SetupChannelRequest(BaseModel):
    channel_id: int | None = None
    channel_title: str | None = "TeleDrive Storage"
    auto_create: bool = False


def _update_env_file(key: str, value: str) -> None:
    """Helper to persist configuration keys in .env for subsequent process boots."""
    env_paths = [Path(".env"), Path("../.env"), Path("backend/.env")]
    for p in env_paths:
        if p.exists():
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
                found = False
                new_lines = []
                for line in lines:
                    if line.startswith(f"{key}="):
                        new_lines.append(f"{key}={value}")
                        found = True
                    else:
                        new_lines.append(line)
                if not found:
                    new_lines.append(f"{key}={value}")
                p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            except Exception as e:
                log.warning("could not update env file %s: %s", p, e)


@router.get("/config", summary="Get Telegram connection and storage status")
async def get_telegram_config(request: Request) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    repo = getattr(request.app.state, "repo", None)
    settings: Settings = request.app.state.settings

    storage_pools = []
    if repo and hasattr(repo, "list_storage_pools"):
        try:
            storage_pools = await repo.list_storage_pools()
        except Exception:
            pass

    sessions_info = []
    if pool:
        snap = pool.snapshot()
        sessions_info = snap.get("sessions", [])

    backend = getattr(request.app.state, "backend", None)
    has_real_sessions = any(not s.get("id", "").startswith("fake-") for s in sessions_info)
    is_real = isinstance(backend, TelethonStore) or has_real_sessions

    return {
        "is_real_telegram": is_real,
        "api_id": settings.telegram_api_id if settings.telegram_api_id else None,
        "has_api_hash": bool(settings.telegram_api_hash),
        "sessions_count": len(sessions_info),
        "sessions": sessions_info,
        "storage_pools": [
            {
                "id": str(p["id"]),
                "label": p["label"],
                "channel_id": int(p["telegram_channel_id"]),
                "stored_bytes": int(p.get("stored_bytes") or 0),
            }
            for p in storage_pools
        ],
    }


@router.post("/auth/send-code", summary="Request login code from Telegram")
async def send_login_code(request: Request, payload: SendCodeRequest) -> dict[str, Any]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    settings: Settings = request.app.state.settings
    api_id = payload.api_id or settings.telegram_api_id or DEFAULT_API_ID
    api_hash = payload.api_hash or settings.telegram_api_hash or DEFAULT_API_HASH

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        result = await client.send_code_request(payload.phone)
        phone_code_hash = result.phone_code_hash

        _PENDING_LOGINS[phone_code_hash] = {
            "client": client,
            "phone": payload.phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "time": time.time(),
        }

        # Persist api_id and api_hash
        _update_env_file("TELEGRAM_API_ID", str(api_id))
        _update_env_file("TELEGRAM_API_HASH", api_hash)

        return {
            "status": "code_sent",
            "phone_code_hash": phone_code_hash,
            "timeout": getattr(result, "timeout", 60),
        }
    except Exception as exc:
        await client.disconnect()
        log.exception("send_code_request failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram Error: {exc}",
        )


@router.post("/auth/verify-code", summary="Verify Telegram login code and save session")
async def verify_login_code(request: Request, payload: VerifyCodeRequest) -> dict[str, Any]:
    from telethon.errors import SessionPasswordNeededError

    pending = _PENDING_LOGINS.get(payload.phone_code_hash)
    if not pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pending login expired or not found. Please request a new code.",
        )

    client = pending["client"]
    phone = pending["phone"]
    api_id = pending["api_id"]
    api_hash = pending["api_hash"]

    try:
        try:
            await client.sign_in(phone=phone, code=payload.code, phone_code_hash=payload.phone_code_hash)
        except SessionPasswordNeededError:
            if not payload.password:
                return {"status": "2fa_required", "message": "Two-step verification password is required"}
            await client.sign_in(password=payload.password)

        me = await client.get_me()
        session_string = client.session.save()

        # Successfully signed in, clean pending
        _PENDING_LOGINS.pop(payload.phone_code_hash, None)

        # Enroll session into pool
        await _enroll_live_session(request, client=client, session_string=session_string, label=f"sess-{me.id}")

        return {
            "status": "success",
            "user_id": me.id,
            "first_name": me.first_name,
            "username": me.username,
            "phone": me.phone,
            "session_string": session_string,
        }

    except Exception as exc:
        log.exception("verify_login_code failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram Sign In Error: {exc}",
        )


@router.post("/auth/bot-token", summary="Connect using a Telegram Bot Token")
async def connect_bot_token(request: Request, payload: BotLoginRequest) -> dict[str, Any]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    settings: Settings = request.app.state.settings
    api_id = payload.api_id or settings.telegram_api_id or DEFAULT_API_ID
    if api_id == 5:
        api_id = 6  # User typo correction (6 is official Android client ID)
    api_hash = payload.api_hash or settings.telegram_api_hash or DEFAULT_API_HASH

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(bot_token=payload.bot_token)
        me = await client.get_me()
        session_string = client.session.save()

        _update_env_file("TELEGRAM_API_ID", str(api_id))
        _update_env_file("TELEGRAM_API_HASH", api_hash)
        _update_env_file("TELEGRAM_BOT_TOKEN", payload.bot_token)
        os.environ["TELEGRAM_BOT_TOKEN"] = payload.bot_token

        bot_service = getattr(request.app.state, "bot_service", None)
        if bot_service:
            try:
                await bot_service.restart(bot_token=payload.bot_token)
            except Exception as bot_err:
                log.warning("Could not restart bot service after token connect: %s", bot_err)

        await _enroll_live_session(request, client=client, session_string=session_string, label=f"bot-{me.id}")

        return {
            "status": "success",
            "bot_id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "session_string": session_string,
        }
    except Exception as exc:
        await client.disconnect()
        log.exception("bot login failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram Bot Connection Error: {exc}",
        )


@router.post("/auth/session-string", summary="Connect directly with an existing Telethon StringSession")
async def connect_string_session(request: Request, payload: StringSessionLoginRequest) -> dict[str, Any]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    settings: Settings = request.app.state.settings
    api_id = payload.api_id or settings.telegram_api_id or DEFAULT_API_ID
    api_hash = payload.api_hash or settings.telegram_api_hash or DEFAULT_API_HASH

    client = TelegramClient(StringSession(payload.session_string), api_id, api_hash)
    try:
        await client.connect()
        me = await client.get_me()
        if not me:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session string or not authorized")

        _update_env_file("TELEGRAM_API_ID", str(api_id))
        _update_env_file("TELEGRAM_API_HASH", api_hash)

        await _enroll_live_session(request, client=client, session_string=payload.session_string, label=f"user-{me.id}")

        return {
            "status": "success",
            "user_id": me.id,
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", "Telegram User"),
        }
    except Exception as exc:
        await client.disconnect()
        log.exception("connect_string_session failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"StringSession Connection Error: {exc}",
        )


@router.post("/channels/setup", summary="Set or auto-create a private Telegram storage channel")
async def setup_storage_channel(request: Request, payload: SetupChannelRequest) -> dict[str, Any]:
    from telethon.tl.functions.channels import CreateChannelRequest

    pool = getattr(request.app.state, "pool", None)
    repo = getattr(request.app.state, "repo", None)

    if not pool or pool.healthy_count() == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active Telegram session found. Please connect your Telegram account or bot first.",
        )

    channel_id = payload.channel_id

    # If auto-create is requested, pick a healthy session and create a private channel
    if payload.auto_create or not channel_id:
        lease = await pool.acquire(timeout=10.0)
        client = lease.session.client
        try:
            title = payload.channel_title or "TeleDrive Storage"
            result = await client(CreateChannelRequest(
                title=title,
                about="TeleDrive Encrypted Virtual File System Storage Pool",
                megagroup=False
            ))
            created_channel = result.chats[0]
            # Telegram channel IDs for supergroups/channels start with -100
            raw_id = created_channel.id
            channel_id = -int(f"100{raw_id}") if not str(raw_id).startswith("-100") else int(raw_id)
            log.info("Auto-created private storage channel: %s (id: %s)", title, channel_id)
        except Exception as exc:
            log.exception("Failed to auto-create channel")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create storage channel: {exc}. You can create a private channel in Telegram manually and enter its ID.",
            )
        finally:
            await pool.release(lease)

    # If channel_id provided, verify entity access and pre-cache
    elif channel_id and pool and pool.healthy_count() > 0:
        try:
            lease = await pool.acquire(timeout=10.0)
            client = lease.session.client
            try:
                entity = await client.get_entity(channel_id)
                backend = getattr(request.app.state, "backend", None)
                if hasattr(backend, "_entity_cache"):
                    backend._entity_cache[channel_id] = entity
                log.info("Storage channel pre-resolved successfully: %s", channel_id)
            finally:
                await pool.release(lease)
        except Exception as exc:
            log.warning("Storage channel pre-resolution note for %s: %s", channel_id, exc)

    # Register in repository and deactivate dummy placeholder pools
    if repo and hasattr(repo, "register_storage_pool"):
        if hasattr(repo, "storage_pools"):
            for p in list(repo.storage_pools.values()):
                cid = int(p.get("telegram_channel_id", 0))
                if cid in {-1001111111111, -1001111111112} or cid != int(channel_id):
                    p["is_active"] = False

        label = f"pool-{abs(channel_id) % 10000}"
        await repo.register_storage_pool(
            label=label,
            telegram_channel_id=channel_id,
            priority=0,
        )

    # Tell backend store to seed or track channel
    backend = getattr(request.app.state, "backend", None)
    if backend and hasattr(backend, "seed_channel"):
        backend.seed_channel(channel_id)

    # Update .env
    _update_env_file("STORAGE_POOL_CHANNEL_IDS", str(channel_id))
    _update_env_file("TELEDRIVE_FAKE_TELEGRAM", "0")
    os.environ["STORAGE_POOL_CHANNEL_IDS"] = str(channel_id)

    bot_service = getattr(request.app.state, "bot_service", None)
    if bot_service:
        bot_service.channel_id = str(channel_id)

    return {
        "status": "ready",
        "channel_id": channel_id,
        "message": f"Storage channel {channel_id} successfully enrolled!",
    }


async def _enroll_live_session(request: Request, *, client: Any, session_string: str, label: str) -> None:
    """Wire live Telethon client and store into runtime state."""
    pool = getattr(request.app.state, "pool", None)
    repo = getattr(request.app.state, "repo", None)
    settings: Settings = request.app.state.settings

    # If backend is fake or not TelethonStore, upgrade backend to real TelethonStore
    backend = getattr(request.app.state, "backend", None)
    if not isinstance(backend, TelethonStore):
        real_store = TelethonStore()
        request.app.state.backend = real_store
        backend = real_store

    # Ensure SessionPool is initialized and running
    if pool is None:
        from app.telegram.pool import SessionPool
        pool = SessionPool(backend=backend, settings=settings)
        await pool.start()
        request.app.state.pool = pool
    else:
        pool.backend = backend

    # Purge fake sessions from pool so requests are dispatched only to real Telegram clients
    fake_ids = [sid for sid in list(pool._sessions.keys()) if sid.startswith("fake-")]
    for fid in fake_ids:
        try:
            await pool.remove_session(fid)
        except Exception:
            pass

    session_id = uuid.uuid4().hex[:12]
    await pool.add_session(session_id=session_id, label=label, client=client)

    # Wire upload and download services with the live pool and backend
    from app.services.upload import UploadService
    from app.services.download import DownloadService

    request.app.state.upload_service = UploadService(
        repository=repo,
        pool=pool,
        backend=backend,
        settings=settings,
    )
    request.app.state.download_service = DownloadService(
        repository=repo,
        pool=pool,
        backend=backend,
        settings=settings,
    )

    # Persist in repo if postgres/memory
    if repo and hasattr(repo, "register_telegram_session"):
        try:
            from app.core.crypto import encrypt_session_string
            enc = encrypt_session_string(
                session_string, settings.master_kek, session_id_bytes=uuid.UUID(int=0).bytes
            )
            await repo.register_telegram_session(
                session_enc=enc,
                label=label,
                dc_id=getattr(getattr(client, "session", None), "dc_id", 2),
            )
        except Exception as e:
            log.warning("could not register session in repository: %s", e)
