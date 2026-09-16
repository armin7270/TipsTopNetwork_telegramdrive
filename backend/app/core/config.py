"""Application configuration.

Every value is environment-driven so that the same image runs in development,
CI, and production without a rebuild. Secrets have no defaults: a missing
``MASTER_KEK`` or ``JWT_SECRET`` must fail loudly at startup rather than
silently fall back to a predictable value.
"""

from __future__ import annotations

import base64
import functools
import os
from dataclasses import dataclass, field

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# Telegram's ``upload.getFile`` returns data on 4 KiB boundaries. Any request for
# an arbitrary offset must be rounded down to this granularity and trimmed
# in-process. Kept as a constant because it is a property of the protocol, not a
# tuning knob.
MTProto_ALIGNMENT = 4 * KIB

# Hard protocol ceiling for a single Telegram message payload. This is an
# estimate to be re-verified against current Telegram limits; it is enforced as
# a guard so a misconfigured chunk size cannot produce messages Telegram will
# reject.
TELEGRAM_MAX_DOCUMENT_BYTES = 2 * GIB

# Bounds for the user-selectable chunk size. The lower bound exists because
# thousands of tiny Telegram messages are a fast route to rate limiting; the
# upper bound is the Telegram document ceiling.
MIN_CHUNK_SIZE = 1 * MIB
MAX_CHUNK_SIZE = min(2 * GIB, TELEGRAM_MAX_DOCUMENT_BYTES)
DEFAULT_CHUNK_SIZE = 64 * MIB

# Sub-read size for streaming a chunk out of MTProto. Small enough that a slow
# client cannot pin much memory via backpressure, large enough to keep the
# number of round-trips reasonable.
STREAM_READ_CHUNK = 1 * MIB


class ConfigError(RuntimeError):
    """Raised when configuration is missing or internally inconsistent."""


def _b64env(name: str, *, required: bool, expected_len: int | None = None) -> bytes | None:
    raw = os.environ.get(name)
    if not raw:
        if required:
            raise ConfigError(f"{name} is required but not set")
        return None
    try:
        value = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise ConfigError(f"{name} must be valid base64: {exc}") from exc
    if expected_len is not None and len(value) != expected_len:
        raise ConfigError(
            f"{name} must decode to {expected_len} bytes, got {len(value)}"
        )
    return value


def _intenv(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _floatenv(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _listenvel(name: str) -> list[int]:
    raw = os.environ.get(name, "")
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece:
            out.append(int(piece))
    return out


def _corsenv(name: str) -> tuple[str, ...]:
    """Parse a comma-separated CORS allowlist.

    Falls back to the local development origins when unset, so a developer
    running the web client on :3000 needs no configuration, while production must
    state its origins explicitly.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ("http://localhost:3000", "http://127.0.0.1:3000")
    return tuple(piece.strip() for piece in raw.split(",") if piece.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings."""

    env: str = "development"

    database_url: str = "postgresql+asyncpg://teledrive:teledrive@localhost:5432/teledrive"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: bytes = b""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "teledrive"
    jwt_audience: str = "teledrive-clients"
    jwt_access_ttl_seconds: int = 15 * 60
    jwt_refresh_ttl_seconds: int = 30 * 24 * 3600

    # Wraps per-user DEKs and encrypts MTProto session strings at rest.
    master_kek: bytes = b""

    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_bot_token: str = ""
    telegram_session_pool_size: int = 2
    storage_pool_channel_ids: list[int] = field(default_factory=list)

    storage_chunk_size_bytes: int = DEFAULT_CHUNK_SIZE
    upload_session_ttl_seconds: int = 24 * 3600
    max_concurrent_uploads: int = 4
    max_concurrent_downloads: int = 8
    floodwait_max_sleep_seconds: int = 900
    floodwait_default_quarantine_seconds: int = 1800
    session_requests_per_minute: int = 15
    trash_retention_days: int = 30

    log_level: str = "INFO"
    sentry_dsn: str = ""

    # Explicit origin allowlist for the web client and the Mini App. Kept as a
    # list rather than a wildcard because these endpoints carry bearer tokens and
    # credentials are allowed.
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )

    # In-memory mode lets the entire stack run without Postgres, Redis, or
    # Telegram credentials. It is what the end-to-end test suite uses.
    use_in_memory_backends: bool = False
    use_fake_telegram: bool = False

    def __post_init__(self) -> None:
        if self.storage_chunk_size_bytes % MIB != 0:
            raise ConfigError("STORAGE_CHUNK_SIZE_BYTES must be a multiple of 1 MiB")
        if not (MIN_CHUNK_SIZE <= self.storage_chunk_size_bytes <= MAX_CHUNK_SIZE):
            raise ConfigError(
                "STORAGE_CHUNK_SIZE_BYTES must be between "
                f"{MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes"
            )
        if self.telegram_session_pool_size < 1:
            raise ConfigError("TELEGRAM_SESSION_POOL_SIZE must be >= 1")
        if self.max_concurrent_uploads < 1 or self.max_concurrent_downloads < 1:
            raise ConfigError("concurrency limits must be >= 1")

    @property
    def memory_ceiling_bytes(self) -> int:
        """Minimum RAM the process needs, since chunks are buffered, not spooled.

        This is the single most commonly violated operational constraint: raising
        the chunk size without raising the container memory limit causes OOM
        kills under concurrent upload.
        """
        return self.max_concurrent_uploads * self.storage_chunk_size_bytes

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        use_memory = os.environ.get("TELEDRIVE_IN_MEMORY", "").lower() in {"1", "true", "yes"}
        master_kek = _b64env("MASTER_KEK", required=not use_memory, expected_len=32)
        jwt_secret = _b64env("JWT_SECRET", required=not use_memory)

        if use_memory:
            master_kek = master_kek or b"0123456789abcdef0123456789abcdef"
            jwt_secret = jwt_secret or b"test-secret-that-is-long-enough-32b"

        api_id = _intenv("TELEGRAM_API_ID", 0)
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")

        if not use_memory:
            if not jwt_secret or len(jwt_secret) < 32:
                raise ConfigError("JWT_SECRET must decode to at least 32 bytes")
            if api_id <= 0 or not api_hash:
                raise ConfigError(
                    "TELEGRAM_API_ID and TELEGRAM_API_HASH are required to reach MTProto"
                )

        return cls(
            env=os.environ.get("TELEDRIVE_ENV", "development"),
            database_url=os.environ.get(
                "DATABASE_URL",
                "postgresql+asyncpg://teledrive:teledrive@localhost:5432/teledrive",
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            jwt_secret=jwt_secret or b"\x00" * 32,
            jwt_access_ttl_seconds=_intenv("JWT_ACCESS_TTL_SECONDS", 15 * 60, minimum=30),
            jwt_refresh_ttl_seconds=_intenv("JWT_REFRESH_TTL_SECONDS", 30 * 24 * 3600, minimum=60),
            master_kek=master_kek or b"\x00" * 32,
            telegram_api_id=api_id,
            telegram_api_hash=api_hash,
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_session_pool_size=_intenv("TELEGRAM_SESSION_POOL_SIZE", 2, minimum=1),
            storage_pool_channel_ids=_listenvel("STORAGE_POOL_CHANNEL_IDS"),
            storage_chunk_size_bytes=_intenv(
                "STORAGE_CHUNK_SIZE_BYTES", DEFAULT_CHUNK_SIZE, minimum=MIN_CHUNK_SIZE
            ),
            upload_session_ttl_seconds=_intenv(
                "UPLOAD_SESSION_TTL_SECONDS", 24 * 3600, minimum=300
            ),
            max_concurrent_uploads=_intenv("MAX_CONCURRENT_UPLOADS", 4, minimum=1),
            max_concurrent_downloads=_intenv("MAX_CONCURRENT_DOWNLOADS", 8, minimum=1),
            floodwait_max_sleep_seconds=_intenv(
                "FLOODWAIT_MAX_SLEEP_SECONDS", 900, minimum=1
            ),
            session_requests_per_minute=_intenv(
                "SESSION_REQUESTS_PER_MINUTE", 15, minimum=1
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            sentry_dsn=os.environ.get("SENTRY_DSN", ""),
            cors_origins=_corsenv("CORS_ORIGINS"),
            use_in_memory_backends=use_memory,
            use_fake_telegram=(
                False
                if os.environ.get("TELEDRIVE_FAKE_TELEGRAM", "").lower() in {"0", "false", "no"}
                else (os.environ.get("TELEDRIVE_FAKE_TELEGRAM", "").lower() in {"1", "true", "yes"} or use_memory)
            ),
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()