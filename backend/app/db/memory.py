"""In-memory repository.

Implements the same semantics as the PostgreSQL repository — including the rules
that are enforced by database triggers in production — so the test suite
exercises real behaviour rather than a simplified mock. Specifically it
reproduces:

* materialised ``ancestor_ids``/``depth`` maintenance and cycle rejection,
* cross-owner parent rejection,
* sibling-uniqueness including the single-root rule,
* quota reservation semantics.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

from app.core.errors import (
    email_conflict,
    folder_not_empty,
    node_cycle,
    node_name_conflict,
    not_found,
    root_immutable,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


from datetime import datetime
from pathlib import Path
import json

def _to_json_compatible(obj: Any) -> Any:
    if isinstance(obj, bytes):
        return {"__bytes_hex__": obj.hex()}
    if isinstance(obj, datetime):
        return {"__datetime_iso__": obj.isoformat()}
    if isinstance(obj, dict):
        return {str(k): _to_json_compatible(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_json_compatible(item) for item in obj]
    return obj

def _from_json_compatible(obj: Any) -> Any:
    if isinstance(obj, dict):
        if "__bytes_hex__" in obj:
            return bytes.fromhex(obj["__bytes_hex__"])
        if "__datetime_iso__" in obj:
            return datetime.fromisoformat(obj["__datetime_iso__"])
        return {k: _from_json_compatible(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_json_compatible(item) for item in obj]
    return obj


class InMemoryRepository:
    """Dictionary-backed repository with production-equivalent invariants and disk persistence."""

    #: Mirrors the ``users.quota_bytes`` column default. Telegram itself imposes
    #: no storage quota, so this is a *product* limit, not a technical one: it
    #: exists to bound abuse and to keep one account from consuming a channel.
    DEFAULT_QUOTA_BYTES = 100 * 1024**4  # 100 TiB

    def __init__(self, *, default_quota_bytes: int | None = None, db_path: str | None = None) -> None:
        import os
        self.default_quota_bytes = (
            default_quota_bytes
            if default_quota_bytes is not None
            else self.DEFAULT_QUOTA_BYTES
        )
        self.users: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}
        self.chunk_replicas: dict[str, dict[str, Any]] = {}
        self.chunk_read_failures: list[dict[str, Any]] = []
        self.upload_sessions: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[bytes, dict[str, Any]] = {}
        self.storage_pools: dict[str, dict[str, Any]] = {}
        self.jobs: list[dict[str, Any]] = []
        self.audit_log: list[dict[str, Any]] = []

        # Monotonic pool allocator so chunks spread across pools rather than
        # concentrating on the first one.
        self._pool_cursor = 0
        # Chunk accounting per channel, mirroring storage_pools counters.
        self.pool_channel_bytes: dict[int, int] = {}
        self.pool_channel_messages: dict[int, int] = {}

        # Standalone persistent database file path
        self.db_path = db_path or os.environ.get("TELEDRIVE_DB_PATH")
        if not self.db_path and "PYTEST_CURRENT_TEST" not in os.environ:
            if os.path.isdir("data") or os.environ.get("TELEDRIVE_ENV") == "production":
                self.db_path = "data/teledrive_db.json"

        self._load_state()

    def _save_state(self) -> None:
        """Atomically persist state to disk if db_path is configured."""
        if not self.db_path:
            return
        try:
            p = Path(self.db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "users": self.users,
                "nodes": self.nodes,
                "chunks": self.chunks,
                "storage_pools": self.storage_pools,
                "pool_channel_bytes": {str(k): v for k, v in self.pool_channel_bytes.items()},
                "pool_channel_messages": {str(k): v for k, v in self.pool_channel_messages.items()},
            }
            serialized = _to_json_compatible(data)
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(serialized, f, indent=2, ensure_ascii=False)
            tmp.replace(p)
        except Exception as e:
            log.debug("Could not persist database state to %s: %s", self.db_path, e)

    def _load_state(self) -> None:
        """Load state from disk if db_path exists."""
        if not self.db_path:
            return
        p = Path(self.db_path)
        if not p.is_file():
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            data = _from_json_compatible(raw)
            self.users.update(data.get("users", {}))
            self.nodes.update(data.get("nodes", {}))
            self.chunks.update(data.get("chunks", {}))
            self.storage_pools.update(data.get("storage_pools", {}))
            if "pool_channel_bytes" in data:
                self.pool_channel_bytes.update({int(k): v for k, v in data["pool_channel_bytes"].items()})
            if "pool_channel_messages" in data:
                self.pool_channel_messages.update({int(k): v for k, v in data["pool_channel_messages"].items()})
            log.info("Loaded persistent database from %s (%d users, %d nodes, %d chunks)",
                     self.db_path, len(self.users), len(self.nodes), len(self.chunks))
        except Exception as e:
            log.debug("Could not load database state from %s: %s", self.db_path, e)

    async def connect(self) -> None:
        self._load_state()

    async def close(self) -> None:
        self._save_state()

    # --- users ----------------------------------------------------------

    async def create_user(
        self,
        *,
        email: str | None,
        password_hash: str | None,
        display_name: str,
        telegram_user_id: int | None = None,
        telegram_username: str | None = None,
    ) -> dict[str, Any]:
        if email:
            for user in self.users.values():
                if user["email"] and user["email"].lower() == email.lower():
                    raise email_conflict(email)
        if telegram_user_id is not None:
            for user in self.users.values():
                if user.get("telegram_user_id") == telegram_user_id:
                    raise email_conflict(f"telegram id {telegram_user_id}")

        user_id = new_id()
        user = {
            "id": user_id,
            "email": email.lower() if email else None,
            "password_hash": password_hash,
            "display_name": display_name,
            "role": "user",
            "status": "active",
            "telegram_user_id": telegram_user_id,
            "telegram_username": telegram_username,
            # Mirrors the DEFAULT on users.quota_bytes. A zero quota would make
            # every upload fail, so the in-memory repository must apply the same
            # default rather than depending on a database trigger that is absent.
            "quota_bytes": self.default_quota_bytes,
            "used_bytes": 0,
            "dek_wrapped": None,
            "dek_version": 1,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        self.users[user_id] = user

        # Mirror the users_create_root() trigger.
        root_id = new_id()
        self.nodes[root_id] = {
            "id": root_id,
            "owner_id": user_id,
            "parent_id": None,
            "kind": "folder",
            "name": "/",
            "name_folded": "/",
            "ancestor_ids": [],
            "depth": 0,
            "size_bytes": 0,
            "mime_type": None,
            "sha256": None,
            "hash_mode": "none",
            "chunk_size": None,
            "total_chunks": 0,
            "encryption_mode": "server_managed",
            "encryption_meta": {},
            "upload_state": "ready",
            "is_starred": False,
            "trashed_at": None,
            "trash_parent_id": None,
            "purge_after": None,
            "metadata": {},
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        self._save_state()
        return dict(user)

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        user = self.users.get(user_id)
        return dict(user) if user else None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        target = email.lower()
        for user in self.users.values():
            if user["email"] and user["email"].lower() == target:
                return dict(user)
        return None

    async def get_user_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        for user in self.users.values():
            if user.get("telegram_user_id") == telegram_user_id:
                return dict(user)
        return None

    async def get_user_wrapped_dek(self, user_id: str) -> bytes | None:
        user = self.users.get(user_id)
        return user.get("dek_wrapped") if user else None

    async def set_user_wrapped_dek(self, user_id: str, wrapped: bytes, version: int) -> None:
        user = self.users.get(user_id)
        if user:
            user["dek_wrapped"] = wrapped
            user["dek_version"] = version
            user["updated_at"] = utcnow()

    async def update_password_hash(self, user_id: str, password_hash: str) -> None:
        """Replace a stored password hash, e.g. when upgrading the KDF policy."""
        user = self.users.get(user_id)
        if user:
            user["password_hash"] = password_hash
            user["updated_at"] = utcnow()

    # --- refresh tokens --------------------------------------------------

    async def store_refresh_token(
        self, *, token_hash: bytes, family_id: str, user_id: str,
        expires_at: Any, user_agent: str | None, ip: str | None,
    ) -> None:
        self.refresh_tokens[token_hash] = {
            "token_hash": token_hash,
            "family_id": family_id,
            "user_id": user_id,
            "issued_at": utcnow(),
            "expires_at": expires_at,
            "used_at": None,
            "revoked_at": None,
            "replaced_by": None,
            "user_agent": user_agent,
            "ip": ip,
        }

    async def get_refresh_token(self, token_hash: bytes) -> dict[str, Any] | None:
        token = self.refresh_tokens.get(token_hash)
        return dict(token) if token else None

    async def rotate_refresh_token(
        self, *, old_hash: bytes, new_hash: bytes, family_id: str, user_id: str,
        expires_at: Any, user_agent: str | None, ip: str | None,
    ) -> None:
        old = self.refresh_tokens.get(old_hash)
        if old:
            old["used_at"] = utcnow()
            old["replaced_by"] = new_hash
        await self.store_refresh_token(
            token_hash=new_hash,
            family_id=family_id,
            user_id=user_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )

    async def revoke_token_family(self, family_id: str) -> None:
        for token in self.refresh_tokens.values():
            if token["family_id"] == family_id and token["revoked_at"] is None:
                token["revoked_at"] = utcnow()

    async def revoke_refresh_token(self, token_hash: bytes) -> None:
        token = self.refresh_tokens.get(token_hash)
        if token:
            token["revoked_at"] = utcnow()

    # --- nodes ----------------------------------------------------------

    def _live(self, node: dict[str, Any]) -> bool:
        return node.get("trashed_at") is None

    async def get_root_node(self, owner_id: str) -> dict[str, Any] | None:
        for node in self.nodes.values():
            if node["owner_id"] == owner_id and node["parent_id"] is None:
                return dict(node)
        return None

    async def get_node(self, node_id: str, owner_id: str) -> dict[str, Any] | None:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            return None
        return dict(node)

    async def find_child_by_name(
        self, owner_id: str, parent_id: str | None, name: str
    ) -> dict[str, Any] | None:
        folded = name.lower()
        for node in self.nodes.values():
            if (
                node["owner_id"] == owner_id
                and node["parent_id"] == parent_id
                and node["name_folded"] == folded
                and self._live(node)
            ):
                return dict(node)
        return None

    async def list_children(
        self, *, owner_id: str, parent_id: str, order: str,
        limit: int, offset: int, kind: str | None = None,
        include_trashed: bool = False,
    ) -> list[dict[str, Any]]:
        children = [
            dict(n)
            for n in self.nodes.values()
            if n["owner_id"] == owner_id
            and n["parent_id"] == parent_id
            and (include_trashed or self._live(n))
            and (kind is None or n["kind"] == kind)
        ]

        descending = order.startswith("-")
        field = order.lstrip("-")
        key_map = {
            "name": lambda n: n["name_folded"],
            "created_at": lambda n: n["created_at"],
            "updated_at": lambda n: n["updated_at"],
            "size_bytes": lambda n: n["size_bytes"],
        }
        keyfn = key_map.get(field, key_map["name"])

        # Folders always sort before files, matching the NULLS-first behaviour a
        # Finder-style client expects. Doing it here keeps the two repository
        # implementations consistent.
        children.sort(key=lambda n: (n["kind"] != "folder", keyfn(n)), reverse=descending)
        return children[offset : offset + limit]

    async def count_children(
        self, owner_id: str, parent_id: str, include_trashed: bool = False
    ) -> int:
        return sum(
            1
            for n in self.nodes.values()
            if n["owner_id"] == owner_id
            and n["parent_id"] == parent_id
            and (include_trashed or self._live(n))
        )

    async def list_trashed_nodes(
        self, owner_id: str, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """Trashed nodes, most recently trashed first.

        Only *roots of* trashed subtrees are listed: when a folder is trashed its
        descendants are trashed with it, and showing every one of them would make
        the trash view a flat dump of the whole tree instead of the handful of
        things the user actually deleted.
        """
        items = [
            n
            for n in self.nodes.values()
            if n["owner_id"] == owner_id
            and n.get("trashed_at") is not None
            and n.get("trash_parent_id") is None
        ]
        items.sort(key=lambda n: n["trashed_at"], reverse=True)
        return [dict(n) for n in items[offset : offset + limit]]

    async def list_ancestors(self, node_id: str, owner_id: str) -> list[dict[str, Any]]:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            return []
        ancestors = [self.nodes[a] for a in node["ancestor_ids"] if a in self.nodes]
        ancestors.sort(key=lambda n: n["depth"])
        return [dict(a) for a in ancestors]

    async def create_folder_node(
        self, *, owner_id: str, parent_id: str, name: str
    ) -> dict[str, Any]:
        self._validate_name(name)
        parent = self.nodes.get(parent_id)
        if parent is None or parent["owner_id"] != owner_id:
            raise not_found(f"Parent folder {parent_id} was not found")
        if parent["kind"] != "folder":
            raise node_name_conflict(f"Parent {parent_id} is not a folder")
        if await self.find_child_by_name(owner_id, parent_id, name):
            raise node_name_conflict(name)

        node_id = new_id()
        self.nodes[node_id] = {
            "id": node_id,
            "owner_id": owner_id,
            "parent_id": parent_id,
            "kind": "folder",
            "name": name,
            "name_folded": name.lower(),
            "ancestor_ids": [*parent["ancestor_ids"], parent_id],
            "depth": parent["depth"] + 1,
            "size_bytes": 0,
            "mime_type": None,
            "sha256": None,
            "hash_mode": "none",
            "chunk_size": None,
            "total_chunks": 0,
            "encryption_mode": "server_managed",
            "encryption_meta": {},
            "upload_state": "ready",
            "is_starred": False,
            "trashed_at": None,
            "trash_parent_id": None,
            "purge_after": None,
            "metadata": {},
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        self._save_state()
        return dict(self.nodes[node_id])

    async def create_file_node(
        self, *, owner_id: str, parent_id: str, name: str, size_bytes: int,
        mime_type: str | None, chunk_size: int, total_chunks: int,
        encryption_mode: str, upload_state: str,
    ) -> dict[str, Any]:
        self._validate_name(name)
        parent = self.nodes.get(parent_id)
        if parent is None or parent["owner_id"] != owner_id:
            raise not_found(f"Parent folder {parent_id} was not found")

        node_id = new_id()
        self.nodes[node_id] = {
            "id": node_id,
            "owner_id": owner_id,
            "parent_id": parent_id,
            "kind": "file",
            "name": name,
            "name_folded": name.lower(),
            "ancestor_ids": [*parent["ancestor_ids"], parent_id],
            "depth": parent["depth"] + 1,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "sha256": None,
            "hash_mode": "none",
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "encryption_mode": encryption_mode,
            "encryption_meta": {},
            "upload_state": upload_state,
            "is_starred": False,
            "trashed_at": None,
            "trash_parent_id": None,
            "purge_after": None,
            "metadata": {},
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        return dict(self.nodes[node_id])

    async def rename_node(self, node_id: str, owner_id: str, name: str) -> dict[str, Any]:
        self._validate_name(name)
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            raise not_found(f"Node {node_id} was not found")
        if node["parent_id"] is None:
            raise root_immutable()

        sibling = await self.find_child_by_name(owner_id, node["parent_id"], name)
        if sibling and sibling["id"] != node_id:
            raise node_name_conflict(name)

        node["name"] = name
        node["name_folded"] = name.lower()
        node["updated_at"] = utcnow()
        return dict(node)

    async def move_node(
        self, node_id: str, owner_id: str, new_parent_id: str
    ) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            raise not_found(f"Node {node_id} was not found")
        if node["parent_id"] is None:
            raise root_immutable()

        parent = self.nodes.get(new_parent_id)
        if parent is None or parent["owner_id"] != owner_id:
            raise not_found(f"Target folder {new_parent_id} was not found")
        if parent["kind"] != "folder":
            raise node_name_conflict(f"Target {new_parent_id} is not a folder")

        # Cycle check: the target must not be the node itself or one of its
        # descendants. Without this the ancestor arrays would become
        # self-referential and subtree walks would loop forever.
        if new_parent_id == node_id or new_parent_id in node["ancestor_ids"]:
            raise node_cycle()
        if node_id in parent["ancestor_ids"]:
            raise node_cycle()

        sibling = await self.find_child_by_name(owner_id, new_parent_id, node["name"])
        if sibling and sibling["id"] != node_id:
            raise node_name_conflict(node["name"])

        self._reparent(node, parent)
        return dict(node)

    def _reparent(self, node: dict[str, Any], parent: dict[str, Any]) -> None:
        """Rewrite the subtree's ancestor arrays after a move.

        Relies on the invariant that ``depth == len(ancestor_ids)``: the root has
        ``ancestor_ids == []`` and ``depth == 0``, and every child appends exactly
        one ancestor. That makes depth a derived value rather than a separately
        maintained counter that could drift.

        Because ``ancestor_ids`` is ordered root-first, a descendant's list is
        ``[old ancestors..., node_id, ...intermediate ancestors...]``. Everything
        after ``node_id`` stays valid; only the prefix is replaced.
        """
        new_ancestors = [*parent["ancestor_ids"], parent["id"]]

        for other in self.nodes.values():
            if other["id"] == node["id"]:
                continue
            if node["id"] not in other["ancestor_ids"]:
                continue

            below_position = other["ancestor_ids"].index(node["id"]) + 1
            below = other["ancestor_ids"][below_position:]
            other["ancestor_ids"] = [*new_ancestors, node["id"], *below]
            other["depth"] = len(other["ancestor_ids"])
            other["updated_at"] = utcnow()

        node["parent_id"] = parent["id"]
        node["ancestor_ids"] = new_ancestors
        node["depth"] = len(new_ancestors)
        node["updated_at"] = utcnow()

    async def set_starred(self, node_id: str, owner_id: str, starred: bool) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            raise not_found(f"Node {node_id} was not found")
        node["is_starred"] = starred
        node["updated_at"] = utcnow()
        return dict(node)

    async def trash_node(self, node_id: str, owner_id: str, purge_after_days: int) -> None:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            raise not_found(f"Node {node_id} was not found")
        if node["parent_id"] is None:
            raise root_immutable()

        when = utcnow()
        purge_at = when + timedelta(days=purge_after_days)

        # Mirror the cascading trigger: the whole subtree is marked together so
        # that restore is exact rather than a partial reconstruction.
        if node["trashed_at"] is None:
            node["trashed_at"] = when
            node["purge_after"] = purge_at
            node["trash_parent_id"] = None
            node["updated_at"] = when

        for child in [n for n in self.nodes.values() if node_id in n["ancestor_ids"]]:
            if child["trashed_at"] is None:
                child["trashed_at"] = when
                child["purge_after"] = purge_at
                child["trash_parent_id"] = node_id
                child["updated_at"] = when

        await self.recompute_usage(owner_id)

    async def restore_node(self, node_id: str, owner_id: str) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            raise not_found(f"Node {node_id} was not found")

        targets = [node] + [
            n for n in self.nodes.values() if node_id in n["ancestor_ids"]
        ]
        for target in targets:
            target["trashed_at"] = None
            target["purge_after"] = None
            target["trash_parent_id"] = None
            target["updated_at"] = utcnow()

        await self.recompute_usage(owner_id)
        return dict(node)

    async def delete_node(self, node_id: str, owner_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            return
        if node["parent_id"] is None:
            raise root_immutable()

        # Cascade to the whole subtree, which is what makes a recursive delete
        # consistent: leaving orphaned children behind would strand their chunks.
        for child in list(self.nodes.values()):
            if node_id in child["ancestor_ids"]:
                self._delete_single(child["id"])
        self._delete_single(node_id)
        await self.recompute_usage(owner_id)

    def _delete_single(self, node_id: str) -> None:
        node = self.nodes.pop(node_id, None)
        if node is None:
            return
        for chunk_id in [
            c["id"] for c in self.chunks.values() if c["node_id"] == node_id
        ]:
            self.chunks.pop(chunk_id, None)
        for key in [
            k for k, r in self.chunk_replicas.items() if r["chunk_id"] not in self.chunks
        ]:
            self.chunk_replicas.pop(key, None)

    async def is_descendant(self, node_id: str, candidate_ancestor_id: str) -> bool:
        node = self.nodes.get(node_id)
        if node is None:
            return False
        return candidate_ancestor_id in node["ancestor_ids"]

    async def copy_file_node(
        self, *, source_node_id: str, owner_id: str, parent_id: str, name: str
    ) -> dict[str, Any]:
        """Copy a file, sharing the source's chunk rows rather than re-uploading.

        A copy therefore costs zero Telegram messages. The trade-off is that the
        same physical message is now referenced by more than one node, so garbage
        collection must delete a message only when *no* chunk row still points at
        it — see ``gc_orphan_chunks``.
        """
        source = self.nodes.get(source_node_id)
        if source is None or source["owner_id"] != owner_id:
            raise not_found(f"Node {source_node_id} was not found")
        if source["kind"] != "file":
            raise node_name_conflict("Only files can be copied with copy_file_node")

        parent = self.nodes.get(parent_id)
        if parent is None or parent["owner_id"] != owner_id:
            raise not_found(f"Parent folder {parent_id} was not found")
        if await self.find_child_by_name(owner_id, parent_id, name):
            raise node_name_conflict(name)

        node_id = new_id()
        clone = dict(source)
        clone.update(
            {
                "id": node_id,
                "parent_id": parent_id,
                "name": name,
                "name_folded": name.lower(),
                "ancestor_ids": [*parent["ancestor_ids"], parent_id],
                "depth": parent["depth"] + 1,
                "is_starred": False,
                "trashed_at": None,
                "trash_parent_id": None,
                "purge_after": None,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
        )
        self.nodes[node_id] = clone

        # Duplicate the chunk index rows so each node owns its own index, while
        # still pointing at the same Telegram message. Callers may then delete
        # one node's chunks without disturbing the other's.
        for chunk in [c for c in self.chunks.values() if c["node_id"] == source_node_id]:
            copy = dict(chunk)
            chunk_id = new_id()
            copy["id"] = chunk_id
            copy["node_id"] = node_id
            copy["created_at"] = utcnow()
            self.chunks[chunk_id] = copy

        return dict(clone)

    async def copy_folder_subtree(
        self, *, source_node_id: str, owner_id: str, parent_id: str, name: str
    ) -> dict[str, Any]:
        """Copy a folder and everything beneath it into a new subtree."""
        source = self.nodes.get(source_node_id)
        if source is None or source["owner_id"] != owner_id:
            raise not_found(f"Node {source_node_id} was not found")
        if source["kind"] != "folder":
            raise node_name_conflict("Only folders can be copied with copy_folder_subtree")

        parent = self.nodes.get(parent_id)
        if parent is None or parent["owner_id"] != owner_id:
            raise not_found(f"Parent folder {parent_id} was not found")
        if await self.find_child_by_name(owner_id, parent_id, name):
            raise node_name_conflict(name)

        # Snapshot the subtree before mutating, and order by depth so a parent is
        # always created before its children.
        descendants = sorted(
            (n for n in self.nodes.values() if source_node_id in n["ancestor_ids"]),
            key=lambda n: n["depth"],
        )

        root = await self.create_folder_node(
            owner_id=owner_id, parent_id=parent_id, name=name
        )
        # Maps old id -> new id so children can be re-parented as we walk down.
        remap: dict[str, str] = {source_node_id: root["id"]}

        for node in descendants:
            new_parent_id = remap.get(node["parent_id"])
            if new_parent_id is None:
                # Cannot happen with a well-formed tree, but skipping is far
                # better than attaching the child to the wrong folder.
                continue

            if node["kind"] == "folder":
                created = await self.create_folder_node(
                    owner_id=owner_id, parent_id=new_parent_id, name=node["name"]
                )
                remap[node["id"]] = created["id"]
            else:
                created = await self.copy_file_node(
                    source_node_id=node["id"],
                    owner_id=owner_id,
                    parent_id=new_parent_id,
                    name=node["name"],
                )
                remap[node["id"]] = created["id"]

        return await self.get_node(root["id"], owner_id)

    async def finalize_file_node(
        self, *, node_id: str, owner_id: str, sha256_hex: str | None, hash_mode: str
    ) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        if node is None or node["owner_id"] != owner_id:
            raise not_found(f"Node {node_id} was not found")

        node["upload_state"] = "ready"
        node["hash_mode"] = hash_mode
        if sha256_hex:
            node["sha256"] = bytes.fromhex(sha256_hex)
        node["updated_at"] = utcnow()
        self._save_state()
        return dict(node)

    async def search_nodes(
        self, *, owner_id: str, query: str, mime: str | None, kind: str | None,
        include_trashed: bool, limit: int, offset: int,
    ) -> list[dict[str, Any]]:
        needle = query.strip().lower()
        results = []
        for node in self.nodes.values():
            if node["owner_id"] != owner_id or node["parent_id"] is None:
                continue
            if not include_trashed and not self._live(node):
                continue
            if mime and node["mime_type"] != mime:
                continue
            if kind and node["kind"] != kind:
                continue
            if needle and needle not in node["name_folded"]:
                continue
            results.append(dict(node))

        results.sort(
            key=lambda n: (
                0 if n["name_folded"] == needle else 1 if n["name_folded"].startswith(needle) else 2,
                n["name_folded"],
            )
        )
        return results[offset : offset + limit]

    # --- quota ----------------------------------------------------------

    async def reserve_quota(self, owner_id: str, size_bytes: int) -> tuple[bool, int, int]:
        """Reserve quota, mirroring the FOR UPDATE + check of the SQL version."""
        user = self.users.get(owner_id)
        if user is None:
            raise not_found(f"User {owner_id} was not found")

        used, quota = user["used_bytes"], user["quota_bytes"]
        if quota > 0 and used + size_bytes > quota:
            return False, used, quota

        user["used_bytes"] = used + size_bytes
        user["updated_at"] = utcnow()
        return True, user["used_bytes"], quota

    async def release_quota(self, owner_id: str, size_bytes: int) -> None:
        user = self.users.get(owner_id)
        if user:
            user["used_bytes"] = max(0, user["used_bytes"] - size_bytes)
            user["updated_at"] = utcnow()

    async def recompute_usage(self, owner_id: str) -> int:
        total = sum(
            n["size_bytes"]
            for n in self.nodes.values()
            if n["owner_id"] == owner_id and n["kind"] == "file" and self._live(n)
        )
        user = self.users.get(owner_id)
        if user:
            user["used_bytes"] = total
        return total

    async def usage_breakdown(self, owner_id: str) -> dict[str, Any]:
        """Counts and trashed bytes used by the usage endpoint.

        ``trashed_bytes`` is reported separately rather than folded into
        ``used_bytes``: trashed files still occupy Telegram storage, but charging
        them against the quota would make deletion appear to free no space.
        """
        live_files = [
            n
            for n in self.nodes.values()
            if n["owner_id"] == owner_id and n["kind"] == "file" and self._live(n)
        ]
        folders = [
            n
            for n in self.nodes.values()
            if n["owner_id"] == owner_id and n["kind"] == "folder" and self._live(n)
        ]
        trashed = [
            n
            for n in self.nodes.values()
            if n["owner_id"] == owner_id
            and n["kind"] == "file"
            and n.get("trashed_at") is not None
        ]
        return {
            "file_count": len(live_files),
            "folder_count": len(folders),
            "trashed_bytes": sum(int(n["size_bytes"] or 0) for n in trashed),
            "largest_file_bytes": max(
                (int(n["size_bytes"] or 0) for n in live_files), default=0
            ),
        }

    async def ping(self) -> bool:
        """Trivial liveness check against the repository.

        The in-memory implementation cannot fail; the Postgres one runs
        ``SELECT 1``, which is enough to prove the connection is usable.
        """
        return True

    async def list_storage_pools(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self.storage_pools.values()]

    # --- chunks ---------------------------------------------------------

    async def insert_chunk(self, **kwargs: Any) -> dict[str, Any] | None:
        node_id = kwargs["node_id"]
        index = kwargs["chunk_index"]
        for chunk in self.chunks.values():
            if chunk["node_id"] == node_id and chunk["chunk_index"] == index:
                return None  # ON CONFLICT DO NOTHING

        chunk_id = new_id()
        chunk = {
            "id": chunk_id,
            "node_id": node_id,
            "chunk_index": index,
            "plaintext_size": kwargs["plaintext_size"],
            "ciphertext_size": kwargs["ciphertext_size"],
            "sha256": kwargs["sha256"],
            "iv": kwargs["iv"],
            "auth_tag": kwargs["auth_tag"],
            "storage_pool_id": kwargs["storage_pool_id"],
            "telegram_channel_id": kwargs["telegram_channel_id"],
            "telegram_message_id": kwargs["telegram_message_id"],
            "telegram_file_id": kwargs.get("telegram_file_id"),
            "telegram_access_hash": kwargs.get("telegram_access_hash"),
            "telegram_dc_id": kwargs.get("telegram_dc_id"),
            "uploaded_by_session": kwargs.get("uploaded_by_session"),
            "uploaded_at": utcnow(),
            "verified_at": None,
        }
        self.chunks[chunk_id] = chunk
        self.pool_channel_bytes[chunk["telegram_channel_id"]] = (
            self.pool_channel_bytes.get(chunk["telegram_channel_id"], 0)
            + chunk["ciphertext_size"]
        )
        self.pool_channel_messages[chunk["telegram_channel_id"]] = (
            self.pool_channel_messages.get(chunk["telegram_channel_id"], 0) + 1
        )
        self._save_state()
        return dict(chunk)

    async def get_chunk(self, node_id: str, chunk_index: int) -> dict[str, Any] | None:
        for chunk in self.chunks.values():
            if chunk["node_id"] == node_id and chunk["chunk_index"] == chunk_index:
                return dict(chunk)
        return None

    async def list_chunks(self, node_id: str) -> list[dict[str, Any]]:
        chunks = [dict(c) for c in self.chunks.values() if c["node_id"] == node_id]
        chunks.sort(key=lambda c: c["chunk_index"])
        return chunks

    async def list_chunks_in_range(
        self, *, node_id: str, first_index: int, last_index: int
    ) -> list[dict[str, Any]]:
        chunks = [
            dict(c)
            for c in self.chunks.values()
            if c["node_id"] == node_id and first_index <= c["chunk_index"] <= last_index
        ]
        chunks.sort(key=lambda c: c["chunk_index"])
        return chunks

    async def list_chunk_indices(self, node_id: str) -> list[int]:
        return sorted(c["chunk_index"] for c in self.chunks.values() if c["node_id"] == node_id)

    async def sum_chunk_plaintext_bytes(self, node_id: str) -> int:
        return sum(c["plaintext_size"] for c in self.chunks.values() if c["node_id"] == node_id)

    async def delete_node_chunks(self, node_id: str) -> None:
        for chunk_id in [c["id"] for c in self.chunks.values() if c["node_id"] == node_id]:
            self.chunks.pop(chunk_id, None)

    async def list_chunk_replicas(self, chunk_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.chunk_replicas.values()
            if r["chunk_id"] == chunk_id and not r.get("is_primary")
        ]

    async def add_chunk_replica(self, **kwargs: Any) -> dict[str, Any]:
        """Test helper: attach an extra copy of a chunk."""
        replica_id = new_id()
        replica = {"id": replica_id, "is_primary": False, **kwargs}
        self.chunk_replicas[replica_id] = replica
        return dict(replica)

    async def record_chunk_read_failure(
        self, *, chunk_id: str, message_id: int, error: str
    ) -> None:
        self.chunk_read_failures.append(
            {
                "chunk_id": chunk_id,
                "message_id": message_id,
                "failure_count": 1,
                "last_error": error,
                "blocked_until": utcnow() + timedelta(minutes=30),
            }
        )

    # --- upload sessions ------------------------------------------------

    async def create_upload_session(self, **kwargs: Any) -> dict[str, Any]:
        upload_id = new_id()
        session = {
            "id": upload_id,
            "received_chunks": 0,
            "received_bytes": 0,
            "ciphertext_bytes": 0,
            "status": "pending",
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "completed_at": None,
            **kwargs,
        }
        self.upload_sessions[upload_id] = session
        return dict(session)

    async def get_upload_session(self, upload_id: str) -> dict[str, Any] | None:
        session = self.upload_sessions.get(upload_id)
        return dict(session) if session else None

    async def find_upload_by_idempotency(
        self, owner_id: str, key: str
    ) -> dict[str, Any] | None:
        for session in self.upload_sessions.values():
            if session["owner_id"] == owner_id and session.get("idempotency_key") == key:
                return dict(session)
        return None

    async def bump_upload_progress(
        self, *, upload_id: str, plaintext_delta: int, ciphertext_delta: int
    ) -> None:
        session = self.upload_sessions.get(upload_id)
        if session:
            session["received_chunks"] += 1
            session["received_bytes"] += plaintext_delta
            session["ciphertext_bytes"] = session.get("ciphertext_bytes", 0) + ciphertext_delta
            session["updated_at"] = utcnow()

    async def complete_upload_session(self, *, upload_id: str) -> None:
        session = self.upload_sessions.get(upload_id)
        if session:
            session["status"] = "completed"
            session["completed_at"] = utcnow()
            session["updated_at"] = utcnow()

    async def abort_upload_session(self, *, upload_id: str) -> None:
        session = self.upload_sessions.get(upload_id)
        if session:
            session["status"] = "aborted"
            session["updated_at"] = utcnow()

    # --- storage pools --------------------------------------------------

    async def register_storage_pool(
        self,
        *,
        label: str,
        telegram_channel_id: int,
        max_bytes: int = 0,
        weight: int = 100,
        session_label: str | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        """Register a private channel as a storage destination.

        ``session_label`` pins a channel to the account that is a member of it.
        A channel cannot be written to by an account that has not joined, so
        recording the affinity lets selection skip pairings that would fail.
        """
        pool_id = new_id()
        pool = {
            "id": pool_id,
            "label": label,
            "telegram_channel_id": telegram_channel_id,
            "session_label": session_label,
            "max_bytes": max_bytes,
            "weight": weight,
            "priority": priority,
            "used_bytes": 0,
            "stored_bytes": 0,
            "chunk_count": 0,
            "state": "active",
            "is_active": True,
        }
        self.storage_pools[pool_id] = pool
        self._save_state()
        return dict(pool)

    async def choose_storage_pool(
        self, *, size_bytes: int, prefer_available: bool = True
    ) -> dict[str, Any]:
        """Pick a pool by weighted least-utilisation.

        Round-robin (``_pool_cursor``) is deliberately *not* the primary strategy:
        spreading messages evenly across channels is what keeps any single chat
        under its per-chat message rate limit, so round-robin is used to break
        ties between equally-loaded pools rather than as the sole rule.
        """
        candidates = [p for p in self.storage_pools.values() if p["is_active"]]
        if not candidates:
            raise not_found("No storage pools are configured")

        usable = [
            p for p in candidates if p["max_bytes"] == 0 or p["used_bytes"] + size_bytes <= p["max_bytes"]
        ] or candidates

        def utilisation(pool: dict[str, Any]) -> float:
            if pool["max_bytes"] > 0:
                return pool["used_bytes"] / pool["max_bytes"]
            # Uncapped pools are compared on absolute bytes so that a pool with a
            # soft cap does not automatically outrank an empty uncapped one.
            return pool["used_bytes"] / (100 * 1024**3)

        min_util = min(utilisation(p) for p in usable)
        finalists = [p for p in usable if utilisation(p) <= min_util * 1.02 + 1e-9]
        chosen = finalists[self._pool_cursor % len(finalists)]
        self._pool_cursor += 1

        chosen["used_bytes"] += size_bytes
        chosen["chunk_count"] += 1
        return dict(chosen)

    # --- jobs & audit ---------------------------------------------------

    async def enqueue_job(self, kind: str, payload: dict[str, Any]) -> None:
        self.jobs.append(
            {"id": new_id(), "kind": kind, "payload": payload, "attempts": 0,
             "run_at": utcnow(), "completed_at": None}
        )

    async def audit(
        self, *, actor_user_id: str | None, action: str, target_type: str | None = None,
        target_id: str | None = None, outcome: str = "success",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.audit_log.append(
            {
                "id": len(self.audit_log) + 1,
                "actor_user_id": actor_user_id,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "outcome": outcome,
                "detail": detail or {},
                "created_at": utcnow(),
            }
        )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name or len(name) > 255:
            raise node_name_conflict(name or "<empty>")
        if "/" in name or "\x00" in name:
            raise node_name_conflict(name)