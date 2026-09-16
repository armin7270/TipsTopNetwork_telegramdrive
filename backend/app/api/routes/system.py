"""System endpoints: health, readiness, usage, storage pools.

The split between *liveness* and *readiness* matters operationally. A process that
is alive but has no healthy Telegram session is not ready: it cannot store
anything, so a load balancer should stop sending it traffic. Folding those into a
single endpoint would cause a rolling restart during a FloodWait storm, which is
exactly when the accounts least need extra login traffic.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import get_settings
from app.core.errors import forbidden
from app.schemas import (
    HealthResponse,
    ReadyResponse,
    StoragePoolResponse,
    UsageResponse,
    UserResponse,
)
from app.services.auth import Principal, current_user, require_admin

router = APIRouter(tags=["system"])

# Account-scoped endpoints live under the versioned API prefix; the probe and
# build endpoints stay at the root because orchestrators and operators expect them
# there regardless of API version.
api_router = APIRouter(tags=["system"])

VERSION = "0.1.0"


@router.get("/healthz", response_model=HealthResponse, tags=["system"],
            summary="Liveness probe")
async def healthz(request: Request) -> HealthResponse:
    """Liveness only: is the process up and serving?

    Deliberately does not touch the database or Telegram. A liveness probe that
    fails on a dependency outage causes the orchestrator to restart healthy
    processes, turning a partial degradation into a total one.
    """
    return HealthResponse(status="ok", version=VERSION, checks={"process": "ok"})


@router.get("/readyz", response_model=ReadyResponse, tags=["system"],
            summary="Readiness probe")
async def readyz(request: Request, response: Response) -> ReadyResponse:
    """Readiness: can this instance actually serve requests?

    Checks the database, because without it nothing can be read or written — the
    chunks live in Telegram, but their index does not. Telegram session health is
    reported but does not fail the probe: a FloodWait is transient and must not
    pull an instance out of rotation.
    """
    checks: dict[str, str] = {}
    ready = True

    try:
        await request.app.state.repo.ping()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        checks["database"] = f"error: {type(exc).__name__}"
        ready = False

    pool = getattr(request.app.state, "pool", None)
    healthy = total = 0
    detail: str | None = None
    if pool is not None:
        snapshot = pool.snapshot()
        healthy = int(snapshot["healthy"])
        total = int(snapshot["pool_size"])
        checks["telegram_sessions"] = f"{healthy}/{total} healthy"
        if total and healthy == 0:
            # Reported as degraded, not unready: the instance can still serve
            # downloads from replicas and answer management requests.
            detail = (
                "No Telegram session is currently healthy. Uploads will fail until "
                "one recovers; check FloodWait state and session validity."
            )

    checks["storage"] = "ok" if healthy or not total else "degraded"

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        status="ready" if ready else "not_ready",
        database=checks["database"],
        sessions_healthy=healthy,
        sessions_total=total,
        detail=detail,
    )


@router.get("/version", tags=["system"], summary="Build information")
async def version() -> dict[str, str]:
    settings = get_settings()
    return {
        "version": VERSION,
        "chunk_size_default": str(settings.storage_chunk_size_bytes),
        "max_concurrent_uploads": str(settings.max_concurrent_uploads),
        "memory_ceiling_bytes": str(settings.memory_ceiling_bytes),
    }


@api_router.get("/me/usage", response_model=UsageResponse,
                summary="Quota and usage breakdown")
async def get_usage(
    request: Request, principal: Principal = Depends(current_user)
) -> UsageResponse:
    repo = request.app.state.repo
    user = await repo.get_user(principal.user_id)
    if user is None:
        raise forbidden("Account no longer exists")

    usage = await repo.usage_breakdown(principal.user_id)
    quota = int(user.get("quota_bytes") or 0)
    used = int(user.get("used_bytes") or 0)
    trashed = int(usage.get("trashed_bytes") or 0)

    return UsageResponse(
        quota_bytes=quota,
        used_bytes=used,
        trashed_bytes=trashed,
        available_bytes=max(0, quota - used),
        file_count=int(usage.get("file_count") or 0),
        folder_count=int(usage.get("folder_count") or 0),
    )


@router.get(
    "/admin/storage-pools",
    response_model=list[StoragePoolResponse],
    tags=["admin"],
    summary="Storage pool roster",
    description=(
        "Operational visibility into which private Telegram channels are in use and "
        "how full they are. Administrator only: the channel ids here are the "
        "physical location of every stored byte."
    ),
)
async def list_storage_pools(
    request: Request, _: Principal = Depends(require_admin)
) -> list[StoragePoolResponse]:
    pools = await request.app.state.repo.list_storage_pools()
    return [
        StoragePoolResponse(
            id=str(p["id"]),
            label=p["label"],
            telegram_channel_id=int(p["telegram_channel_id"]),
            session_label=p.get("session_label"),
            state=p.get("state", "active"),
            chunk_count=int(p.get("chunk_count") or 0),
            stored_bytes=int(p.get("stored_bytes") or 0),
            priority=int(p.get("priority") or 0),
        )
        for p in pools
    ]


@router.get(
    "/admin/sessions",
    tags=["admin"],
    summary="Telegram session health",
    description=(
        "Per-session rate-limit and error state. This is the endpoint to check when "
        "uploads start failing: it shows which accounts are gated, for how long, and "
        "whether any have been quarantined by the circuit breaker."
    ),
)
async def session_health(
    request: Request, _: Principal = Depends(require_admin)
) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return {"pool_size": 0, "sessions": [], "note": "session pool is not running"}
    return pool.snapshot()