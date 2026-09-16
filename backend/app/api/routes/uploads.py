"""Upload session endpoints.

The chunk-addressed protocol is the heart of resumability. It replaces the usual
"one long multipart POST" with four calls:

1. ``POST /uploads``            — declare intent, reserve quota, get a chunk plan.
2. ``GET  /uploads/{id}/missing`` — ask what is still needed (resume).
3. ``PUT  /uploads/{id}/chunks/{index}`` — send one chunk.
4. ``POST /uploads/{id}/complete`` — seal the file.

The server is deliberately *not* the source of truth for progress. A client that
loses its local state can resume correctly after a restart, because the missing
set is computed from the database rather than from a counter that could drift.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.core.errors import ProblemError, chunk_hash_mismatch, invalid_argument, not_found
from app.schemas import (
    AbortUploadRequest,
    ChunkReceiptResponse,
    CompleteUploadRequest,
    CompleteUploadResponse,
    CreateUploadRequest,
    MissingChunksResponse,
    NodeResponse,
    UploadSessionResponse,
)
from app.services.auth import Principal, current_user
from app.services.upload import UploadService
from app.services.vfs import VFSService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/uploads", tags=["uploads"])


def get_uploads(request: Request) -> UploadService:
    service = getattr(request.app.state, "upload_service", None)
    if service is None:
        service = UploadService(
            repository=request.app.state.repo,
            pool=getattr(request.app.state, "pool", None),
            backend=getattr(request.app.state, "backend", None),
            settings=request.app.state.settings,
        )
        request.app.state.upload_service = service
    return service


def get_vfs(request: Request) -> VFSService:
    return VFSService(repository=request.app.state.repo, settings=request.app.state.settings)


@router.post(
    "",
    response_model=UploadSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an upload session",
    description=(
        "Reserves quota and returns the chunk plan. Pass `Idempotency-Key` to make "
        "this safe to retry: repeating the call with the same key returns the "
        "original session instead of creating a second one.\n\n"
        "Quota is reserved now rather than at completion. Without a reservation two "
        "concurrent uploads could each pass a check against the same free space and "
        "together exceed the quota."
    ),
)
async def create_upload(
    payload: CreateUploadRequest,
    response: Response,
    request: Request,
    uploads: UploadService = Depends(get_uploads),
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> UploadSessionResponse:
    parent_id = payload.parent_id
    if parent_id is None:
        parent_id = (await vfs.get_root(principal.user_id))["id"]
    else:
        parent = await vfs.get_node(parent_id, principal.user_id)
        if parent["kind"] != "folder":
            raise invalid_argument("parent_id must reference a folder")

    try:
        session = await uploads.create_session(
            owner_id=principal.user_id,
            parent_id=parent_id,
            name=payload.name,
            size_bytes=payload.size_bytes,
            mime_type=payload.mime_type,
            chunk_size=payload.chunk_size,
            encryption_mode=payload.encryption_mode,
            overwrite=payload.overwrite,
            idempotency_key=payload.idempotency_key or idempotency_key,
        )
        return _session(session)
    except ProblemError:
        raise
    except Exception as exc:
        log.exception("Failed to create upload session: %s", exc)
        raise ProblemError(
            status.HTTP_400_BAD_REQUEST,
            "upload_session_failed",
            f"Failed to initialize upload session: {str(exc)}",
        ) from exc


@router.get(
    "/{upload_id}",
    response_model=UploadSessionResponse,
    summary="Upload session state",
)
async def get_upload(
    upload_id: str,
    uploads: UploadService = Depends(get_uploads),
    principal: Principal = Depends(current_user),
) -> UploadSessionResponse:
    session = await uploads.get_session(upload_id, principal.user_id)
    return _session(session)


@router.get(
    "/{upload_id}/missing",
    response_model=MissingChunksResponse,
    summary="Which chunks are still needed",
    description=(
        "The resume primitive. Computed as a set difference against the chunks "
        "actually stored, so it is correct even if the client's own bookkeeping was "
        "lost, and it is safe to call at any point including during an active upload."
    ),
)
async def get_missing_chunks(
    upload_id: str,
    uploads: UploadService = Depends(get_uploads),
    principal: Principal = Depends(current_user),
) -> MissingChunksResponse:
    session = await uploads.get_session(upload_id, principal.user_id)
    _assert_resumable(session)
    info = await uploads.missing_chunks(upload_id, principal.user_id)
    missing = info["missing_chunks"] if isinstance(info, dict) and "missing_chunks" in info else info
    return MissingChunksResponse(
        upload_id=upload_id,
        total_chunks=int(session["total_chunks"]),
        missing_chunks=missing,
        missing_count=len(missing),
        uploaded_chunks=int(session["total_chunks"]) - len(missing),
        uploaded_bytes=int(session.get("uploaded_bytes") or (info.get("received_bytes") if isinstance(info, dict) else 0)),
    )


@router.put(
    "/{upload_id}/chunks/{chunk_index}",
    response_model=ChunkReceiptResponse,
    summary="Upload one chunk",
    description=(
        "Body is the raw chunk bytes (`application/octet-stream`). Send "
        "`X-Chunk-SHA256` with the lowercase hex digest of the *plaintext* chunk; "
        "the server verifies it before making any Telegram call, so a corrupted "
        "transfer is rejected without spending the account's rate-limit budget.\n\n"
        "Retrying a chunk that already landed is safe: sending identical bytes again "
        "returns 200. Sending *different* bytes for an index that already exists "
        "returns 409, because silently overwriting would corrupt the file."
    ),
)
async def put_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    uploads: UploadService = Depends(get_uploads),
    principal: Principal = Depends(current_user),
    x_chunk_sha256: str | None = Header(default=None, alias="X-Chunk-SHA256"),
    x_chunk_iv: str | None = Header(
        default=None,
        alias="X-Chunk-IV",
        description=(
            "Base64 12-byte AES-GCM IV. Required in zero-knowledge mode, where the "
            "server must not choose the IV because it must not hold the key."
        ),
    ),
) -> ChunkReceiptResponse:
    session = await uploads.get_session(upload_id, principal.user_id)
    _assert_resumable(session)

    body = await request.body()
    if not body:
        raise invalid_argument("Chunk body must not be empty")

    if not x_chunk_sha256:
        raise invalid_argument(
            "X-Chunk-SHA256 header is required so the server can verify the chunk "
            "before it is sealed and sent to Telegram"
        )
    declared = x_chunk_sha256.strip().lower()
    if len(declared) != 64 or any(c not in "0123456789abcdef" for c in declared):
        raise invalid_argument("X-Chunk-SHA256 must be a 64-character lowercase hex digest")

    # Verify here as well as inside the service, so a mismatched chunk is rejected
    # before the body is handed to any lower layer.
    actual = hashlib.sha256(body).hexdigest()
    if actual != declared:
        raise chunk_hash_mismatch(declared, actual, chunk_index=chunk_index)

    receipt = await uploads.put_chunk(
        upload_id=upload_id,
        owner_id=principal.user_id,
        chunk_index=chunk_index,
        body=body,
        declared_sha256=declared,
        client_iv_b64=x_chunk_iv,
    )

    # The service returns a receipt for the chunk alone; the session-level
    # progress figures are added here because the client uses them to drive a
    # progress bar without a second request.
    info = await uploads.missing_chunks(upload_id, principal.user_id)
    missing = info["missing_chunks"] if isinstance(info, dict) and "missing_chunks" in info else info
    total = int(session["total_chunks"])
    return ChunkReceiptResponse(
        upload_id=upload_id,
        chunk_index=int(receipt["chunk_index"]),
        size_bytes=int(receipt["plaintext_size"]),
        ciphertext_size=int(receipt["ciphertext_size"]),
        sha256=receipt["sha256"],
        status=receipt["status"],
        uploaded_chunks=total - len(missing),
        total_chunks=total,
        uploaded_bytes=int(session.get("uploaded_bytes") or (info.get("received_bytes") if isinstance(info, dict) else 0)),
        complete=not missing,
        telegram_message_id=receipt.get("telegram_message_id"),
    )


@router.post(
    "/{upload_id}/complete",
    response_model=CompleteUploadResponse,
    summary="Finish an upload",
    description=(
        "Fails with 409 `upload_incomplete` if any chunk is missing, listing them in "
        "the problem document's `missing_chunks` extension. On success the file "
        "becomes readable and the node transitions to `ready`."
    ),
)
async def complete_upload(
    upload_id: str,
    payload: CompleteUploadRequest,
    request: Request,
    uploads: UploadService = Depends(get_uploads),
    principal: Principal = Depends(current_user),
) -> CompleteUploadResponse:
    import time

    started = time.perf_counter()
    node = await uploads.complete(
        upload_id=upload_id,
        owner_id=principal.user_id,
        declared_sha256=payload.sha256,
    )
    # ``verified`` reports whether a whole-file digest was actually checked. It is
    # False in zero-knowledge mode, where the server has no plaintext to hash —
    # claiming verification there would be a lie the client might rely on.
    verified = payload.sha256 is not None and node.get("hash_mode") != "client_hmac"

    # Notify via Telegram bot if configured
    try:
        bot_svc = getattr(request.app.state, "bot_service", None)
        if bot_svc is not None:
            import asyncio
            asyncio.create_task(bot_svc.notify_upload(node, principal))
    except Exception as exc:
        log.debug("Could not trigger upload notification: %s", exc)

    return CompleteUploadResponse(
        node=_node(node),
        verified=verified,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


@router.delete(
    "/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Abort an upload",
    description=(
        "Releases the reserved quota, discards the partial node, and schedules the "
        "already-uploaded chunk messages for reclamation."
    ),
)
async def abort_upload(
    upload_id: str,
    payload: AbortUploadRequest | None = None,
    uploads: UploadService = Depends(get_uploads),
    principal: Principal = Depends(current_user),
) -> Response:
    await uploads.abort(upload_id=upload_id, owner_id=principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _assert_resumable(session: dict) -> None:
    """Reject writes to a session that can no longer accept them.

    Attempting to continue a completed or expired upload would otherwise create
    chunk rows for a node that has already been finalised.
    """
    state = session.get("status")
    if state == "completed":
        raise invalid_argument("This upload has already been completed")
    if state in {"aborted", "expired"}:
        raise invalid_argument(f"This upload is {state} and cannot accept more chunks")


def _session(session: dict) -> UploadSessionResponse:
    missing = session.get("missing_chunks")
    total = int(session.get("total_chunks") or 0)
    uploaded = int(session.get("uploaded_chunks") or 0)
    if missing is None:
        if uploaded == 0:
            missing = list(range(total))
        else:
            missing = []
    else:
        uploaded = total - len(missing)
    return UploadSessionResponse(
        id=str(session["id"]),
        node_id=str(session["node_id"]),
        name=session.get("name") or "",
        size_bytes=int(session.get("size_bytes") or 0),
        chunk_size=int(session.get("chunk_size") or 0),
        total_chunks=total,
        status=session.get("status") or "active",
        encryption_mode=session.get("encryption_mode") or "server_managed",
        uploaded_chunks=uploaded,
        uploaded_bytes=int(session.get("uploaded_bytes") or 0),
        missing_chunks=missing,
        missing_count=len(missing),
        expires_at=session.get("expires_at"),
    )


def _node(node: dict) -> NodeResponse:
    sha = node.get("sha256")
    if isinstance(sha, (bytes, bytearray, memoryview)):
        sha = bytes(sha).hex()
    elif sha is not None:
        sha = str(sha)
    return NodeResponse(
        id=str(node["id"]),
        parent_id=str(node["parent_id"]) if node.get("parent_id") else None,
        kind=node["kind"],
        name=node["name"],
        size_bytes=int(node.get("size_bytes") or 0),
        mime_type=node.get("mime_type"),
        sha256=sha,
        hash_mode=node.get("hash_mode") or "none",
        chunk_size=node.get("chunk_size"),
        total_chunks=int(node.get("total_chunks") or 0),
        encryption_mode=node.get("encryption_mode") or "server_managed",
        upload_state=node.get("upload_state") or "ready",
        is_starred=bool(node.get("is_starred")),
        trashed_at=node.get("trashed_at"),
        purge_after=node.get("purge_after"),
        created_at=node["created_at"],
        updated_at=node["updated_at"],
        depth=int(node.get("depth") or 0),
    )