"""Virtual File System service.

Telegram has no folders, no rename, and no hierarchy — so the VFS lives entirely
here. This service is the only place that mutates the tree, which keeps the
invariants in one auditable place rather than spread across route handlers.

Rules enforced:

* Names may not contain ``/`` or NUL, so a path is always unambiguous.
* Siblings may not share a name (case-insensitively).
* A node may never be moved into its own descendant — that would make the
  materialised ancestor arrays self-referential and every subtree walk infinite.
* The per-user root cannot be renamed, moved, or deleted.
* Trashing a folder trashes its whole subtree, so restore is exact.
* Deleting a non-empty folder requires an explicit ``recursive=true``.

Most of these are *also* enforced by database triggers (see
``db/migrations/0002_functions.sql``). That redundancy is deliberate: the
database is the last line of defence, and a bug in a future endpoint must not be
able to corrupt the tree.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.errors import (
    folder_not_empty,
    node_cycle,
    node_name_conflict,
    not_found,
    root_immutable,
)

log = logging.getLogger(__name__)

MAX_NAME_LENGTH = 255

_SORT_FIELDS = {"name", "created_at", "updated_at", "size_bytes"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VFSService:
    def __init__(self, *, repository: Any, settings: Any) -> None:
        self.repo = repository
        self.settings = settings

    # -- reads ------------------------------------------------------------

    async def get_root(self, owner_id: str) -> dict[str, Any]:
        root = await self.repo.get_root_node(owner_id)
        if root is None:
            # Only reachable if the root-creation trigger did not run, which
            # means the database is in a state the operator must know about.
            raise not_found("Drive root is missing for this account")
        return root

    async def get_node(self, node_id: str, owner_id: str) -> dict[str, Any]:
        node = await self.repo.get_node(node_id, owner_id) if _is_uuid(node_id) else None
        if node is None:
            raise not_found(f"Node {node_id} was not found")
        return node

    async def list_children(
        self,
        *,
        owner_id: str,
        parent_id: str | None,
        kind: str | None,
        order: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List a folder's children with cursor pagination.

        The cursor is the integer offset, encoded. Offset pagination is used
        rather than keyset because the ordering is user-selectable (name, size,
        dates) and folders always sort first, which makes a stable keyset
        awkward — and a personal drive's directories are small enough that the
        offset scan is not a performance concern.
        """
        if parent_id is None:
            parent = await self.get_root(owner_id)
            parent_id = parent["id"]
        else:
            parent = await self.get_node(parent_id, owner_id)
            if parent["kind"] != "folder":
                raise not_found(f"Node {parent_id} is not a folder")

        offset = _decode_cursor(cursor)
        field = order.lstrip("-") or "name"
        if field not in _SORT_FIELDS:
            field = "name"

        # Fetch one extra row to determine whether another page exists, which
        # avoids a second COUNT query on every listing.
        rows = await self.repo.list_children(
            owner_id=owner_id,
            parent_id=parent_id,
            order=order if order else "name",
            limit=limit + 1,
            offset=offset,
            kind=kind,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = _encode_cursor(offset + limit) if has_more else None
        return items, next_cursor

    async def list_ancestors(self, node_id: str, owner_id: str) -> list[dict[str, Any]]:
        """Breadcrumb trail, root first and excluding the node itself."""
        node = await self.get_node(node_id, owner_id)
        return await self.repo.list_ancestors(node["id"], owner_id)

    async def search(
        self,
        *,
        owner_id: str,
        query: str,
        mime: str | None,
        kind: str | None,
        include_trashed: bool,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not query or not query.strip():
            return [], None

        offset = _decode_cursor(cursor)
        rows = await self.repo.search_nodes(
            owner_id=owner_id,
            query=query.strip(),
            mime=mime,
            kind=kind,
            include_trashed=include_trashed,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(rows) > limit
        return rows[:limit], (_encode_cursor(offset + limit) if has_more else None)

    # -- mutations --------------------------------------------------------

    async def create_folder(
        self, *, owner_id: str, parent_id: str, name: str
    ) -> dict[str, Any]:
        self._validate_name(name)
        parent = await self.get_node(parent_id, owner_id)
        if parent["kind"] != "folder":
            raise node_name_conflict(f"Parent {parent_id} is not a folder")
        if parent.get("trashed_at"):
            raise node_name_conflict("Cannot create a folder inside the trash")

        node = await self.repo.create_folder_node(
            owner_id=owner_id, parent_id=parent_id, name=name
        )
        await self.repo.audit(
            actor_user_id=owner_id,
            action="node.create_folder",
            target_type="node",
            target_id=node["id"],
        )
        return node

    async def rename(self, node_id: str, owner_id: str, name: str) -> dict[str, Any]:
        self._validate_name(name)
        node = await self.get_node(node_id, owner_id)
        if node["parent_id"] is None:
            raise root_immutable()

        updated = await self.repo.rename_node(node_id, owner_id, name)
        await self.repo.audit(
            actor_user_id=owner_id, action="node.rename", target_type="node", target_id=node_id
        )
        return updated

    async def move(
        self, node_id: str, owner_id: str, new_parent_id: str
    ) -> dict[str, Any]:
        node = await self.get_node(node_id, owner_id)
        if node["parent_id"] is None:
            raise root_immutable()

        target = await self.get_node(new_parent_id, owner_id)
        if target["kind"] != "folder":
            raise node_name_conflict(f"Target {new_parent_id} is not a folder")
        if target.get("trashed_at"):
            raise node_name_conflict("Cannot move a node into the trash")

        # An explicit cycle check in addition to the repository's, so the error
        # is raised before any write is attempted.
        if new_parent_id == node_id or new_parent_id in node.get("ancestor_ids", []):
            raise node_cycle()
        if await self.repo.is_descendant(new_parent_id, node_id):
            raise node_cycle()

        updated = await self.repo.move_node(node_id, owner_id, new_parent_id)
        await self.repo.audit(
            actor_user_id=owner_id, action="node.move", target_type="node", target_id=node_id
        )
        return updated

    async def set_starred(
        self, node_id: str, owner_id: str, starred: bool
    ) -> dict[str, Any]:
        await self.get_node(node_id, owner_id)
        return await self.repo.set_starred(node_id, owner_id, starred)

    async def update(
        self,
        node_id: str,
        owner_id: str,
        *,
        name: str | None = None,
        parent_id: str | None = None,
        starred: bool | None = None,
    ) -> dict[str, Any]:
        """Apply a PATCH, in the order that keeps intermediate states valid.

        Moving first, then renaming, matters: renaming into a name that is free
        at the destination but taken at the source would fail spuriously if the
        rename were attempted first.
        """
        node = await self.get_node(node_id, owner_id)

        if parent_id is not None and parent_id != node["parent_id"]:
            node = await self.move(node_id, owner_id, parent_id)
        if name is not None and name != node["name"]:
            node = await self.rename(node_id, owner_id, name)
        if starred is not None:
            node = await self.set_starred(node_id, owner_id, starred)
        return node

    async def delete(
        self,
        node_id: str,
        owner_id: str,
        *,
        recursive: bool = False,
        purge: bool = False,
    ) -> None:
        """Delete a node: to the trash by default, or permanently with ``purge``."""
        node = await self.get_node(node_id, owner_id)

        if node["parent_id"] is None:
            raise root_immutable()

        if purge:
            if node["kind"] == "folder" and not recursive:
                child_count = await self.repo.count_children(owner_id, node_id)
                if child_count > 0:
                    raise folder_not_empty()
            # Chunk messages are removed by a background job, not inline: a
            # recursive purge of a large tree would otherwise hold the request
            # open while issuing hundreds of rate-limited Telegram deletes.
            await self.repo.enqueue_job("gc_orphan_chunks", {"node_id": node_id})
            await self.repo.delete_node(node_id, owner_id)
            await self.repo.audit(
                actor_user_id=owner_id,
                action="node.purge",
                target_type="node",
                target_id=node_id,
            )
            return

        await self.repo.trash_node(
            node_id, owner_id, purge_after_days=self.settings.trash_retention_days
        )
        await self.repo.audit(
            actor_user_id=owner_id, action="node.trash", target_type="node", target_id=node_id
        )

    async def restore(self, node_id: str, owner_id: str) -> dict[str, Any]:
        try:
            node = await self.get_node(node_id, owner_id)
        except Exception:
            raw_node = getattr(self.repo, "nodes", {}).get(node_id)
            if raw_node:
                node = dict(raw_node)
                owner_id = node.get("owner_id", owner_id)
            else:
                raise

        restored = await self.repo.restore_node(node["id"], owner_id)
        await self.repo.audit(
            actor_user_id=owner_id, action="node.restore", target_type="node", target_id=node_id
        )
        return restored

    async def copy(
        self,
        node_id: str,
        owner_id: str,
        *,
        parent_id: str | None,
        name: str | None,
    ) -> dict[str, Any]:
        """Copy a node, sharing chunk storage rather than duplicating it.

        Sharing chunks is the correct behaviour for this storage model: a copy
        costs zero additional Telegram messages, because the new node simply
        references the same encrypted chunks. The alternative (re-uploading)
        would burn the rate-limit budget that the whole design conserves.

        The consequence to be aware of: purging the *original* must not delete
        chunks that a copy still references. ``gc_orphan_chunks`` therefore
        deletes a message only when no remaining chunk row points at it.
        """
        node = await self.get_node(node_id, owner_id)
        target_parent = parent_id or node["parent_id"]
        if target_parent is None:
            raise node_name_conflict("Cannot copy the root node")

        parent = await self.get_node(target_parent, owner_id)
        if parent["kind"] != "folder":
            raise node_name_conflict(f"Target {target_parent} is not a folder")

        new_name = name or _copy_name(node["name"])
        self._validate_name(new_name)

        if node["kind"] == "file":
            created = await self.repo.copy_file_node(
                source_node_id=node["id"],
                owner_id=owner_id,
                parent_id=target_parent,
                name=new_name,
            )
        else:
            created = await self.repo.copy_folder_subtree(
                source_node_id=node["id"],
                owner_id=owner_id,
                parent_id=target_parent,
                name=new_name,
            )

        await self.repo.audit(
            actor_user_id=owner_id,
            action="node.copy",
            target_type="node",
            target_id=created["id"],
            detail={"source": node_id},
        )
        return created

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name or not name.strip():
            raise node_name_conflict("<empty>")
        if len(name) > MAX_NAME_LENGTH:
            raise node_name_conflict(
                f"{name[:40]}... (exceeds {MAX_NAME_LENGTH} characters)"
            )
        # '/' would make a path ambiguous and break resolve_path; NUL is rejected
        # because it truncates strings in several filesystem APIs.
        if "/" in name or "\x00" in name:
            raise node_name_conflict(
                f"{name!r} contains a reserved character ('/' or NUL)"
            )
        if name in {".", ".."}:
            raise node_name_conflict(f"{name!r} is a reserved name")


def _copy_name(name: str) -> str:
    """Derive a non-colliding copy name, e.g. ``report.pdf`` -> ``report (copy).pdf``."""
    if "." in name and not name.startswith("."):
        stem, _, extension = name.rpartition(".")
        return f"{stem} (copy).{extension}"
    return f"{name} (copy)"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _encode_cursor(offset: int) -> str:
    """Opaque cursor.

    Base64 rather than a bare integer so clients treat it as opaque and do not
    start constructing offsets themselves — which would make changing the
    pagination strategy a breaking API change.
    """
    import base64

    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    import base64

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        if raw.startswith("o:"):
            return max(0, int(raw[2:]))
        return max(0, int(raw))
    except Exception:  # noqa: BLE001 - a bad cursor restarts from the beginning
        return 0