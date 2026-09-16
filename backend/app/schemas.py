"""Request and response models.

Kept separate from the ORM-free dicts the repository returns on purpose: the wire
format is a public contract that must be able to outlive internal renames, and
serialising a database row directly couples the two.

Every response model excludes secrets. Note what is *never* present: chunk IVs in
zero-knowledge mode, wrapped DEKs, session strings, and raw Telegram access
hashes. Those are either decryption material or account-takeover material, and
clients have no use for any of them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

MAX_NAME_LENGTH = 255


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class RegisterRequest(_Base):
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)
    display_name: str = Field(default="", max_length=120)


class LoginRequest(_Base):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(_Base):
    refresh_token: str = Field(min_length=16)


class TelegramLoginRequest(_Base):
    """Mini App login.

    ``init_data`` is the verbatim ``Telegram.WebApp.initData`` string. It must be
    sent unmodified: the HMAC covers the exact field ordering and encoding.
    """

    init_data: str = Field(min_length=8)


class TokenResponse(_Base):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds")


class UserResponse(_Base):
    id: str
    email: str | None
    display_name: str
    role: str
    status: str
    quota_bytes: int
    used_bytes: int
    created_at: Any

    @property
    def quota_used_fraction(self) -> float:
        if not self.quota_bytes:
            return 0.0
        return round(self.used_bytes / self.quota_bytes, 6)


class AuthResponse(_Base):
    user: UserResponse
    tokens: TokenResponse


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

class NodeResponse(_Base):
    """A file or folder.

    ``path`` is materialised for convenience. Build it from ``ancestor_ids``
    rather than trusting a cached string: a rename deep in the tree would
    otherwise leave stale paths in clients that stored them.
    """

    id: str
    parent_id: str | None
    kind: Literal["file", "folder"]
    name: str
    size_bytes: int
    mime_type: str | None = None
    sha256: str | None = None
    hash_mode: str = "none"
    chunk_size: int | None = None
    total_chunks: int = 0
    encryption_mode: str = "server_managed"
    upload_state: str = "ready"
    is_starred: bool = False
    trashed_at: Any | None = None
    purge_after: Any | None = None
    created_at: Any
    updated_at: Any
    path: str | None = None
    depth: int = 0


class NodeListResponse(_Base):
    items: list[NodeResponse]
    next_cursor: str | None = None


class CreateFolderRequest(_Base):
    parent_id: str | None = Field(
        default=None, description="Omit or null to create directly under the drive root"
    )
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def _no_separators(cls, value: str) -> str:
        if "/" in value or "\x00" in value:
            raise ValueError("name must not contain '/' or NUL")
        if value in {".", ".."}:
            raise ValueError("name must not be '.' or '..'")
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class UpdateNodeRequest(_Base):
    """All fields optional; only those present are applied."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    parent_id: str | None = None
    is_starred: bool | None = None

    @field_validator("name")
    @classmethod
    def _no_separators(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "/" in value or "\x00" in value:
            raise ValueError("name must not contain '/' or NUL")
        return value


class MoveNodeRequest(_Base):
    parent_id: str


class BreadcrumbResponse(_Base):
    ancestors: list[NodeResponse]
    path: str


class SearchResponse(_Base):
    items: list[NodeResponse]
    next_cursor: str | None = None
    query: str


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

class CreateUploadRequest(_Base):
    parent_id: str | None = None
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    size_bytes: int = Field(ge=0, le=1 << 50, description="Exact plaintext size")
    mime_type: str | None = Field(default=None, max_length=255)
    chunk_size: int | None = Field(
        default=None,
        description=(
            "Chunk size in bytes. Must be a multiple of 1 MiB. Defaults to 64 MiB. "
            "A smaller value gives finer resume granularity at the cost of more "
            "Telegram messages (and therefore more rate-limit budget)."
        ),
    )
    encryption_mode: Literal["server_managed", "zero_knowledge"] = "server_managed"
    overwrite: bool = False
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _no_separators(cls, value: str) -> str:
        if "/" in value or "\x00" in value:
            raise ValueError("name must not contain '/' or NUL")
        return value

    @field_validator("chunk_size")
    @classmethod
    def _aligned(cls, value: int | None) -> int | None:
        if value is None:
            return None
        mib = 1024 * 1024
        if value % mib != 0:
            raise ValueError("chunk_size must be a multiple of 1 MiB (1048576 bytes)")
        return value


class UploadSessionResponse(_Base):
    """Everything a client needs to drive a resumable upload.

    ``missing_chunks`` is included on creation so a client resuming an interrupted
    transfer can start immediately without a second round trip.
    """

    id: str
    node_id: str
    name: str
    size_bytes: int
    chunk_size: int
    total_chunks: int
    status: str
    encryption_mode: str
    uploaded_chunks: int
    uploaded_bytes: int
    missing_chunks: list[int]
    missing_count: int
    expires_at: Any | None = None


class MissingChunksResponse(_Base):
    upload_id: str
    total_chunks: int
    missing_chunks: list[int]
    missing_count: int
    uploaded_chunks: int
    uploaded_bytes: int


class ChunkReceiptResponse(_Base):
    upload_id: str
    chunk_index: int
    size_bytes: int
    ciphertext_size: int
    sha256: str
    status: Literal["stored", "already_present"]
    uploaded_chunks: int
    total_chunks: int
    uploaded_bytes: int
    complete: bool
    telegram_message_id: int | None = None


class CompleteUploadRequest(_Base):
    sha256: str | None = Field(
        default=None,
        description=(
            "Hex-encoded SHA-256 of the whole plaintext file. Verified against the "
            "server's own recomputation before the file is marked ready."
        ),
    )


class CompleteUploadResponse(_Base):
    node: NodeResponse
    verified: bool
    duration_ms: int


class AbortUploadRequest(_Base):
    reason: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

class UsageResponse(_Base):
    quota_bytes: int
    used_bytes: int
    trashed_bytes: int
    available_bytes: int
    file_count: int
    folder_count: int


class StoragePoolResponse(_Base):
    id: str
    label: str
    telegram_channel_id: int
    session_label: str | None = None
    state: str
    chunk_count: int
    stored_bytes: int
    priority: int


class HealthResponse(_Base):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, str]


class ReadyResponse(_Base):
    status: Literal["ready", "not_ready"]
    database: str
    sessions_healthy: int
    sessions_total: int
    detail: str | None = None


class ProblemDetail(_Base):
    """RFC 9457 problem document.

    ``code`` is the stable, machine-readable field. Clients must branch on it and
    never on ``detail``, which is prose and will change.
    """

    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    instance: str | None = None
    request_id: str | None = None
    retry_after_seconds: int | None = None
    extensions: dict[str, Any] | None = None