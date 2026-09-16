"""Telegram session pool.

The pool is the single most important availability component in the system,
because the scarce resource is not CPU or bandwidth but **Telegram's tolerance
for this account fleet**. Its job is to make sure no individual account is ever
pushed hard enough to be restricted.

Responsibilities:

* Hold N connected MTProto clients, one per authorised account.
* Hand out the *least-loaded healthy* session for each operation.
* Enforce a hard FloodWait gate: a gated session receives no traffic until
  Telegram's instruction expires. There is no bypass.
* Quarantine sessions that repeatedly fail, using an escalating ladder.
* Cap global in-flight storage operations, independent of per-session limits.

A subtle but load-bearing design point: sessions are recovered
**automatically** once their gate expires. A naive implementation marks a
session permanently dead on the first FloodWait, which shrinks the pool until
throughput collapses. Instead, ``floodwait`` is a *temporary* state and
``quarantined`` is the escalating one.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.telegram.base import (
    FloodWaitError,
    PermanentStorageError,
    SessionUnavailableError,
    StorageBackend,
    StorageError,
)
from app.telegram.ratelimit import (
    AdaptiveConcurrencyLimiter,
    CircuitBreaker,
    FloodWaitGate,
    TokenBucket,
)

log = logging.getLogger(__name__)


@dataclass
class PooledSession:
    """One authorised Telegram account and its live health state."""

    id: str
    label: str
    client: Any
    dc_id: int | None = None
    phone_masked: str | None = None

    bucket: TokenBucket = field(default_factory=lambda: TokenBucket(8, 0.5))
    in_flight: int = 0
    total_requests: int = 0
    total_errors: int = 0
    total_uploaded_bytes: int = 0
    total_downloaded_bytes: int = 0
    last_used_at: float = 0.0
    # Set when the pool decides this session should not be used; the gate and
    # breaker hold the authoritative timers.
    disabled: bool = False
    disabled_reason: str = ""

    @property
    def display(self) -> str:
        return f"{self.label}({self.id[:8]})"


@dataclass
class Lease:
    """A right to use one session for one operation.

    Releasing is **complete and self-contained**: :meth:`release` returns both the
    per-session in-flight count *and* the global concurrency slot. An earlier
    design kept those in two different methods, which meant a caller who used
    ``lease.release()`` silently leaked a global slot until the pool deadlocked.
    A resource that must be returned should be returnable in exactly one way.

    Idempotent, so a ``finally`` block plus an explicit release cannot
    double-release.
    """

    session: PooledSession
    limiter: "AdaptiveConcurrencyLimiter | None" = None
    _released: bool = False

    async def release(self, *, uploaded_bytes: int = 0, downloaded_bytes: int = 0) -> None:
        if self._released:
            return
        self._released = True

        self.session.in_flight = max(0, self.session.in_flight - 1)
        self.session.total_uploaded_bytes += uploaded_bytes
        self.session.total_downloaded_bytes += downloaded_bytes
        self.session.last_used_at = time.monotonic()

        # Return the global slot exactly once, and only after the in-flight
        # count has dropped, so selection never sees a stale load figure.
        limiter, self.limiter = self.limiter, None
        if limiter is not None:
            await limiter.__aexit__(None, None, None)

    async def __aenter__(self) -> "Lease":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()


class SessionPool:
    """Least-loaded selection over a set of MTProto accounts."""

    def __init__(
        self,
        *,
        backend: StorageBackend,
        settings: Settings,
        floodwait_gate: FloodWaitGate | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.gate = floodwait_gate or FloodWaitGate()
        self.breaker = breaker or CircuitBreaker()
        self._sessions: dict[str, PooledSession] = {}
        self._lock = asyncio.Lock()
        self._waiting = asyncio.Condition(self._lock)

        # Global ceiling. Per-session limits alone are insufficient: a large
        # fleet hammering Telegram concurrently is itself a suspicious pattern.
        self.limiter = AdaptiveConcurrencyLimiter(
            max(1, settings.telegram_session_pool_size * 2)
        )
        self.acquire_timeouts = 0
        self.floodwait_count = 0

    # -- registration -----------------------------------------------------

    async def add_session(
        self,
        *,
        session_id: str,
        label: str,
        client: Any,
        dc_id: int | None = None,
        phone_masked: str | None = None,
    ) -> PooledSession:
        """Register a connected client.

        The token bucket rate is derived from ``SESSION_REQUESTS_PER_MINUTE``,
        with the per-session concurrency equal to that rate divided by four. The
        default of 15 requests/minute sits comfortably below the ~20
        messages/minute/chat ceiling so that a single session can serve a chat
        without immediately tripping it.
        """
        per_minute = self.settings.session_requests_per_minute
        bucket = TokenBucket(
            capacity=max(2.0, per_minute / 4.0),
            refill_per_second=per_minute / 60.0,
        )

        session = PooledSession(
            id=session_id,
            label=label,
            client=client,
            dc_id=dc_id,
            phone_masked=phone_masked,
            bucket=bucket,
        )

        async with self._lock:
            self._sessions[session_id] = session
            self._waiting.notify_all()

        if hasattr(self.backend, "register_client"):
            self.backend.register_client(label, client)  # type: ignore[attr-defined]

        log.info(
            "session pool: registered %s at %s req/min (pool size now %d)",
            session.display,
            per_minute,
            len(self._sessions),
        )
        return session

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            if hasattr(self.backend, "unregister_client"):
                self.backend.unregister_client(session.label)  # type: ignore[attr-defined]
            log.warning("session pool: removed %s", session.display)

    # -- health -----------------------------------------------------------

    def _is_selectable(self, session: PooledSession) -> bool:
        if session.disabled:
            return False
        if not self.gate.is_clear(session.label):
            return False
        if not self.breaker.is_available(session.label):
            return False
        return True

    def healthy_count(self) -> int:
        return sum(1 for s in self._sessions.values() if self._is_selectable(s))

    def snapshot(self) -> dict[str, Any]:
        """Pool state for ``GET /readyz`` and the metrics exporter."""
        sessions = []
        for s in self._sessions.values():
            sessions.append(
                {
                    "id": s.id,
                    "label": s.label,
                    "dc_id": s.dc_id,
                    "healthy": self._is_selectable(s),
                    "disabled": s.disabled,
                    "disabled_reason": s.disabled_reason,
                    "in_flight": s.in_flight,
                    "requests": s.total_requests,
                    "errors": s.total_errors,
                    "tokens_available": round(s.bucket.available, 3),
                    "floodwait_remaining": round(self.gate.remaining(s.label), 1),
                    "quarantine_remaining": round(self.breaker.remaining(s.label), 1),
                    "uploaded_bytes": s.total_uploaded_bytes,
                    "downloaded_bytes": s.total_downloaded_bytes,
                }
            )
        return {
            "pool_size": len(self._sessions),
            "healthy": self.healthy_count(),
            "in_flight": self.limiter.in_flight,
            "peak_in_flight": self.limiter.peak_in_flight,
            "floodwait_events": self.gate.events,
            "floodwait_seconds_total": round(self.gate.total_floodwait_seconds, 1),
            "acquire_timeouts": self.acquire_timeouts,
            "sessions": sessions,
        }

    # -- acquisition ------------------------------------------------------

    async def acquire(
        self,
        *,
        timeout: float = 60.0,
        require_healthy: bool = True,
    ) -> Lease:
        """Acquire the least-loaded healthy session.

        Selection prefers fewest in-flight operations, then most available
        tokens. Both matter: in-flight count reflects current contention, while
        token availability reflects recent rate history. Ties are broken randomly
        so that a fleet of identically-loaded sessions does not develop a
        deterministic "always pick session 0" bias — that would concentrate load
        on one account and eventually get it restricted.

        The global limiter is acquired *first*, then a session. Acquiring in the
        other order would let a caller hold a session lease while queued behind
        the global ceiling, which makes per-session in-flight counts misleading.
        """
        deadline = time.monotonic() + timeout

        await self.limiter.__aenter__()
        try:
            while True:
                async with self._lock:
                    candidates = [
                        s for s in self._sessions.values() if self._is_selectable(s)
                    ]

                    if candidates:
                        # Prefer sessions with a token ready right now, so we
                        # throttle at selection time rather than sleeping inside
                        # a lease.
                        ready = [s for s in candidates if s.bucket.available >= 1.0]
                        pool = ready or candidates
                        min_in_flight = min(s.in_flight for s in pool)
                        least_loaded = [s for s in pool if s.in_flight == min_in_flight]
                        best_tokens = max(s.bucket.available for s in least_loaded)
                        finalists = [
                            s for s in least_loaded if s.bucket.available >= best_tokens * 0.99
                        ]
                        chosen = random.choice(finalists)
                        chosen.in_flight += 1
                        chosen.total_requests += 1
                        return Lease(session=chosen, limiter=self.limiter)

                    if not require_healthy and self._sessions:
                        # Degraded mode: everything is gated, so pick whichever
                        # session frees up soonest and wait for it.
                        soonest = min(
                            self._sessions.values(),
                            key=lambda s: self.gate.remaining(s.label),
                        )
                    else:
                        soonest = None

                    # Fail fast when waiting cannot possibly help. A request
                    # that blocks for the full acquire timeout and *then* fails
                    # is the worst outcome: the client waits a minute and a half
                    # to learn what was already knowable immediately.
                    #
                    # The test is whether the earliest possible recovery falls
                    # outside the deadline. Disabled or quarantined sessions have
                    # no recovery timer at all, and a rate-limited session whose
                    # gate outlasts the deadline is equally hopeless.
                    futile_reason: str | None = None
                    if self._sessions:
                        usable = [s for s in self._sessions.values() if not s.disabled]
                        if not usable:
                            futile_reason = (
                                "every Telegram session is disabled; operator "
                                "action is required (see GET /readyz)"
                            )
                        else:
                            soonest_recovery = min(
                                max(
                                    self.gate.remaining(s.label),
                                    self.breaker.remaining(s.label),
                                )
                                for s in usable
                            )
                            if soonest_recovery > deadline - time.monotonic():
                                futile_reason = (
                                    "all Telegram sessions are rate-limited; "
                                    f"soonest recovery in {soonest_recovery:.0f}s, "
                                    f"request deadline is {timeout:.0f}s. "
                                    "This is Telegram FloodWait backpressure — do not "
                                    "retry aggressively; back off and retry later."
                                )

                    if futile_reason:
                        self.acquire_timeouts += 1
                        raise SessionUnavailableError(futile_reason)

                if time.monotonic() >= deadline:
                    self.acquire_timeouts += 1
                    raise SessionUnavailableError(
                        "no healthy Telegram session available within "
                        f"{timeout:.0f}s (pool={len(self._sessions)}, "
                        f"healthy={self.healthy_count()})"
                    )

                if soonest is not None:
                    wait_for = min(
                        max(1.0, self.gate.remaining(soonest.label)),
                        deadline - time.monotonic(),
                    )
                else:
                    wait_for = min(1.0, deadline - time.monotonic())

                # Bounded sleep rather than a condition wait: the states that
                # make a session selectable again (a gate expiring) are time
                # based, not event based, so nobody would notify us.
                await asyncio.sleep(max(0.05, wait_for))
        except BaseException:
            await self.limiter.__aexit__(None, None, None)
            raise

    async def release(self, lease: Lease, **kwargs: Any) -> None:
        """Release a lease.

        Retained for symmetry with :meth:`acquire`; it simply delegates, because
        a lease now owns everything that needs returning.
        """
        await lease.release(**kwargs)

    # -- outcome reporting ------------------------------------------------

    async def report_floodwait(self, session: PooledSession, seconds: int) -> None:
        """Record Telegram's backpressure instruction.

        Waiting exactly the instructed duration is mandatory. Shortening it makes
        the next penalty larger, and escalation is the documented route to a ban.
        If the wait exceeds ``FLOODWAIT_MAX_SLEEP_SECONDS`` the session is
        quarantined rather than merely gated, so a heavily-penalised account is
        taken out of rotation entirely instead of stalling requests for minutes.
        """
        self.floodwait_count += 1
        await self.gate.gate(session.label, seconds)
        quarantine = self.breaker.record_floodwait(session.label, seconds)

        if seconds > self.settings.floodwait_max_sleep_seconds:
            session.disabled = True
            session.disabled_reason = (
                f"floodwait {seconds}s exceeds FLOODWAIT_MAX_SLEEP_SECONDS "
                f"({self.settings.floodwait_max_sleep_seconds}s)"
            )
            log.error(
                "session pool: %s disabled for excessive floodwait (%ss)",
                session.display,
                seconds,
            )
        elif quarantine:
            log.warning(
                "session pool: %s quarantined for %.0fs after repeated floodwaits",
                session.display,
                quarantine,
            )

    async def report_failure(self, session: PooledSession, exc: BaseException) -> None:
        session.total_errors += 1
        quarantine = self.breaker.record_failure(session.label, reason=type(exc).__name__)
        if quarantine:
            log.warning(
                "session pool: %s quarantined for %.0fs after repeated failures",
                session.display,
                quarantine,
            )

    async def report_success(
        self,
        session: PooledSession,
        *,
        uploaded_bytes: int = 0,
        downloaded_bytes: int = 0,
    ) -> None:
        self.breaker.record_success(session.label)
        session.total_uploaded_bytes += uploaded_bytes
        session.total_downloaded_bytes += downloaded_bytes

    # -- retrying execution helper ---------------------------------------

    async def execute_with_retry(
        self,
        operation_name: str,
        fn: Any,
        *,
        max_attempts: int = 4,
        acquire_timeout: float = 60.0,
        exclude_labels: set[str] | None = None,
    ) -> Any:
        """Run ``fn(session)``, rotating sessions on FloodWait or failure.

        This is the heart of the ban-avoidance strategy, and the policy is:

        * **FloodWait** → gate that session for exactly the instructed time, then
          retry on a *different* session. Never sleep and retry on the same one,
          because that is what converts a warning into a ban.
        * **Other failures** → count them; after the breaker threshold the session
          is quarantined and the retry moves elsewhere.
        * **Success** → reset the consecutive-failure counter.

        Retries are bounded. Unlimited retry against a platform that is telling
        you to stop is precisely the behaviour being defended against.
        """
        exclude = set(exclude_labels or ())
        last_error: BaseException | None = None

        for attempt in range(max_attempts):
            lease = await self.acquire(timeout=acquire_timeout)
            session = lease.session
            exclude.add(session.label)

            try:
                result = await fn(session)
                await self.report_success(session)
                return result

            except FloodWaitError as exc:
                await self.report_floodwait(session, exc.seconds)
                last_error = exc
                log.info(
                    "%s: floodwait %ss on %s, rotating (attempt %d/%d)",
                    operation_name,
                    exc.seconds,
                    session.display,
                    attempt + 1,
                    max_attempts,
                )

            except PermanentStorageError as exc:
                # The fault is with the stored object or the request, not the
                # account. Rotating sessions would be pointless, and blaming the
                # session would quarantine a healthy account — so the failure is
                # propagated immediately without touching the breaker.
                log.warning(
                    "%s: permanent failure (%s) on %s; not retrying and not "
                    "penalising the session",
                    operation_name,
                    type(exc).__name__,
                    session.display,
                )
                raise

            except StorageError as exc:
                await self.report_failure(session, exc)
                last_error = exc
                log.warning(
                    "%s: %s on %s (attempt %d/%d)",
                    operation_name,
                    type(exc).__name__,
                    session.display,
                    attempt + 1,
                    max_attempts,
                )

            except Exception as exc:  # noqa: BLE001 - unexpected, still rotate
                await self.report_failure(session, exc)
                last_error = exc
                log.exception("%s: unexpected error on %s", operation_name, session.display)

            finally:
                await self.release(lease)

            if attempt + 1 < max_attempts:
                # Full jitter avoids a synchronised retry storm across workers.
                await asyncio.sleep(
                    random.uniform(0.2, min(5.0, 0.5 * (2**attempt)))
                )

        raise SessionUnavailableError(
            f"{operation_name} failed after {max_attempts} attempts across "
            f"{len(exclude)} session(s); last error: {last_error}"
        ) from last_error

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Connect every registered client and verify authorisation.

        A session that fails its probe is registered but marked disabled, so an
        operator sees it in ``/readyz`` rather than having it silently vanish.

        Clients that do not expose Telethon's surface — the in-memory store's
        placeholders, for instance — are skipped rather than probed. Testing for
        the capability is cleaner than maintaining a separate code path for tests,
        and it means the fake backend exercises exactly the same pool logic.
        """
        for session in list(self._sessions.values()):
            client = session.client
            if not hasattr(client, "connect") or not hasattr(client, "get_me"):
                log.debug(
                    "session pool: %s has no Telethon client; skipping probe",
                    session.display,
                )
                continue
            try:
                await client.connect()
                me = await client.get_me()
                session.dc_id = session.dc_id or getattr(
                    getattr(client, "session", None), "dc_id", None
                )
                log.info(
                    "session pool: connected %s (user id %s)",
                    session.display,
                    getattr(me, "id", "?"),
                )
            except Exception as exc:  # noqa: BLE001
                session.disabled = True
                session.disabled_reason = f"connect failed: {exc}"
                log.error("session pool: %s failed to connect: %s", session.display, exc)

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            client = session.client
            if not hasattr(client, "disconnect"):
                continue
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                log.debug("session pool: error disconnecting %s", session.display, exc_info=True)