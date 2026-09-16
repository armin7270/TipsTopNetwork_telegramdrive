"""Rate limiting, FloodWait gating, circuit breaking, and session rotation.

These tests are the executable form of the ban-avoidance policy. The behaviour
they pin down is what keeps the account fleet alive:

* FloodWait is honoured exactly, never shortened.
* Traffic rotates to a *different* session rather than hammering the gated one.
* Sessions recover automatically after their gate expires.
* A permanently-deleted message does not quarantine a healthy account.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.core.config import Settings
from app.telegram.base import (
    FloodWaitError,
    MessageNotFoundError,
    SessionUnavailableError,
)
from app.telegram.fake_store import InMemoryStore
from app.telegram.pool import SessionPool
from app.telegram.ratelimit import (
    CircuitBreaker,
    FloodWaitGate,
    TokenBucket,
    full_jitter,
)


class TestTokenBucket:
    async def test_starts_full_and_allows_immediate_use(self):
        bucket = TokenBucket(capacity=5, refill_per_second=1)
        assert bucket.available == 5
        assert bucket.try_acquire(5)
        assert not bucket.try_acquire(1)

    async def test_refills_over_time(self):
        bucket = TokenBucket(capacity=10, refill_per_second=100)
        bucket.try_acquire(10)
        await asyncio.sleep(0.05)
        assert bucket.available > 0

    async def test_acquire_waits_and_reports_wait(self):
        bucket = TokenBucket(capacity=1, refill_per_second=20)
        bucket.try_acquire(1)
        waited = await bucket.acquire(1)
        assert waited > 0

    async def test_never_exceeds_capacity(self):
        bucket = TokenBucket(capacity=3, refill_per_second=1000)
        await asyncio.sleep(0.02)
        assert bucket.available <= 3

    async def test_acquire_more_than_capacity_is_rejected(self):
        bucket = TokenBucket(capacity=2, refill_per_second=1)
        with pytest.raises(ValueError):
            await bucket.acquire(3)

    def test_rejects_invalid_construction(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_per_second=1)
        with pytest.raises(ValueError):
            TokenBucket(capacity=1, refill_per_second=0)


class TestFloodWaitGate:
    async def test_gate_blocks_traffic(self):
        gate = FloodWaitGate()
        assert gate.is_clear("a")
        await gate.gate("a", 60)
        assert not gate.is_clear("a")
        assert gate.remaining("a") > 55

    async def test_gate_is_per_session(self):
        gate = FloodWaitGate()
        await gate.gate("a", 60)
        assert not gate.is_clear("a")
        assert gate.is_clear("b"), "gating one session must not affect another"

    async def test_overlapping_gate_extends_never_shortens(self):
        """A longer instruction must win: shortening a FloodWait is exactly the
        behaviour that escalates penalties."""
        gate = FloodWaitGate()
        await gate.gate("a", 60)
        await gate.gate("a", 120)
        assert gate.remaining("a") > 110

        await gate.gate("a", 5)
        assert gate.remaining("a") > 100, "a shorter wait must not shorten the gate"

    async def test_events_and_totals_are_counted_for_metrics(self):
        gate = FloodWaitGate()
        await gate.gate("a", 30)
        await gate.gate("b", 20)
        assert gate.events == 2
        assert gate.total_floodwait_seconds == 50

    async def test_expiry_clears_naturally(self):
        gate = FloodWaitGate()
        await gate.gate("a", 0)
        assert gate.is_clear("a")

    async def test_manual_clear_is_available_for_operators(self):
        gate = FloodWaitGate()
        await gate.gate("a", 999)
        gate.clear("a")
        assert gate.is_clear("a")


class TestCircuitBreaker:
    def test_quarantines_after_failure_threshold(self):
        breaker = CircuitBreaker(failure_threshold=3, base_quarantine_seconds=10)
        assert breaker.is_available("a")
        breaker.record_failure("a")
        breaker.record_failure("a")
        assert breaker.is_available("a"), "below threshold must stay available"
        breaker.record_failure("a")
        assert not breaker.is_available("a")

    def test_success_resets_consecutive_failures(self):
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure("a")
        breaker.record_failure("a")
        breaker.record_success("a")
        breaker.record_failure("a")
        breaker.record_failure("a")
        assert breaker.is_available("a"), "consecutive counter should have reset"

    def test_floodwait_threshold_trips_quarantine(self):
        breaker = CircuitBreaker(floodwait_threshold=2, base_quarantine_seconds=10)
        assert breaker.record_floodwait("a", 5) is None
        assert breaker.record_floodwait("a", 5) is not None
        assert not breaker.is_available("a")

    def test_quarantine_escalates(self):
        """An account with a history should be benched longer on relapse."""
        breaker = CircuitBreaker(
            failure_threshold=1, base_quarantine_seconds=10, max_quarantine_seconds=1000
        )
        breaker.record_failure("a")
        first = breaker.remaining("a")
        breaker.record_failure("a")
        second = breaker.remaining("a")
        assert second > first

    def test_quarantine_is_capped(self):
        breaker = CircuitBreaker(
            failure_threshold=1, base_quarantine_seconds=100, max_quarantine_seconds=50
        )
        breaker.record_failure("a")
        assert breaker.remaining("a") <= 60  # cap plus jitter allowance

    def test_isolation_between_sessions(self):
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure("a")
        assert not breaker.is_available("a")
        assert breaker.is_available("b")

    def test_snapshot_reports_state(self):
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure("a", reason="test")
        snap = breaker.snapshot()
        assert "a" in snap
        assert snap["a"]["quarantined"] is True


class TestFullJitter:
    def test_within_bounds(self):
        for attempt in range(6):
            for _ in range(50):
                value = full_jitter(base_seconds=1.0, attempt=attempt, cap_seconds=30.0)
                assert 0.0 <= value <= min(30.0, 1.0 * (2**attempt))

    def test_cap_is_respected(self):
        for _ in range(50):
            assert full_jitter(10.0, 10, cap_seconds=5.0) <= 5.0

    def test_is_randomised(self):
        """Decorrelating retries is the point: a fixed backoff across a fleet
        re-trips the same limit simultaneously."""
        values = {full_jitter(1.0, 3, cap_seconds=30.0) for _ in range(50)}
        assert len(values) > 40


class TestSessionPool:
    async def test_least_loaded_selection(self, pool):
        lease_a = await pool.acquire()
        lease_b = await pool.acquire()
        # Two sessions, two leases: both should be distinct before either is
        # reused, because letting one session carry everything concentrates load.
        assert lease_a.session.id != lease_b.session.id
        await lease_a.release()
        await lease_b.release()

    async def test_lease_release_decrements_in_flight(self, pool):
        lease = await pool.acquire()
        session = lease.session
        assert session.in_flight == 1
        await lease.release()
        assert session.in_flight == 0

    async def test_double_release_is_safe(self, pool):
        lease = await pool.acquire()
        await lease.release()
        await lease.release()
        assert lease.session.in_flight == 0

    async def test_floodwait_removes_session_from_selection(self, pool):
        lease = await pool.acquire()
        session = lease.session
        await pool.report_floodwait(session, 60)
        await lease.release()

        # Every subsequent acquire must pick the other session.
        for _ in range(5):
            other = await pool.acquire()
            assert other.session.id != session.id
            await other.release()

    async def test_floodwait_rotation_uses_a_different_session(self, store, settings):
        """The core ban-avoidance behaviour: never retry on the gated account.

        Rather than assuming which session the (randomised) selection picks
        first, this gates the session it actually chose and then asserts that the
        retry moved elsewhere. That is the property that matters, and it is
        independent of tie-breaking.
        """
        pool = SessionPool(backend=store, settings=settings)
        await pool.add_session(session_id="s1", label="one", client=object())
        await pool.add_session(session_id="s2", label="two", client=object())

        store.seed_channel(-1009999999999)
        attempt_labels: list[str] = []
        gated_label: str | None = None

        async def operation(session):
            nonlocal gated_label
            attempt_labels.append(session.label)
            # Trip a FloodWait on whichever session is used first.
            if gated_label is None:
                gated_label = session.label
                raise FloodWaitError(30, session_label=session.label)
            return await store.upload_chunk(
                channel_id=-1009999999999,
                data=b"payload",
                filename="f",
                session_label=session.label,
            )

        result = await pool.execute_with_retry("upload", operation, max_attempts=3)
        assert result is not None
        assert gated_label is not None
        assert len(attempt_labels) >= 2, "must have retried at least once"
        assert attempt_labels[1] != gated_label, (
            f"retry reused the gated session {gated_label!r} instead of rotating"
        )
        # The gated session is now on the bench for the instructed duration.
        assert pool.gate.remaining(gated_label) > 25

    async def test_floodwait_seconds_are_honoured_exactly(self, pool, store):
        store.floodwait_once["pool-a"] = 45
        lease = await pool.acquire()
        session = lease.session
        await pool.report_floodwait(session, 45)
        await lease.release()
        remaining = pool.gate.remaining(session.label)
        assert 40 < remaining <= 45, "the gate must match Telegram's instruction"

    async def test_excessive_floodwait_disables_session(self, store):
        """A multi-hour penalty means the account should leave rotation entirely
        rather than stall every request waiting for it."""
        settings = Settings(
            master_kek=b"0123456789abcdef0123456789abcdef",
            jwt_secret=b"test-secret-that-is-long-enough-32b",
            floodwait_max_sleep_seconds=100,
            storage_chunk_size_bytes=1024 * 1024,
        )
        pool = SessionPool(backend=store, settings=settings)
        await pool.add_session(session_id="s1", label="one", client=object())
        session = pool._sessions["s1"]
        await pool.report_floodwait(session, 5000)
        assert session.disabled
        assert "exceeds FLOODWAIT_MAX_SLEEP_SECONDS" in session.disabled_reason

    async def test_all_sessions_gated_raises_rather_than_spinning(self, store):
        """When every account is rate-limited the correct answer is a clear
        failure, not an unbounded wait that holds the request open."""
        settings = Settings(
            master_kek=b"0123456789abcdef0123456789abcdef",
            jwt_secret=b"test-secret-that-is-long-enough-32b",
            storage_chunk_size_bytes=1024 * 1024,
        )
        pool = SessionPool(backend=store, settings=settings)
        await pool.add_session(session_id="s1", label="one", client=object())
        await pool.add_session(session_id="s2", label="two", client=object())
        await pool.gate.gate("one", 300)
        await pool.gate.gate("two", 300)

        assert pool.healthy_count() == 0
        with pytest.raises(SessionUnavailableError):
            await pool.acquire(timeout=0.5)

    async def test_deleted_message_does_not_penalise_the_session(self, pool, store):
        """A missing message is a storage-object fault, not an account fault.
        Blaming the session would quarantine a healthy account."""
        lease = await pool.acquire()
        session = lease.session
        await lease.release()

        async def operation(_session):
            raise MessageNotFoundError("message is gone")

        with pytest.raises(MessageNotFoundError):
            await pool.execute_with_retry("read", operation, max_attempts=3)

        assert session.total_errors == 0
        assert pool.breaker.is_available(session.label), (
            "a deleted message must not quarantine a healthy session"
        )

    async def test_retry_budget_is_bounded(self, pool, store):
        """Unlimited retry against a platform saying 'stop' is the behaviour
        being defended against."""
        calls = 0

        async def always_fails(_session):
            nonlocal calls
            calls += 1
            raise FloodWaitError(1)

        with pytest.raises(SessionUnavailableError):
            await pool.execute_with_retry("op", always_fails, max_attempts=3)
        assert calls == 3

    async def test_success_resets_breaker(self, pool, store):
        lease = await pool.acquire()
        session = lease.session
        await lease.release()
        await pool.report_failure(session, RuntimeError("transient"))
        await pool.report_success(session)
        assert pool.breaker.state(session.label).consecutive_failures == 0

    async def test_global_concurrency_ceiling_is_enforced(self, store):
        settings = Settings(
            master_kek=b"0123456789abcdef0123456789abcdef",
            jwt_secret=b"test-secret-that-is-long-enough-32b",
            storage_chunk_size_bytes=1024 * 1024,
            telegram_session_pool_size=1,
            session_requests_per_minute=6000,
        )
        pool = SessionPool(backend=store, settings=settings)
        await pool.add_session(session_id="s1", label="one", client=object())

        # The limiter allows pool_size * 2 concurrent operations.
        peak = 0
        active = 0

        async def worker():
            nonlocal peak, active
            lease = await pool.acquire()
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            await pool.release(lease)

        await asyncio.gather(*(worker() for _ in range(8)))
        assert peak <= 2

    async def test_metrics_snapshot_shape(self, pool):
        snapshot = pool.snapshot()
        assert snapshot["pool_size"] == 2
        assert snapshot["healthy"] == 2
        assert len(snapshot["sessions"]) == 2
        assert "floodwait_seconds_total" in snapshot

    async def test_uploaded_bytes_are_accounted(self, pool, store):
        store.seed_channel(-1007777777777)
        lease = await pool.acquire()
        session = lease.session
        await lease.release()
        await pool.report_success(session, uploaded_bytes=1234)
        assert session.total_uploaded_bytes == 1234


class TestEndToEndFloodWaitRecovery:
    async def test_upload_succeeds_despite_floodwait_on_first_session(
        self, upload_service, seeded, store, chunk_size
    ):
        """The realistic scenario: one account is throttled mid-upload and the
        file still completes because traffic rotates."""
        data = os.urandom(chunk_size)
        store.floodwait_once["pool-a"] = 20
        store.floodwait_once["pool-b"] = 20

        from tests.conftest import upload_bytes

        node = await upload_bytes(
            upload_service,
            owner_id=seeded["user"]["id"],
            parent_id=seeded["sub"]["id"],
            name="flooded.bin",
            data=data,
            chunk_size=chunk_size,
        )
        assert node["upload_state"] == "ready"

    async def test_total_blackout_raises_clear_error(
        self, upload_service, seeded, store, chunk_size
    ):
        """When every account is throttled, the client must get an actionable
        error rather than a hang."""
        from app.core.errors import ProblemError

        store.floodwait_always["pool-a"] = 3600
        store.floodwait_always["pool-b"] = 3600

        session = await upload_service.create_session(
            owner_id=seeded["user"]["id"], parent_id=seeded["sub"]["id"],
            name="blackout.bin", size_bytes=chunk_size, mime_type=None,
            chunk_size=chunk_size,
        )
        import hashlib

        body = os.urandom(chunk_size)
        with pytest.raises(ProblemError) as exc:
            await upload_service.put_chunk(
                upload_id=session["id"], owner_id=seeded["user"]["id"],
                chunk_index=0, body=body,
                declared_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert exc.value.status_code == 503
        assert exc.value.code == "storage_unavailable"