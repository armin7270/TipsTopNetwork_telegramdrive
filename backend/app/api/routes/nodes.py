"""Node (Virtual File System) endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.schemas import (
    BreadcrumbResponse,
    CreateFolderRequest,
    MoveNodeRequest,
    NodeListResponse,
    NodeResponse,
    SearchResponse,
    UpdateNodeRequest,
)
from app.services.auth import Principal, current_user
from app.services.vfs import VFSService

router = APIRouter(prefix="/nodes", tags=["nodes"])


def get_vfs(request: Request) -> VFSService:
    return VFSService(repository=request.app.state.repo, settings=request.app.state.settings)


@router.get(
    "/root",
    response_model=NodeResponse,
    summary="The caller's drive root",
    description=(
        "Every account has exactly one root, created when the account is. Its id is "
        "the correct `parent_id` for items at the top level — clients should not "
        "assume a fixed id or name."
    ),
)
async def get_root(
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> NodeResponse:
    return _node(await vfs.get_root(principal.user_id))


@router.get(
    "",
    response_model=NodeListResponse,
    summary="List a folder's contents",
)
async def list_nodes(
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
    parent_id: str | None = Query(default=None, description="Defaults to the drive root"),
    kind: str | None = Query(default=None, pattern="^(file|folder)$"),
    order: str = Query(default="name", description="name | created_at | updated_at | size_bytes, prefix '-' to reverse"),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
) -> NodeListResponse:
    items, next_cursor = await vfs.list_children(
        owner_id=principal.user_id,
        parent_id=parent_id,
        kind=kind,
        order=order,
        limit=limit,
        cursor=cursor,
    )
    return NodeListResponse(items=[_node(n) for n in items], next_cursor=next_cursor)


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search by name",
    description=(
        "Trigram-backed substring search with prefix matches ranked first. "
        "Unauthorised and trashed nodes are excluded unless `include_trashed` is set."
    ),
)
async def search_nodes(
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
    q: str = Query(min_length=1, max_length=200),
    mime: str | None = Query(default=None, max_length=255),
    kind: str | None = Query(default=None, pattern="^(file|folder)$"),
    include_trashed: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> SearchResponse:
    items, next_cursor = await vfs.search(
        owner_id=principal.user_id,
        query=q,
        mime=mime,
        kind=kind,
        include_trashed=include_trashed,
        limit=limit,
        cursor=cursor,
    )
    return SearchResponse(items=[_node(n) for n in items], next_cursor=next_cursor, query=q)


@router.get(
    "/trash",
    response_model=NodeListResponse,
    summary="List trashed nodes",
)
async def list_trash(
    request: Request,
    principal: Principal = Depends(current_user),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
) -> NodeListResponse:
    import base64

    offset = 0
    if cursor:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode()
            offset = max(0, int(raw[2:] if raw.startswith("o:") else raw))
        except Exception:  # noqa: BLE001 - a bad cursor restarts from the beginning
            offset = 0

    items = await request.app.state.repo.list_trashed_nodes(
        principal.user_id, limit=limit + 1, offset=offset
    )
    has_more = len(items) > limit
    page = items[:limit]
    next_cursor = None
    if has_more:
        next_cursor = (
            base64.urlsafe_b64encode(f"o:{offset + limit}".encode()).decode().rstrip("=")
        )
    return NodeListResponse(items=[_node(n) for n in page], next_cursor=next_cursor)


@router.post(
    "/folders",
    response_model=NodeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a folder",
)
async def create_folder(
    payload: CreateFolderRequest,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> NodeResponse:
    node = await vfs.create_folder(
        owner_id=principal.user_id,
        parent_id=payload.parent_id or (await vfs.get_root(principal.user_id))["id"],
        name=payload.name,
    )
    return _node(node)


@router.get("/{node_id}", response_model=NodeResponse, summary="Get one node")
async def get_node(
    node_id: str,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> NodeResponse:
    return _node(await vfs.get_node(node_id, principal.user_id))


@router.patch(
    "/{node_id}",
    response_model=NodeResponse,
    summary="Rename, move, or star a node",
    description=(
        "Partial update. Moves are applied before renames so that renaming into a "
        "name that is free at the destination does not fail against the source."
    ),
)
async def update_node(
    node_id: str,
    payload: UpdateNodeRequest,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> NodeResponse:
    node = await vfs.update(
        node_id,
        principal.user_id,
        name=payload.name,
        parent_id=payload.parent_id,
        starred=payload.is_starred,
    )
    return _node(node)


@router.put(
    "/{node_id}/parent",
    response_model=NodeResponse,
    summary="Move a node",
    description=(
        "Separate from PATCH because a move is a distinct, higher-risk operation: "
        "it rewrites the whole subtree's ancestor arrays."
    ),
)
async def move_node(
    node_id: str,
    payload: MoveNodeRequest,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> NodeResponse:
    return _node(await vfs.move(node_id, principal.user_id, payload.parent_id))


@router.get(
    "/{node_id}/ancestors",
    response_model=BreadcrumbResponse,
    summary="Breadcrumb trail for a node",
)
async def get_ancestors(
    node_id: str,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> BreadcrumbResponse:
    node = await vfs.get_node(node_id, principal.user_id)
    ancestors = await vfs.list_ancestors(node_id, principal.user_id)
    segments = [*(a["name"] for a in ancestors), node["name"]]
    return BreadcrumbResponse(
        ancestors=[_node(a) for a in ancestors],
        path="/" + "/".join(segments),
    )


@router.post(
    "/{node_id}/copy",
    response_model=NodeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Copy a node",
    description=(
        "A copy shares the original's already-uploaded encrypted chunks, so it costs "
        "no additional Telegram storage or bandwidth."
    ),
)
async def copy_node(
    node_id: str,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
    parent_id: str | None = Query(default=None),
    name: str | None = Query(default=None, max_length=255),
) -> NodeResponse:
    node = await vfs.copy(
        node_id, principal.user_id, parent_id=parent_id, name=name
    )
    return _node(node)


@router.post(
    "/{node_id}/restore",
    response_model=NodeResponse,
    summary="Restore a node from the trash",
)
async def restore_node(
    node_id: str,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
) -> NodeResponse:
    return _node(await vfs.restore(node_id, principal.user_id))


@router.delete(
    "/{node_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Move a node to the trash, or purge it permanently",
    description=(
        "By default the node moves to the trash and is restorable for the retention "
        "window. With `purge=true` it is deleted permanently: the rows go immediately "
        "and the underlying Telegram messages are reclaimed asynchronously by a "
        "background job."
    ),
)
async def delete_node(
    node_id: str,
    vfs: VFSService = Depends(get_vfs),
    principal: Principal = Depends(current_user),
    recursive: bool = Query(default=False, description="Required to purge a non-empty folder"),
    purge: bool = Query(default=False),
) -> Response:
    await vfs.delete(
        node_id, principal.user_id, recursive=recursive, purge=purge
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _node(node: dict[str, Any]) -> NodeResponse:
    """Serialise a node, normalising the few fields whose storage form differs.

    ``sha256`` is stored as bytes but must never reach a client that way, and
    ``UUID``/``datetime`` values need to be stringified for JSON.
    """
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