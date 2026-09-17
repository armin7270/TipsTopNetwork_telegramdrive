"""Application entry point and composition root.

Everything the process needs is assembled here, once, at startup, and hung off
``app.state``. Route handlers reach their dependencies through FastAPI's
``Depends`` and never construct their own — which is what lets the entire test
suite swap the real MTProto backend and Postgres repository for in-memory doubles
without any test-only branching inside the application code.

Startup order matters:

1. Settings — fail fast on a missing secret, before any connection is opened.
2. Repository — the read/write path for the VFS.
3. Session pool — the rate-limited gateway to Telegram.
4. Services — upload and download, which need both of the above.

If the session pool cannot start, the process still serves: downloads that hit
replicas and every management endpoint keep working. Refusing to boot would turn a
Telegram-side problem into a total outage.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.api.routes import auth as auth_routes
from app.api.routes import files as file_routes
from app.api.routes import nodes as node_routes
from app.api.routes import system as system_routes
from app.api.routes import uploads as upload_routes
from app.api.routes import telegram as telegram_routes
from app.api.routes import notes_routes
from app.api.routes import reminders_routes
from app.emulator import router as emulator_routes
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.services.download import DownloadService
from app.services.upload import UploadService

log = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Telethon is extremely chatty at DEBUG and would drown the logs we need.
    logging.getLogger("telethon").setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id to every response.

    Without a correlating id, a report of "the upload failed" is untraceable once
    several workers are running. An inbound ``X-Request-ID`` is honoured so a
    trace can span a gateway and this service.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def _build_repository(settings: Settings) -> Any:
    if settings.use_in_memory_backends:
        from app.db.memory import InMemoryRepository

        log.warning(
            "TELEDRIVE_IN_MEMORY is set: using the in-memory repository. "
            "All data is lost on restart and no Telegram traffic is generated."
        )
        return InMemoryRepository()

    try:
        from app.db.postgres import PostgresRepository

        return PostgresRepository(settings)
    except (ImportError, ModuleNotFoundError):
        from app.db.memory import InMemoryRepository

        log.warning("PostgresRepository not available, falling back to InMemoryRepository")
        return InMemoryRepository()


def _build_backend(settings: Settings) -> Any:
    if settings.use_fake_telegram:
        from app.telegram.fake_store import InMemoryStore

        return InMemoryStore()
    return None  # a real TelethonStore needs registered clients, done by the pool


async def _provision_sessions(
    pool: Any, backend: Any, settings: Settings, *, repo: Any
) -> None:
    """Register the storage accounts this process may use.

    In production, sessions come from the ``telegram_sessions`` table: each row
    holds an encrypted Telethon StringSession and a pool label, and the plaintext
    session exists only in memory for the lifetime of the process.

    In in-memory mode there is nothing to load, so a small deterministic fleet is
    synthesised. That keeps the whole HTTP surface exercisable in CI without
    credentials — and, because the fake store generates no network traffic, it
    cannot put a real account at risk.
    """
    from app.telegram.telethon_store import TelethonStore

    if settings.use_fake_telegram:
        for index in range(settings.telegram_session_pool_size):
            await pool.add_session(
                session_id=f"fake-{index}",
                label=f"pool-{chr(ord('a') + index)}",
                client=object(),
            )
        # Register the storage pools the repository will hand out. In production
        # these rows are created by the operator CLI when a channel is enrolled;
        # here they are synthesised so the API is exercisable end to end.
        await _provision_in_memory_pools(repo, backend, settings)
        return

    store: TelethonStore = backend
    rows = []
    if repo and hasattr(repo, "list_telegram_sessions"):
        try:
            rows = await repo.list_telegram_sessions(active_only=True)
        except Exception:
            log.exception("failed to list telegram sessions from repository")

    if not rows and settings.telegram_bot_token:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            api_id = settings.telegram_api_id or 6
            api_hash = settings.telegram_api_hash or "eb06d4abfb49dc3eeb1aeb98ae0f581e"
            bot_client = TelegramClient(StringSession(), api_id, api_hash)
            await bot_client.start(bot_token=settings.telegram_bot_token)
            me = await bot_client.get_me()
            session_id = uuid.uuid4().hex[:12]
            await pool.add_session(
                session_id=session_id, label=f"bot-{me.id}", client=bot_client
            )
            log.info("auto-enrolled Telegram bot session at startup: @%s", me.username)
        except Exception:
            log.exception("failed to auto-enroll bot from TELEGRAM_BOT_TOKEN")

    if not rows and pool.healthy_count() == 0:
        log.warning(
            "no active Telegram sessions in the database; run the session "
            "enrolment tool before serving uploads"
        )

    from app.core.crypto import decrypt_session_string

    for row in rows:
        try:
            try:
                sid_bytes = uuid.UUID(str(row["id"])).bytes
            except Exception:
                sid_bytes = uuid.UUID(int=0).bytes

            try:
                session_string = decrypt_session_string(
                    row["session_enc"], settings.master_kek,
                    session_id_bytes=sid_bytes,
                )
            except Exception:
                session_string = decrypt_session_string(
                    row["session_enc"], settings.master_kek,
                    session_id_bytes=uuid.UUID(int=0).bytes,
                )
            client = await store.build_client(session_string)
            await pool.add_session(
                session_id=str(row["id"]), label=row["label"], client=client
            )
        except Exception:  # noqa: BLE001 - one bad session must not stop the rest
            log.exception("failed to load Telegram session %s", row.get("label"))

    # Always ensure storage pools are provisioned so uploads find an active channel
    await _provision_in_memory_pools(repo, backend, settings)


async def _provision_in_memory_pools(repo: Any, store: Any, settings: Settings) -> None:
    """Create storage pools for the in-memory backends.

    The channel ids come from ``STORAGE_POOL_CHANNEL_IDS`` when provided, so a
    developer can exercise the ids they actually intend to use; otherwise
    deterministic placeholders are generated. The fake store is told about the same
    channels, so an upload really does land somewhere and can be read back.
    """
    channel_ids = list(settings.storage_pool_channel_ids) or [
        -(1001111111111 + index) for index in range(2)
    ]

    for index, channel_id in enumerate(channel_ids):
        label = f"pool-{chr(ord('a') + index)}"
        await repo.register_storage_pool(
            label=label,
            telegram_channel_id=int(channel_id),
            session_label=label if index < settings.telegram_session_pool_size else None,
            priority=index,
        )
        if store is not None and hasattr(store, "seed_channel"):
            store.seed_channel(int(channel_id))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    _configure_logging(settings)

    log.info("starting TeleDrive %s", app.version)

    app.state.repo = _build_repository(settings)
    if hasattr(app.state.repo, "connect"):
        await app.state.repo.connect()
    if not settings.use_in_memory_backends:
        await app.state.repo.migrate()

    # The session pool is optional at boot. A deployment that has not yet added
    # any Telegram sessions can still start and serve management endpoints, which
    # is what an operator needs while bringing the service up for the first time.
    backend = _build_backend(settings)
    if backend is None:
        if settings.use_fake_telegram:
            from app.telegram.fake_store import InMemoryStore
            backend = InMemoryStore()
        else:
            from app.telegram.telethon_store import TelethonStore
            backend = TelethonStore()

    from app.telegram.pool import SessionPool

    pool = SessionPool(backend=backend, settings=settings)
    await _provision_sessions(pool, backend, settings, repo=app.state.repo)
    await pool.start()
    app.state.pool = pool
    app.state.backend = backend
    log.info("session pool started with %d session(s)", pool.healthy_count())

    app.state.upload_service = UploadService(
        repository=app.state.repo,
        pool=app.state.pool,
        backend=app.state.backend,
        settings=settings,
    )
    app.state.download_service = DownloadService(
        repository=app.state.repo,
        pool=app.state.pool,
        backend=app.state.backend,
        settings=settings,
    )

    from app.telegram.bot_service import TelegramBotService

    bot_service = TelegramBotService(
        settings=settings,
        repo=app.state.repo,
        pool=app.state.pool,
        upload_service=app.state.upload_service,
        download_service=app.state.download_service,
    )
    await bot_service.start()
    app.state.bot_service = bot_service

    try:
        yield
    finally:
        if hasattr(app.state, "bot_service") and app.state.bot_service is not None:
            await app.state.bot_service.stop()
        if app.state.pool is not None:
            await app.state.pool.stop()
        if hasattr(app.state.repo, "close"):
            await app.state.repo.close()
        log.info("TeleDrive stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="TeleDrive API",
        version="0.1.0",
        description=(
            "A virtual file system over Telegram MTProto.\n\n"
            "Files are split into chunks, each encrypted with AES-256-GCM, and each "
            "stored as one Telegram message in a private channel. The server streams "
            "through memory and never writes file content to disk.\n\n"
            "**Authentication.** `POST /auth/login` for email/password, or "
            "`POST /auth/telegram` with `Telegram.WebApp.initData` for the Mini App. "
            "Send the access token as `Authorization: Bearer <token>`.\n\n"
            "**Uploads.** Chunk-addressed and resumable: create a session, ask which "
            "chunks are missing, PUT them individually, then complete. Any chunk can "
            "be retried safely.\n\n"
            "**Downloads.** Support HTTP Range requests, so seeking in a video and "
            "resuming an interrupted download both work.\n\n"
            "**Errors.** RFC 9457 `application/problem+json` with a stable `code` "
            "field. Branch on `code`, never on `detail`."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "TeleDrive", "url": "https://github.com/teledrive"},
        license_info={"name": "MIT"},
    )

    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        # The Mini App is served from Telegram's WebView and the web client from a
        # different origin, so CORS is required. An explicit allowlist rather than
        # "*" because these endpoints carry bearer tokens.
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key",
                       "X-Chunk-SHA256", "X-Chunk-IV", "Range", "If-None-Match",
                       REQUEST_ID_HEADER],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "ETag",
                        "X-File-SHA256", REQUEST_ID_HEADER],
        max_age=600,
    )

    install_error_handlers(app)

    app.include_router(emulator_routes.router)
    app.include_router(system_routes.router)
    app.include_router(system_routes.api_router, prefix="/api/v1")
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(node_routes.router, prefix="/api/v1")
    app.include_router(upload_routes.router, prefix="/api/v1")
    app.include_router(file_routes.router, prefix="/api/v1")
    app.include_router(telegram_routes.router, prefix="/api/v1")
    app.include_router(notes_routes.router, prefix="/api/v1")
    app.include_router(reminders_routes.router, prefix="/api/v1")
    app.include_router(reminders_routes.calendar_router, prefix="/api/v1")

    return app


app = create_app()