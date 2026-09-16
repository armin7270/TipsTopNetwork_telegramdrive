"""Shared test fixtures.

The suite runs entirely against the in-memory backends, so it needs no
PostgreSQL, no Redis, and no Telegram credentials. That is a deliberate design
goal: the pipeline's correctness — chunk math, resumption, AEAD, Range
arithmetic, failover — must be verifiable in CI without touching a real account,
because the tests themselves must never generate Telegram traffic.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Must be set before app.core.config is imported anywhere.
os.environ.setdefault("TELEDRIVE_IN_MEMORY", "1")
os.environ.setdefault("MASTER_KEK", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("JWT_SECRET", "dGVzdC1zZWNyZXQtdGhhdC1pcy1sb25nLWVub3VnaC0zMmI=")
os.environ.setdefault("STORAGE_CHUNK_SIZE_BYTES", str(1024 * 1024))

from app.core.config import Settings  # noqa: E402
from app.db.memory import InMemoryRepository  # noqa: E402
from app.services.download import DownloadService  # noqa: E402
from app.services.upload import UploadService  # noqa: E402
from app.telegram.fake_store import InMemoryStore  # noqa: E402
from app.telegram.pool import SessionPool  # noqa: E402


@pytest.fixture
def chunk_size() -> int:
    """1 MiB chunks keep tests fast while exercising real multi-chunk paths."""
    return 1024 * 1024


@pytest.fixture
def settings(chunk_size: int) -> Settings:
    return Settings(
        env="test",
        master_kek=b"0123456789abcdef0123456789abcdef",
        jwt_secret=b"test-secret-that-is-long-enough-32b",
        storage_chunk_size_bytes=chunk_size,
        max_concurrent_uploads=4,
        max_concurrent_downloads=4,
        telegram_session_pool_size=2,
        session_requests_per_minute=600,
        # The HTTP suite builds the real application, whose composition root
        # branches on these flags. Keeping them on means the whole API can be
        # exercised without Postgres or Telegram credentials.
        use_in_memory_backends=True,
        use_fake_telegram=True,
        # A fixed token so the Mini App initData HMAC can be exercised for real
        # rather than skipped.
        telegram_bot_token="123456:TEST-BOT-TOKEN-FOR-UNIT-TESTS",
    )


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def store() -> InMemoryStore:
    store = InMemoryStore()
    store.seed_channel(-1001111111111)
    store.seed_channel(-1002222222222)
    return store


@pytest.fixture
async def pool(store: InMemoryStore, settings: Settings) -> SessionPool:
    pool = SessionPool(backend=store, settings=settings)
    await pool.add_session(session_id="sess-a", label="pool-a", client=object(), dc_id=2)
    await pool.add_session(session_id="sess-b", label="pool-b", client=object(), dc_id=2)
    yield pool


@pytest.fixture
async def seeded(repo: InMemoryRepository, store: InMemoryStore):
    """A user with a root folder, a subfolder, and two storage pools."""
    user = await repo.create_user(
        email="owner@example.com", password_hash="x", display_name="Owner"
    )
    root = await repo.get_root_node(user["id"])
    sub = await repo.create_folder_node(
        owner_id=user["id"], parent_id=root["id"], name="documents"
    )
    await repo.register_storage_pool(label="pool-one", telegram_channel_id=-1001111111111)
    await repo.register_storage_pool(label="pool-two", telegram_channel_id=-1002222222222)
    return {"user": user, "root": root, "sub": sub}


@pytest.fixture
async def upload_service(
    pool: SessionPool, store: InMemoryStore, settings: Settings, repo: InMemoryRepository
) -> UploadService:
    return UploadService(pool=pool, backend=store, settings=settings, repository=repo)


@pytest.fixture
async def download_service(
    pool: SessionPool, store: InMemoryStore, settings: Settings, repo: InMemoryRepository
) -> DownloadService:
    return DownloadService(pool=pool, backend=store, settings=settings, repository=repo)


async def upload_bytes(
    service: UploadService,
    *,
    owner_id: str,
    parent_id: str,
    name: str,
    data: bytes,
    chunk_size: int,
    mime_type: str = "application/octet-stream",
) -> dict:
    """Upload ``data`` through the full protocol and return the finished node."""
    import hashlib

    session = await service.create_session(
        owner_id=owner_id,
        parent_id=parent_id,
        name=name,
        size_bytes=len(data),
        mime_type=mime_type,
        sha256_hex=hashlib.sha256(data).hexdigest(),
        chunk_size=chunk_size,
    )

    for index in range(session["total_chunks"]):
        start = index * chunk_size
        body = data[start : start + chunk_size]
        await service.put_chunk(
            upload_id=session["id"],
            owner_id=owner_id,
            chunk_index=index,
            body=body,
            declared_sha256=hashlib.sha256(body).hexdigest(),
        )

    return await service.complete(
        upload_id=session["id"],
        owner_id=owner_id,
        declared_sha256=hashlib.sha256(data).hexdigest(),
    )


async def collect(agen) -> bytes:
    """Drain an async generator into bytes."""
    buf = bytearray()
    async for piece in agen:
        buf.extend(piece)
    return bytes(buf)