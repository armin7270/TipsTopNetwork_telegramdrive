"""HTTP-level end-to-end tests.

These drive the real ASGI application — routing, dependency injection,
authentication, serialisation, and the streaming response — against the
in-memory repository and fake Telegram store. The service-level suites prove the
engine works; this suite proves the *product* works, including the parts that only
exist at the HTTP layer: problem+json error shapes, Range negotiation, auth
boundaries, and the fact that a 206 really carries the right Content-Range.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

MB = 1024 * 1024
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
async def client(settings: Settings):
    """A client bound to a freshly built app, with startup actually executed.

    ``ASGITransport`` does not run lifespan events, so the composition root is
    entered explicitly — otherwise the app would have no repository and every
    request would fail. ``LifespanManager`` is avoided in favour of entering the
    router's lifespan context directly, which keeps the dependency surface small.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _register(client: AsyncClient, email: str = "user@example.com") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Test User"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _upload_file(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str,
    data: bytes,
    parent_id: str | None = None,
    chunk_size: int = MB,
) -> dict:
    """Drive the full four-call upload protocol over HTTP."""
    payload = {
        "name": name,
        "size_bytes": len(data),
        "chunk_size": chunk_size,
        "mime_type": "application/octet-stream",
    }
    if parent_id:
        payload["parent_id"] = parent_id

    created = await client.post("/api/v1/uploads", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    session = created.json()

    for index in range(session["total_chunks"]):
        start = index * chunk_size
        piece = data[start : start + chunk_size]
        resp = await client.put(
            f"/api/v1/uploads/{session['id']}/chunks/{index}",
            content=piece,
            headers={
                **headers,
                "X-Chunk-SHA256": hashlib.sha256(piece).hexdigest(),
                "Content-Type": "application/octet-stream",
            },
        )
        assert resp.status_code == 200, resp.text

    done = await client.post(
        f"/api/v1/uploads/{session['id']}/complete",
        json={"sha256": hashlib.sha256(data).hexdigest()},
        headers=headers,
    )
    assert done.status_code == 200, done.text
    return done.json()


class TestSystemEndpoints:
    async def test_healthz_is_liveness_only(self, client):
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_readyz_reports_database(self, client):
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["database"] == "ok"

    async def test_request_id_is_echoed(self, client):
        resp = await client.get("/healthz")
        assert resp.headers.get("X-Request-ID")

    async def test_inbound_request_id_is_preserved(self, client):
        """Honouring an inbound id lets a trace span the gateway and this service."""
        resp = await client.get("/healthz", headers={"X-Request-ID": "trace-me-123"})
        assert resp.headers["X-Request-ID"] == "trace-me-123"

    async def test_openapi_schema_is_valid(self, client):
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["openapi"].startswith("3.")
        count = sum(len(v) for v in spec["paths"].values())
        assert count >= 30, "every documented endpoint should be in the schema"


class TestAuthFlow:
    async def test_register_returns_tokens_and_provisions_root(self, client):
        body = await _register(client)
        assert body["tokens"]["access_token"]
        assert body["user"]["quota_bytes"] > 0

        headers = _auth(body["tokens"])
        root = await client.get("/api/v1/nodes/root", headers=headers)
        assert root.status_code == 200
        assert root.json()["kind"] == "folder"

    async def test_duplicate_email_is_rejected_without_confirming_existence(self, client):
        await _register(client, "dupe@example.com")
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "dupe@example.com", "password": PASSWORD},
        )
        # 400 invalid_credentials, deliberately not a distinct "already exists"
        # code that would confirm the account is real.
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_credentials"

    async def test_login_and_me(self, client):
        await _register(client, "login@example.com")
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "login@example.com", "password": PASSWORD},
        )
        assert resp.status_code == 200
        headers = _auth(resp.json()["tokens"])
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["email"] == "login@example.com"

    async def test_wrong_password_is_401_problem_json(self, client):
        await _register(client, "wrong@example.com")
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "wrong@example.com", "password": "not-the-password-xx"},
        )
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "invalid_credentials"

    async def test_unknown_email_gives_the_same_error_as_wrong_password(self, client):
        """Different responses would be a user-enumeration oracle."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": PASSWORD},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_credentials"

    async def test_short_password_is_rejected_by_schema(self, client):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "short@example.com", "password": "tiny"},
        )
        assert resp.status_code == 422

    async def test_missing_token_is_401(self, client):
        resp = await client.get("/api/v1/nodes/root")
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"

    async def test_garbage_token_is_401(self, client):
        resp = await client.get(
            "/api/v1/nodes/root", headers={"Authorization": "Bearer not.a.jwt"}
        )
        assert resp.status_code == 401

    async def test_refresh_rotates_and_detects_reuse(self, client):
        body = await _register(client, "rotate@example.com")
        original = body["tokens"]["refresh_token"]

        first = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert first.status_code == 200
        rotated = first.json()["refresh_token"]
        assert rotated != original

        # Replaying the original is theft: the whole family must die.
        replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert replay.status_code == 401
        assert replay.json()["code"] == "refresh_reuse_detected"

        # ...which also invalidates the token the legitimate client was holding.
        after = await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
        assert after.status_code == 401

    async def test_logout_revokes_refresh_token(self, client):
        body = await _register(client, "logout@example.com")
        token = body["tokens"]["refresh_token"]
        out = await client.post("/api/v1/auth/logout", json={"refresh_token": token})
        assert out.status_code == 204
        resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert resp.status_code == 401

    async def test_telegram_login_rejects_forged_initdata(self, client, settings):
        """A forged signature must never produce a session."""
        forged = "user=%7B%22id%22%3A1%7D&auth_date=9999999999&hash=deadbeef"
        resp = await client.post("/api/v1/auth/telegram", json={"init_data": forged})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_init_data"

    async def test_telegram_login_rejects_stale_initdata(self, client, settings):
        """A valid signature must still expire, or a captured string is a
        permanent credential."""
        import hashlib
        import hmac
        import time
        import urllib.parse

        if not settings.telegram_bot_token:
            pytest.skip("no bot token configured")

        stale_date = int(time.time()) - 7200
        fields = {"auth_date": str(stale_date), "query_id": "AAA", "user": '{"id":42}'}
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        secret = hmac.new(
            b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256
        ).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

        resp = await client.post(
            "/api/v1/auth/telegram",
            json={"init_data": urllib.parse.urlencode(fields)},
        )
        assert resp.status_code == 401
        assert "too old" in resp.json()["detail"]


class TestVFSOverHTTP:
    @pytest.fixture
    async def session(self, client):
        body = await _register(client, "vfs@example.com")
        headers = _auth(body["tokens"])
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        return client, headers, root

    async def test_create_and_list_folders(self, session):
        client, headers, root = session
        created = await client.post(
            "/api/v1/nodes/folders",
            json={"parent_id": root["id"], "name": "documents"},
            headers=headers,
        )
        assert created.status_code == 201

        listing = await client.get(
            f"/api/v1/nodes?parent_id={root['id']}", headers=headers
        )
        assert listing.status_code == 200
        names = [n["name"] for n in listing.json()["items"]]
        assert "documents" in names

    async def test_cannot_create_folder_with_slash_in_name(self, session):
        client, headers, root = session
        resp = await client.post(
            "/api/v1/nodes/folders",
            json={"parent_id": root["id"], "name": "a/b"},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_duplicate_sibling_name_conflicts(self, session):
        client, headers, root = session
        payload = {"parent_id": root["id"], "name": "same"}
        assert (await client.post("/api/v1/nodes/folders", json=payload, headers=headers)).status_code == 201
        second = await client.post("/api/v1/nodes/folders", json=payload, headers=headers)
        assert second.status_code == 409
        assert second.json()["code"] == "node_name_conflict"

    async def test_case_insensitive_sibling_uniqueness(self, session):
        """Case-only differences would collide on Windows, macOS, and in users'
        expectations, so they are treated as the same name."""
        client, headers, root = session
        await client.post(
            "/api/v1/nodes/folders",
            json={"parent_id": root["id"], "name": "Reports"},
            headers=headers,
        )
        resp = await client.post(
            "/api/v1/nodes/folders",
            json={"parent_id": root["id"], "name": "reports"},
            headers=headers,
        )
        assert resp.status_code == 409

    async def test_rename_move_and_breadcrumbs(self, session):
        client, headers, root = session
        work = (
            await client.post(
                "/api/v1/nodes/folders",
                json={"parent_id": root["id"], "name": "work"},
                headers=headers,
            )
        ).json()
        inner = (
            await client.post(
                "/api/v1/nodes/folders",
                json={"parent_id": work["id"], "name": "reports"},
                headers=headers,
            )
        ).json()
        leaf = (
            await client.post(
                "/api/v1/nodes/folders",
                json={"parent_id": inner["id"], "name": "2024"},
                headers=headers,
            )
        ).json()

        renamed = await client.patch(
            f"/api/v1/nodes/{leaf['id']}", json={"name": "2025"}, headers=headers
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "2025"

        crumbs = await client.get(
            f"/api/v1/nodes/{leaf['id']}/ancestors", headers=headers
        )
        assert crumbs.status_code == 200
        assert crumbs.json()["path"].endswith("/2025")
        assert "work" in crumbs.json()["path"]

    async def test_move_into_own_descendant_is_rejected(self, session):
        """Allowing this would make the ancestor arrays self-referential and every
        subtree walk infinite."""
        client, headers, root = session
        outer = (
            await client.post(
                "/api/v1/nodes/folders",
                json={"parent_id": root["id"], "name": "outer"},
                headers=headers,
            )
        ).json()
        inner = (
            await client.post(
                "/api/v1/nodes/folders",
                json={"parent_id": outer["id"], "name": "inner"},
                headers=headers,
            )
        ).json()

        resp = await client.put(
            f"/api/v1/nodes/{outer['id']}/parent",
            json={"parent_id": inner["id"]},
            headers=headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "node_cycle"

    async def test_root_cannot_be_renamed_or_deleted(self, session):
        client, headers, root = session
        renamed = await client.patch(
            f"/api/v1/nodes/{root['id']}", json={"name": "newroot"}, headers=headers
        )
        assert renamed.status_code == 409
        assert renamed.json()["code"] == "root_immutable"

        deleted = await client.delete(f"/api/v1/nodes/{root['id']}", headers=headers)
        assert deleted.status_code == 409

    async def test_trash_and_restore(self, session):
        client, headers, root = session
        folder = (
            await client.post(
                "/api/v1/nodes/folders",
                json={"parent_id": root["id"], "name": "temp"},
                headers=headers,
            )
        ).json()

        assert (
            await client.delete(f"/api/v1/nodes/{folder['id']}", headers=headers)
        ).status_code == 204

        # Gone from the normal listing...
        listing = await client.get(f"/api/v1/nodes?parent_id={root['id']}", headers=headers)
        assert folder["id"] not in [n["id"] for n in listing.json()["items"]]

        # ...but present in the trash, and restorable.
        trash = await client.get("/api/v1/nodes/trash", headers=headers)
        assert folder["id"] in [n["id"] for n in trash.json()["items"]]

        restored = await client.post(
            f"/api/v1/nodes/{folder['id']}/restore", headers=headers
        )
        assert restored.status_code == 200
        assert restored.json()["trashed_at"] is None

    async def test_other_users_cannot_see_or_touch_nodes(self, client):
        """The core multi-tenancy guarantee: every query is owner-scoped."""
        a = await _register(client, "alice@example.com")
        b = await _register(client, "bob@example.com")
        ha, hb = _auth(a["tokens"]), _auth(b["tokens"])
        root_a = (await client.get("/api/v1/nodes/root", headers=ha)).json()

        secret = (
            await client.post(
                "/api/v1/nodes/folders",
                json={"parent_id": root_a["id"], "name": "alice-private"},
                headers=ha,
            )
        ).json()

        # Bob cannot read it...
        assert (
            await client.get(f"/api/v1/nodes/{secret['id']}", headers=hb)
        ).status_code == 404
        # ...cannot rename it...
        assert (
            await client.patch(
                f"/api/v1/nodes/{secret['id']}", json={"name": "stolen"}, headers=hb
            )
        ).status_code == 404
        # ...and cannot delete it.
        assert (
            await client.delete(f"/api/v1/nodes/{secret['id']}", headers=hb)
        ).status_code == 404

    async def test_search_finds_by_substring(self, session):
        client, headers, root = session
        await client.post(
            "/api/v1/nodes/folders",
            json={"parent_id": root["id"], "name": "quarterly-reports"},
            headers=headers,
        )
        resp = await client.get("/api/v1/nodes/search?q=quarter", headers=headers)
        assert resp.status_code == 200
        assert any("quarterly" in n["name"] for n in resp.json()["items"])


class TestUploadOverHTTP:
    @pytest.fixture
    async def session(self, client):
        body = await _register(client, "upload@example.com")
        headers = _auth(body["tokens"])
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        return client, headers, root

    async def test_full_upload_and_download_roundtrip(self, session):
        client, headers, root = session
        data = os.urandom(2 * MB + 1234)

        result = await _upload_file(
            client, headers, name="roundtrip.bin", data=data, parent_id=root["id"]
        )
        node = result["node"]
        assert node["upload_state"] == "ready"
        assert node["size_bytes"] == len(data)
        assert result["verified"] is True

        resp = await client.get(f"/api/v1/files/{node['id']}/content", headers=headers)
        assert resp.status_code == 200
        assert resp.content == data
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert resp.headers["Content-Length"] == str(len(data))

    async def test_missing_chunks_endpoint_drives_resume(self, session):
        """The resume contract: a client that lost its state can still finish."""
        client, headers, root = session
        data = os.urandom(3 * MB)
        session_resp = (
            await client.post(
                "/api/v1/uploads",
                json={
                    "parent_id": root["id"],
                    "name": "resume.bin",
                    "size_bytes": len(data),
                    "chunk_size": MB,
                },
                headers=headers,
            )
        ).json()
        upload_id = session_resp["id"]
        assert session_resp["missing_chunks"] == [0, 1, 2]

        # Send only chunk 1, simulating an interrupted transfer.
        piece = data[MB : 2 * MB]
        await client.put(
            f"/api/v1/uploads/{upload_id}/chunks/1",
            content=piece,
            headers={
                **headers,
                "X-Chunk-SHA256": hashlib.sha256(piece).hexdigest(),
            },
        )

        missing = await client.get(
            f"/api/v1/uploads/{upload_id}/missing", headers=headers
        )
        assert missing.status_code == 200
        assert missing.json()["missing_chunks"] == [0, 2]
        assert missing.json()["uploaded_chunks"] == 1

    async def test_chunk_hash_mismatch_is_rejected_before_storage(self, session):
        """A corrupted transfer must be refused without spending the account's
        rate-limit budget on a doomed Telegram call."""
        client, headers, root = session
        session_resp = (
            await client.post(
                "/api/v1/uploads",
                json={
                    "parent_id": root["id"],
                    "name": "bad.bin",
                    "size_bytes": MB,
                    "chunk_size": MB,
                },
                headers=headers,
            )
        ).json()

        resp = await client.put(
            f"/api/v1/uploads/{session_resp['id']}/chunks/0",
            content=b"x" * MB,
            headers={**headers, "X-Chunk-SHA256": hashlib.sha256(b"different").hexdigest()},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "chunk_hash_mismatch"
        assert body["extensions"]["expected_sha256"]

    async def test_missing_hash_header_is_rejected(self, session):
        client, headers, root = session
        session_resp = (
            await client.post(
                "/api/v1/uploads",
                json={
                    "parent_id": root["id"],
                    "name": "nohash.bin",
                    "size_bytes": MB,
                    "chunk_size": MB,
                },
                headers=headers,
            )
        ).json()
        resp = await client.put(
            f"/api/v1/uploads/{session_resp['id']}/chunks/0",
            content=b"x" * MB,
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_argument"

    async def test_idempotent_chunk_replay_succeeds(self, session):
        """Retrying an interrupted request must not corrupt anything."""
        client, headers, root = session
        data = os.urandom(MB)
        session_resp = (
            await client.post(
                "/api/v1/uploads",
                json={
                    "parent_id": root["id"],
                    "name": "idem.bin",
                    "size_bytes": len(data),
                    "chunk_size": MB,
                },
                headers=headers,
            )
        ).json()
        digest = hashlib.sha256(data).hexdigest()
        url = f"/api/v1/uploads/{session_resp['id']}/chunks/0"

        first = await client.put(
            url, content=data, headers={**headers, "X-Chunk-SHA256": digest}
        )
        assert first.status_code == 200
        assert first.json()["status"] == "stored"

        second = await client.put(
            url, content=data, headers={**headers, "X-Chunk-SHA256": digest}
        )
        assert second.status_code == 200
        assert second.json()["status"] == "already_present"

    async def test_complete_with_missing_chunks_lists_them(self, session):
        client, headers, root = session
        session_resp = (
            await client.post(
                "/api/v1/uploads",
                json={
                    "parent_id": root["id"],
                    "name": "partial.bin",
                    "size_bytes": 2 * MB,
                    "chunk_size": MB,
                },
                headers=headers,
            )
        ).json()
        piece = os.urandom(MB)
        await client.put(
            f"/api/v1/uploads/{session_resp['id']}/chunks/0",
            content=piece,
            headers={**headers, "X-Chunk-SHA256": hashlib.sha256(piece).hexdigest()},
        )

        resp = await client.post(
            f"/api/v1/uploads/{session_resp['id']}/complete", json={}, headers=headers
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "upload_incomplete"
        assert body["extensions"]["missing_chunks"] == [1]

    async def test_wrong_whole_file_hash_is_rejected(self, session):
        client, headers, root = session
        data = os.urandom(1000)
        session_resp = (
            await client.post(
                "/api/v1/uploads",
                json={
                    "parent_id": root["id"],
                    "name": "hash.bin",
                    "size_bytes": len(data),
                    "chunk_size": MB,
                },
                headers=headers,
            )
        ).json()
        await client.put(
            f"/api/v1/uploads/{session_resp['id']}/chunks/0",
            content=data,
            headers={**headers, "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
        )
        resp = await client.post(
            f"/api/v1/uploads/{session_resp['id']}/complete",
            json={"sha256": "0" * 64},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "file_hash_mismatch"

    async def test_abort_releases_quota(self, session):
        client, headers, root = session
        before = (await client.get("/api/v1/auth/me", headers=headers)).json()["used_bytes"]

        session_resp = (
            await client.post(
                "/api/v1/uploads",
                json={
                    "parent_id": root["id"],
                    "name": "aborted.bin",
                    "size_bytes": 5 * MB,
                    "chunk_size": MB,
                },
                headers=headers,
            )
        ).json()

        # Quota is reserved at session creation, not at completion.
        during = (await client.get("/api/v1/auth/me", headers=headers)).json()["used_bytes"]
        assert during == before + 5 * MB

        aborted = await client.delete(
            f"/api/v1/uploads/{session_resp['id']}", headers=headers
        )
        assert aborted.status_code == 204

        after = (await client.get("/api/v1/auth/me", headers=headers)).json()["used_bytes"]
        assert after == before, "aborting must return the reservation"

    async def test_quota_exceeded_is_507(self, client, settings):
        body = await _register(client, "quota@example.com")
        headers = _auth(body["tokens"])
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        quota = body["user"]["quota_bytes"]

        resp = await client.post(
            "/api/v1/uploads",
            json={
                "parent_id": root["id"],
                "name": "huge.bin",
                "size_bytes": quota + 1,
                "chunk_size": MB,
            },
            headers=headers,
        )
        assert resp.status_code == 507
        assert resp.json()["code"] == "quota_exceeded"

    async def test_upload_to_another_users_folder_is_404(self, client):
        a = await _register(client, "owner@example.com")
        b = await _register(client, "attacker@example.com")
        ha, hb = _auth(a["tokens"]), _auth(b["tokens"])
        root_a = (await client.get("/api/v1/nodes/root", headers=ha)).json()

        resp = await client.post(
            "/api/v1/uploads",
            json={
                "parent_id": root_a["id"],
                "name": "intrusion.bin",
                "size_bytes": 10,
                "chunk_size": MB,
            },
            headers=hb,
        )
        assert resp.status_code == 404


class TestDownloadOverHTTP:
    @pytest.fixture
    async def uploaded(self, client):
        body = await _register(client, "download@example.com")
        headers = _auth(body["tokens"])
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        data = os.urandom(3 * MB + 777)
        result = await _upload_file(
            client, headers, name="movie.bin", data=data, parent_id=root["id"]
        )
        return client, headers, result["node"], data

    async def test_range_request_returns_206_with_content_range(self, uploaded):
        client, headers, node, data = uploaded
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content",
            headers={**headers, "Range": "bytes=100-199"},
        )
        assert resp.status_code == 206
        assert resp.content == data[100:200]
        assert resp.headers["Content-Range"] == f"bytes 100-199/{len(data)}"
        assert resp.headers["Content-Length"] == "100"

    async def test_open_ended_range(self, uploaded):
        """What a video player sends when seeking."""
        client, headers, node, data = uploaded
        start = 2 * MB
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content",
            headers={**headers, "Range": f"bytes={start}-"},
        )
        assert resp.status_code == 206
        assert resp.content == data[start:]

    async def test_suffix_range(self, uploaded):
        client, headers, node, data = uploaded
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content",
            headers={**headers, "Range": "bytes=-500"},
        )
        assert resp.status_code == 206
        assert resp.content == data[-500:]

    async def test_range_straddling_chunk_boundary(self, uploaded):
        """The boundary case where a naive implementation drops or duplicates
        bytes."""
        client, headers, node, data = uploaded
        start, end = MB - 10, MB + 10
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content",
            headers={**headers, "Range": f"bytes={start}-{end}"},
        )
        assert resp.status_code == 206
        assert resp.content == data[start : end + 1]

    async def test_unsatisfiable_range_returns_416_with_size(self, uploaded):
        client, headers, node, data = uploaded
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content",
            headers={**headers, "Range": f"bytes={len(data) + 100}-"},
        )
        assert resp.status_code == 416
        assert resp.headers["Content-Range"] == f"bytes */{len(data)}"

    async def test_multi_range_is_rejected_explicitly(self, uploaded):
        """Serving only the first range silently would be worse than a clear 416."""
        client, headers, node, data = uploaded
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content",
            headers={**headers, "Range": "bytes=0-99,200-299"},
        )
        assert resp.status_code == 416

    async def test_head_reports_size_without_body(self, uploaded):
        client, headers, node, data = uploaded
        resp = await client.head(f"/api/v1/files/{node['id']}/content", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["Content-Length"] == str(len(data))
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert resp.content == b""

    async def test_etag_and_conditional_request(self, uploaded):
        client, headers, node, data = uploaded
        first = await client.get(f"/api/v1/files/{node['id']}/content", headers=headers)
        etag = first.headers["ETag"]
        assert etag == f'"{hashlib.sha256(data).hexdigest()}"'

        # Re-opening a large file must be free when the client's copy is current.
        second = await client.get(
            f"/api/v1/files/{node['id']}/content",
            headers={**headers, "If-None-Match": etag},
        )
        assert second.status_code == 304

    async def test_content_disposition_handles_non_ascii_names(self, client):
        """A bare UTF-8 filename breaks older clients and invites header
        injection, so the name is emitted in both ASCII and RFC 5987 forms."""
        body = await _register(client, "unicode@example.com")
        headers = _auth(body["tokens"])
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        data = b"hello"
        result = await _upload_file(
            client, headers, name="گزارش سالانه.pdf", data=data, parent_id=root["id"]
        )
        resp = await client.get(
            f"/api/v1/files/{result['node']['id']}/content", headers=headers
        )
        disposition = resp.headers["Content-Disposition"]
        assert "filename*=UTF-8''" in disposition
        assert disposition.startswith("attachment")
        assert resp.content == data

    async def test_inline_disposition_for_previews(self, uploaded):
        client, headers, node, data = uploaded
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content?disposition=inline", headers=headers
        )
        assert resp.headers["Content-Disposition"].startswith("inline")

    async def test_download_requires_auth(self, uploaded):
        client, headers, node, data = uploaded
        resp = await client.get(f"/api/v1/files/{node['id']}/content")
        assert resp.status_code == 401

    async def test_cannot_download_another_users_file(self, client, uploaded):
        _, _, node, data = uploaded
        other = await _register(client, "thief@example.com")
        resp = await client.get(
            f"/api/v1/files/{node['id']}/content", headers=_auth(other["tokens"])
        )
        assert resp.status_code == 404

    async def test_folder_has_no_content(self, uploaded):
        client, headers, node, data = uploaded
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        resp = await client.get(f"/api/v1/files/{root['id']}/content", headers=headers)
        assert resp.status_code == 404


class TestUsageEndpoint:
    async def test_usage_tracks_upload_then_trash(self, client):
        body = await _register(client, "usage@example.com")
        headers = _auth(body["tokens"])
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        data = os.urandom(MB)
        result = await _upload_file(
            client, headers, name="counted.bin", data=data, parent_id=root["id"]
        )

        usage = (await client.get("/api/v1/me/usage", headers=headers)).json()
        assert usage["used_bytes"] == len(data)
        assert usage["file_count"] == 1

        # Trashing frees quota, so the count drops...
        await client.delete(f"/api/v1/nodes/{result['node']['id']}", headers=headers)
        after = (await client.get("/api/v1/me/usage", headers=headers)).json()
        assert after["used_bytes"] == 0
        # ...but the bytes are still on Telegram until they are purged, which the
        # trash figure reports honestly.
        assert after["trashed_bytes"] == len(data)

    async def test_purge_frees_trashed_bytes_from_the_node_tree(self, client):
        body = await _register(client, "purge@example.com")
        headers = _auth(body["tokens"])
        root = (await client.get("/api/v1/nodes/root", headers=headers)).json()
        data = os.urandom(4096)
        result = await _upload_file(
            client, headers, name="purge.bin", data=data, parent_id=root["id"]
        )
        node_id = result["node"]["id"]

        await client.delete(f"/api/v1/nodes/{node_id}", headers=headers)
        purged = await client.delete(
            f"/api/v1/nodes/{node_id}?purge=true", headers=headers
        )
        assert purged.status_code == 204

        # The node is gone entirely, so it cannot be fetched.
        gone = await client.get(f"/api/v1/nodes/{node_id}", headers=headers)
        assert gone.status_code == 404