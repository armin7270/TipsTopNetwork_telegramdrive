"""Content download endpoints.

This is the streaming path, and the whole point of the design: bytes travel
Telegram → server memory → client without ever touching disk. The response is an
async generator fed by :meth:`DownloadService.stream`, so memory use is bounded by
the chunk size rather than the file size — a 10 GiB file is served with the same
footprint as a 10 MiB one.

Two details are easy to get wrong and are handled deliberately:

* **Range requests.** A partially-covered file must answer ``206`` with a
  ``Content-Range`` header; answering ``200`` would make a resuming downloader
  append the whole file to its partial one and silently corrupt it.
* **Authentication errors mid-stream.** Once the first byte is sent the status
  line is already on the wire and cannot be changed. A chunk that fails to
  authenticate therefore *aborts the connection* rather than ending the body
  cleanly — a truncated body that looks complete is the one outcome that would
  let silent corruption reach the client's disk.
"""

from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.core.errors import (
    invalid_argument,
    not_found,
)
from app.services.auth import Principal, current_user
from app.services.download import ByteRange, parse_range_header
from app.services.download import DownloadService
from app.services.vfs import VFSService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["content"])

# Buffered read size while draining a decrypted chunk to the socket. Small enough
# to keep the event loop responsive, large enough that a 10 GiB transfer does not
# pay for a million tiny writes.
STREAM_YIELD_SIZE = 256 * 1024


def get_download(request: Request) -> DownloadService:
    return request.app.state.download_service


def get_vfs(request: Request) -> VFSService:
    return VFSService(repository=request.app.state.repo, settings=request.app.state.settings)


@router.get(
    "/{node_id}/content",
    summary="Download file content",
    response_class=StreamingResponse,
    responses={
        200: {"description": "Full file body", "content": {"application/octet-stream": {}}},
        206: {"description": "Partial body, as requested by Range"},
        416: {"description": "Range not satisfiable"},
    },
)
async def download_content(
    node_id: str,
    request: Request,
    downloads: DownloadService = Depends(get_download),
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
    range_header: str | None = Header(default=None, alias="Range"),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    disposition: str = Query(
        default="attachment",
        pattern="^(attachment|inline)$",
        description="'inline' lets browsers preview images, PDFs, and video.",
    ),
) -> Response:
    if principal.role == "admin":
        raw_node = getattr(request.app.state.repo, "nodes", {}).get(node_id)
        node = dict(raw_node) if raw_node else await vfs.get_node(node_id, principal.user_id)
    else:
        node = await vfs.get_node(node_id, principal.user_id)

    if node["kind"] != "file":
        raise not_found(f"Node {node_id} is not a file")

    total_size = int(node["size_bytes"] or 0)
    etag = downloads.etag_for(node)

    # A matching ETag means the client's copy is already current. This is what
    # makes re-opening a large file in the UI free.
    if etag and if_none_match and _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag})

    byte_range: ByteRange | None = None
    if range_header:
        byte_range = parse_range_header(range_header, total_size)

    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _disposition_header(disposition, node["name"]),
        "Cache-Control": "private, max-age=0, must-revalidate",
        # Discourage intermediaries from trying to inspect or transform an
        # encrypted-at-rest payload.
        "X-Content-Type-Options": "nosniff",
        "Content-Encoding": "identity",
    }
    if node.get("mime_type"):
        headers["Content-Type"] = node["mime_type"]
    if etag:
        headers["ETag"] = etag
    if node.get("sha256"):
        raw = node["sha256"]
        headers["X-File-SHA256"] = (
            bytes(raw).hex() if isinstance(raw, (bytes, bytearray, memoryview)) else str(raw)
        )

    if byte_range is not None:
        headers["Content-Range"] = byte_range.content_range_header(total_size)
        status_code = 206
        content_length = byte_range.length
    else:
        status_code = 200
        content_length = total_size

    # Set explicitly rather than left to the ASGI server: a wrong or missing
    # Content-Length on a 206 makes clients mis-frame the partial body.
    headers["Content-Length"] = str(content_length)

    stream = downloads.stream(
        node_id=node["id"],
        owner_id=node["owner_id"],
        byte_range=byte_range,
    )
    return StreamingResponse(
        _guarded(stream, node_id=node["id"], total_size=total_size),
        status_code=status_code,
        headers=headers,
        media_type=node.get("mime_type") or "application/octet-stream",
    )


@router.get(
    "/{node_id}/thumbnail",
    summary="Download a generated preview thumbnail",
    responses={
        200: {"description": "Thumbnail bytes", "content": {"image/webp": {}}},
        404: {"description": "No thumbnail is available for this file"},
    },
)
async def download_thumbnail(
    node_id: str,
    request: Request,
    downloads: DownloadService = Depends(get_download),
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> Response:
    """Serve a stored thumbnail.

    Only available for ``server_managed`` files: in zero-knowledge mode the server
    never holds the key, so it cannot decrypt an image to generate one. That is an
    inherent consequence of the mode, not a bug, and the endpoint says so rather
    than returning a broken image.
    """
    if principal.role == "admin":
        raw_node = getattr(request.app.state.repo, "nodes", {}).get(node_id)
        node = dict(raw_node) if raw_node else await vfs.get_node(node_id, principal.user_id)
    else:
        node = await vfs.get_node(node_id, principal.user_id)
    if node["kind"] != "file":
        raise not_found(f"Node {node_id} is not a file")

    if node.get("encryption_mode") == "zero_knowledge":
        raise not_found(
            "Thumbnails are unavailable for zero-knowledge files: the server "
            "cannot decrypt them. Generate previews client-side."
        )

    stored = await downloads.repo.get_thumbnail(node["id"]) if hasattr(
        downloads.repo, "get_thumbnail"
    ) else None
    if stored is None:
        raise not_found("No thumbnail has been generated for this file")

    return Response(
        content=stored["data"],
        media_type=stored.get("mime_type") or "image/webp",
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": f'"{stored["sha256"]}"' if stored.get("sha256") else "",
        },
    )


@router.head(
    "/{node_id}/content",
    summary="File metadata without a body",
    description=(
        "Returns the same headers as a GET — including `Accept-Ranges`, `ETag`, and "
        "`Content-Length` — so a client can plan a resumable transfer without "
        "downloading anything."
    ),
)
async def head_content(
    node_id: str,
    request: Request,
    downloads: DownloadService = Depends(get_download),
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> Response:
    if principal.role == "admin":
        raw_node = getattr(request.app.state.repo, "nodes", {}).get(node_id)
        node = dict(raw_node) if raw_node else await vfs.get_node(node_id, principal.user_id)
    else:
        node = await vfs.get_node(node_id, principal.user_id)
    if node["kind"] != "file":
        raise not_found(f"Node {node_id} is not a file")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(int(node["size_bytes"] or 0)),
        "Content-Disposition": _disposition_header("attachment", node["name"]),
    }
    etag = downloads.etag_for(node)
    if etag:
        headers["ETag"] = etag
    if node.get("mime_type"):
        headers["Content-Type"] = node["mime_type"]
    return Response(status_code=200, headers=headers)


async def _guarded(stream, *, node_id: str, total_size: int):
    """Wrap the decrypting stream so mid-transfer failures are not silent.

    If a chunk fails authentication or its message has vanished with no replica,
    the only safe action is to abort: the client sees a truncated response and its
    HTTP layer reports an error. Ending the body cleanly instead would hand over a
    corrupt file that looks complete — precisely the failure this system exists to
    prevent.
    """
    sent = 0
    try:
        async for piece in stream:
            sent += len(piece)
            # Re-slice to a fixed size so a large chunk does not become one huge
            # write that blocks the event loop.
            for offset in range(0, len(piece), STREAM_YIELD_SIZE):
                yield piece[offset : offset + STREAM_YIELD_SIZE]
    except Exception:
        log.error(
            "download of node %s aborted after %d/%d bytes",
            node_id,
            sent,
            total_size,
            exc_info=True,
        )
        raise


def _etag_matches(header_value: str, etag: str) -> bool:
    """Compare an If-None-Match header, honouring weak validators and lists."""
    candidates = [c.strip() for c in header_value.split(",")]
    for candidate in candidates:
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _disposition_header(kind: str, filename: str) -> str:
    """Build a Content-Disposition value that survives non-ASCII names.

    A bare UTF-8 filename breaks in older clients and lets a crafted name inject
    header syntax, so the name is emitted twice: an ASCII-sanitised ``filename``
    and an RFC 5987 ``filename*`` with the exact UTF-8 form.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_").replace("\r", "").replace("\n", "")
    quoted = urllib.parse.quote(filename, safe="")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"