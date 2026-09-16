"""Rate limiting, FloodWait gating, and circuit breaking.

The premise of this module: **Telegram's limits are the scarce resource**, not
CPU, memory, or bandwidth. Throughput is therefore governed by admission control
rather than by adding parallelism. Every mechanism here exists to spend the
platform's goodwill at a sustainable rate.

Three cooperating controls:

1. :class:`TokenBucket` — smooths request rate per session. Prevents a burst of
   concurrent chunk uploads from tripping the per-chat message limit.
2. :class:`FloodWaitGate` — a hard, non-negotiable gate. When Telegram says
   "wait N seconds", the session is removed from selection for exactly N
   seconds. There is no override path, by design.
3. :class:`CircuitBreaker` — escalating quarantine. Repeated FloodWaits or
   failures on one session put it on the bench for progressively longer periods,
   which prevents a marginal account from being hammered into a ban.

Jitter is applied everywhere. Deterministic backoff across N sessions produces a
thundering herd that re-trips the same limit simultaneously; full jitter spreads
the retries out.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


def full_jitter(base_seconds: float, attempt: int, *, cap_seconds: float) -> float:
    """Exponential backoff with *full jitter*.

    Returns a uniform random value in ``[0, min(cap, base * 2**attempt)]``.

    Full jitter rather than "exponential + small random offset": the point is to
    decorrelate a fleet of retries, and full jitter does that maximally. The cost
    is that an unlucky retry can be near-immediate, which is acceptable because
    the circuit breaker — not the backoff — is what protects the account.
    """
    ceiling = min(cap_seconds, base_seconds * (2 ** max(0, attempt)))
    return random.uniform(0.0, ceiling)


@dataclass
class TokenBucket:
    """Leaky-bucket rate limiter.

    Starts **full**, then refills continuously. Starting full is intentional: a
    cold session should be allowed its initial allowance (which is the legitimate
    steady state), while sustained pressure is what the bucket actually throttles.
    """

    capacity: float
    refill_per_second: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, repr=False)

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("bucket capacity and refill rate must be positive")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self.last_refill = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until ``tokens`` are available.

        Returns the number of seconds spent waiting, so callers can record real
        throttling pressure in metrics. A non-zero return is the signal that this
        session is saturated and the pool should prefer another.
        """
        if tokens > self.capacity:
            raise ValueError(f"cannot acquire {tokens} tokens from a bucket of {self.capacity}")

        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return waited
                deficit = tokens - self.tokens
                sleep_for = deficit / self.refill_per_second
                # Cap each hop so cancellation stays responsive on long waits.
                sleep_for = min(sleep_for, 1.0)
                self._lock.release()
                try:
                    await asyncio.sleep(sleep_for)
                    waited += sleep_for
                finally:
                    await self._lock.acquire()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking variant used by the pool's session selection."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    @property
    def available(self) -> float:
        self._refill()
        return self.tokens


class FloodWaitGate:
    """Hard time gate enforcing Telegram's FloodWait instruction.

    Two rules, both non-negotiable:

    * A session gated until ``T`` receives **no traffic** before ``T``.
    * The gate is never shortened. Waiting less than instructed makes the next
      penalty larger, and escalation is how accounts get banned.
    """

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.total_floodwait_seconds = 0.0
        self.events = 0

    async def gate(self, label: str, seconds: int) -> float:
        """Gate ``label`` for ``seconds``; returns the absolute release time."""
        async with self._lock:
            release = time.monotonic() + seconds
            # Extend, never shorten: an overlapping longer wait must win.
            self._until[label] = max(self._until.get(label, 0.0), release)
            self.total_floodwait_seconds += seconds
            self.events += 1
            log.warning(
                "floodwait: gating session %s for %ss (total events=%d)",
                label,
                seconds,
                self.events,
            )
            return release

    def remaining(self, label: str) -> float:
        """Seconds until ``label`` may be used again (0 when clear)."""
        release = self._until.get(label, 0.0)
        remaining = release - time.monotonic()
        return max(0.0, remaining)

    def is_clear(self, label: str) -> bool:
        return self.remaining(label) <= 0.0

    def clear(self, label: str) -> None:
        """Lift the gate early.

        Only for operator action after confirming the account is healthy. The
        application never calls this automatically.
        """
        self._until.pop(label, None)


@dataclass
class BreakerState:
    """Per-session circuit breaker, mirroring ``telegram_sessions`` columns."""

    consecutive_failures: int = 0
    quarantine_until: float = 0.0
    total_failures: int = 0
    total_floodwaits: int = 0

    def is_quarantined(self) -> bool:
        return self.quarantine_until > time.monotonic()


class CircuitBreaker:
    """Escalating quarantine keyed by session label.

    Thresholds are deliberately low. A session that trips FloodWait twice or
    fails three times is not worth defending: the correct action is to bench it
    and let healthy accounts carry the traffic, because a banned account costs
    far more than a temporarily reduced pool.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        floodwait_threshold: int = 2,
        base_quarantine_seconds: float = 60.0,
        max_quarantine_seconds: float = 3600.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.floodwait_threshold = floodwait_threshold
        self.base_quarantine_seconds = base_quarantine_seconds
        self.max_quarantine_seconds = max_quarantine_seconds
        self._states: dict[str, BreakerState] = {}

    def state(self, label: str) -> BreakerState:
        return self._states.setdefault(label, BreakerState())

    def is_available(self, label: str) -> bool:
        return not self.state(label).is_quarantined()

    def remaining(self, label: str) -> float:
        state = self.state(label)
        return max(0.0, state.quarantine_until - time.monotonic())

    def _quarantine(self, label: str, reason: str) -> float:
        state = self.state(label)
        # Escalation ladder: each offence benches the session for longer, capped
        # so an account can always eventually be retried after operator review.
        exponent = max(0, state.total_failures - 1)
        duration = min(
            self.max_quarantine_seconds,
            self.base_quarantine_seconds * (2**exponent),
        )
        duration = min(
            self.max_quarantine_seconds,
            duration * random.uniform(0.8, 1.2),  # jitter avoids lockstep recovery
        )
        state.quarantine_until = time.monotonic() + duration
        log.warning(
            "circuit breaker: quarantining session %s for %.1fs (%s, failures=%d)",
            label,
            duration,
            reason,
            state.total_failures,
        )
        return duration

    def record_success(self, label: str) -> None:
        """A success resets the consecutive counter.

        ``total_failures`` is deliberately *not* reset: an account that has a
        history of problems should escalate faster on relapse, and that history
        is also what an operator reviews before deciding to retire it.
        """
        state = self.state(label)
        state.consecutive_failures = 0

    def record_floodwait(self, label: str, seconds: int) -> float | None:
        state = self.state(label)
        state.total_floodwaits += 1
        state.total_failures += 1
        if state.total_floodwaits >= self.floodwait_threshold:
            return self._quarantine(label, f"floodwait x{state.total_floodwaits}")
        return None

    def record_failure(self, label: str, *, reason: str = "error") -> float | None:
        state = self.state(label)
        state.consecutive_failures += 1
        state.total_failures += 1
        if state.consecutive_failures >= self.failure_threshold:
            return self._quarantine(label, reason)
        return None

    def reset(self, label: str) -> None:
        """Full operator reset, used after manual account review."""
        self._states.pop(label, None)

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        now = time.monotonic()
        return {
            label: {
                "consecutive_failures": state.consecutive_failures,
                "total_failures": state.total_failures,
                "total_floodwaits": state.total_floodwaits,
                "quarantined": state.quarantine_until > now,
                "quarantine_remaining_seconds": max(0.0, state.quarantine_until - now),
            }
            for label, state in self._states.items()
        }


class AdaptiveConcurrencyLimiter:
    """A global ceiling on simultaneous in-flight storage operations.

    Independent of per-session limits: even with many healthy sessions, a large
    burst of concurrent requests across the fleet is itself a pattern that draws
    attention. This caps the aggregate.
    """

    def __init__(self, limit: int) -> None:
        self._semaphore = asyncio.Semaphore(limit)
        self.limit = limit
        self.in_flight = 0
        self.peak_in_flight = 0

    async def __aenter__(self) -> "AdaptiveConcurrencyLimiter":
        await self._semaphore.acquire()
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.in_flight -= 1
        self._semaphore.release()