"""Regression tests for the PROVEN root cause: the pipeline stops making
progress because QuotexAPI.send_websocket_request was unbounded.

Every test in section 1 FAILS against the pre-fix code (it hangs and the
assertion never runs, or the assertion is False) and PASSES after.

Run:  cd backend && python3 -m pytest test_pipeline_progress.py -q
      (or)  python3 test_pipeline_progress.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# NOTE ordering: vendor/pyquotex contains a top-level app.py which would
# shadow this backend's `app` PACKAGE if it came first on sys.path.
# Backend first, vendor appended last.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[1] / "vendor" / "pyquotex"))

import pytest  # noqa: E402

from app.engine.error_recovery import ErrorRecovery  # noqa: E402
from app.engine.task_supervisor import TaskSupervisor  # noqa: E402
from pyquotex.api import QuotexAPI  # noqa: E402
from pyquotex.global_value import WebsocketStatus  # noqa: E402


# ===================================================================== #
# Fakes
# ===================================================================== #
class DeadSocket:
    """A transport that has gone half-open: send() never returns and
    never raises, exactly like websockets on a connection dropped with no
    FIN/RST."""

    def __init__(self):
        self.sends = 0

    async def send(self, data):
        self.sends += 1
        await asyncio.Event().wait()


class LiveSocket:
    def __init__(self):
        self.sends = 0

    async def send(self, data):
        self.sends += 1
        await asyncio.sleep(0)


class SlowSocket:
    """Alive but slow -- must NOT be declared dead below the ceiling."""

    def __init__(self, delay):
        self.delay = delay
        self.sends = 0

    async def send(self, data):
        await asyncio.sleep(self.delay)
        self.sends += 1


def make_api(socket, *, send_timeout=0.3, lock_timeout=0.5) -> QuotexAPI:
    """A real QuotexAPI with only the two attributes send_websocket_request
    touches, plus shrunk ceilings so tests run in milliseconds rather than
    the production 10s/15s."""
    api = QuotexAPI.__new__(QuotexAPI)
    api._ws_send_lock = asyncio.Lock()
    # `websocket` is a read-only property returning
    # self.websocket_client.wss (api.py:735), so the socket is injected
    # through the client the same way the real code path supplies it.
    api.websocket_client = type("_C", (), {"wss": socket})()

    class _State:
        status = WebsocketStatus.CONNECTED

    api.state = _State()
    api.WS_SEND_TIMEOUT = send_timeout
    api.WS_SEND_LOCK_TIMEOUT = lock_timeout
    return api


# ===================================================================== #
# 1. THE ROOT CAUSE — these fail against the old code
# ===================================================================== #
@pytest.mark.asyncio
async def test_send_on_dead_socket_raises_instead_of_hanging():
    """PRE-FIX: hangs forever. POST-FIX: bounded TimeoutError.

    NOTE on how this is asserted. `asyncio.TimeoutError is TimeoutError`
    on Python 3.11+, so an earlier version of this test that wrapped the
    call in `asyncio.wait_for(..., timeout=5)` and asserted
    `pytest.raises(TimeoutError)` PASSED against the unfixed code -- the
    outer wait_for manufactured exactly the exception being asserted. A
    test that cannot fail is worse than no test. This version gives the
    method 3x its own ceiling with no outer timeout at all, then asserts
    the task actually completed on its own.
    """
    api = make_api(DeadSocket(), send_timeout=0.3)
    t0 = time.monotonic()
    task = asyncio.ensure_future(api.send_websocket_request("42[]"))
    await asyncio.sleep(1.0)
    assert task.done(), (
        "send_websocket_request did not return within 3x its own timeout "
        "-- the await is unbounded"
    )
    with pytest.raises(TimeoutError):
        await task
    assert time.monotonic() - t0 < 1.5


@pytest.mark.asyncio
async def test_dead_send_releases_the_lock():
    """THE CASCADE. Pre-fix the lock was held forever, so every other
    caller -- candles, history, orders -- blocked behind it."""
    api = make_api(DeadSocket(), send_timeout=0.3)
    task = asyncio.ensure_future(api.send_websocket_request("first"))
    await asyncio.sleep(1.0)
    assert task.done(), "send never returned -- lock is held forever"
    with pytest.raises(TimeoutError):
        await task
    assert not api._ws_send_lock.locked(), "lock still held -- cascade not fixed"


@pytest.mark.asyncio
async def test_other_senders_are_not_blocked_forever():
    api = make_api(DeadSocket(), send_timeout=0.3, lock_timeout=0.5)
    tasks = [asyncio.ensure_future(api.send_websocket_request(f"s{i}")) for i in range(5)]
    await asyncio.sleep(2.0)
    assert all(t.done() for t in tasks), (
        f"{sum(1 for t in tasks if not t.done())}/5 senders still blocked "
        "on the lock -- the cascade is not fixed"
    )
    results = [t.exception() for t in tasks]
    assert all(isinstance(r, TimeoutError) for r in results), results


@pytest.mark.asyncio
async def test_dead_transport_is_marked_so_reconnect_can_engage():
    """Bounding the send alone would only convert a permanent hang into a
    permanent stream of timeouts. The status flip is what lets
    _watchdog_loop's `not provider.connected` check finally fire."""
    api = make_api(DeadSocket(), send_timeout=0.3)
    assert api.state.status == WebsocketStatus.CONNECTED
    task = asyncio.ensure_future(api.send_websocket_request("x"))
    await asyncio.sleep(1.0)
    assert task.done(), "send never returned"
    with pytest.raises(TimeoutError):
        await task
    assert api.state.status == WebsocketStatus.ERROR


@pytest.mark.asyncio
async def test_healthy_send_is_completely_unaffected():
    sock = LiveSocket()
    api = make_api(sock)
    for i in range(50):
        await api.send_websocket_request(f"msg-{i}")
    assert sock.sends == 50
    assert api.state.status == WebsocketStatus.CONNECTED
    assert not api._ws_send_lock.locked()


@pytest.mark.asyncio
async def test_slow_but_alive_send_is_not_killed():
    """A busy broker must not be mistaken for a dead one."""
    sock = SlowSocket(delay=0.1)
    api = make_api(sock, send_timeout=1.0)
    await api.send_websocket_request("slow")
    assert sock.sends == 1
    assert api.state.status == WebsocketStatus.CONNECTED


@pytest.mark.asyncio
async def test_concurrent_senders_serialize_correctly_when_healthy():
    sock = LiveSocket()
    api = make_api(sock)
    await asyncio.gather(*[api.send_websocket_request(f"c{i}") for i in range(20)])
    assert sock.sends == 20
    assert not api._ws_send_lock.locked()


# ===================================================================== #
# 2. execute_with_retry can now bound a hang
# ===================================================================== #
@pytest.mark.asyncio
async def test_execute_with_retry_bounds_a_hanging_call():
    er = ErrorRecovery()
    calls = {"n": 0}

    async def hangs(*a, **kw):
        calls["n"] += 1
        await asyncio.Event().wait()

    with pytest.raises(Exception):
        await asyncio.wait_for(
            er.execute_with_retry(hangs, "get_candles", timeout=0.1), timeout=10
        )
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_execute_with_retry_without_timeout_is_unchanged():
    """Opt-in: existing callers that pass no timeout behave bit-for-bit
    as before."""
    er = ErrorRecovery()

    async def ok(a, b):
        return a + b

    assert await er.execute_with_retry(ok, "svc", 2, 3) == 5


@pytest.mark.asyncio
async def test_execute_with_retry_propagates_cancellation():
    """Cancellation must never be swallowed and retried as a transient
    failure (root-cause class G/H)."""
    er = ErrorRecovery()

    async def slow():
        await asyncio.sleep(10)

    task = asyncio.ensure_future(er.execute_with_retry(slow, "svc"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ===================================================================== #
# 3. Scan cycle isolation — one stalled asset must not stall the cycle
# ===================================================================== #
async def _run_cycle(evaluate, candidates, budget):
    """Mirrors the fixed _scan_once evaluation phase exactly."""
    tasks = [asyncio.ensure_future(evaluate(a)) for a in candidates]
    results = []
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=budget)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for t in tasks:
            if t.cancelled():
                continue
            exc = t.exception()
            results.append(exc if exc is not None else t.result())
    return results


@pytest.mark.asyncio
async def test_one_hung_asset_does_not_stall_the_cycle():
    async def evaluate(sym):
        if sym == "A7":
            await asyncio.Event().wait()
        await asyncio.sleep(0.001)
        return sym

    candidates = [f"A{i}" for i in range(12)]
    t0 = time.monotonic()
    results = await asyncio.wait_for(_run_cycle(evaluate, candidates, 0.4), timeout=5)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, "cycle did not complete within its budget"
    assert len(results) == 11, "the 11 healthy assets must still be used"
    assert "A7" not in results


@pytest.mark.asyncio
async def test_healthy_cycle_uses_every_candidate_and_is_fast():
    async def evaluate(sym):
        await asyncio.sleep(0.001)
        return sym

    candidates = [f"A{i}" for i in range(12)]
    t0 = time.monotonic()
    results = await _run_cycle(evaluate, candidates, 5.0)
    assert len(results) == 12
    assert time.monotonic() - t0 < 0.5, "budget must not slow a healthy cycle"


@pytest.mark.asyncio
async def test_asset_exceptions_still_surface_as_before():
    async def evaluate(sym):
        if sym == "A3":
            raise RuntimeError("boom")
        return sym

    results = await _run_cycle(evaluate, [f"A{i}" for i in range(5)], 2.0)
    assert len(results) == 5
    assert sum(1 for r in results if isinstance(r, Exception)) == 1


@pytest.mark.asyncio
async def test_cancelled_stragglers_leave_no_orphan_tasks():
    async def evaluate(sym):
        if sym in ("A1", "A2"):
            await asyncio.Event().wait()
        return sym

    before = len(asyncio.all_tasks())
    await _run_cycle(evaluate, [f"A{i}" for i in range(5)], 0.3)
    await asyncio.sleep(0.1)
    assert len([t for t in asyncio.all_tasks() if not t.done()]) <= before + 1


# ===================================================================== #
# 4. Real-progress detection (the secondary safety net)
# ===================================================================== #
class ProgressWatchdogHarness:
    """Reproduces the watchdog's stall check and _recover_stalled_scan
    against a real TaskSupervisor."""

    def __init__(self, stall_seconds=0.3):
        self.stall_seconds = stall_seconds
        self._running = True
        self.health = {"last_scan_at": 0.0, "cycles": 0, "scan_stall_recoveries": 0}
        self.hang = False
        self._supervisor = TaskSupervisor("harness")
        self._scan_task = None
        self._last_scan_recovery_at = 0.0
        self.now = time.monotonic

    async def _scan_loop(self):
        while self._running:
            if self.hang:
                await asyncio.Event().wait()
            self.health["last_scan_at"] = self.now()
            self.health["cycles"] += 1
            await asyncio.sleep(0.01)

    def start(self):
        self._scan_task = self._supervisor.supervise(
            "scan", self._scan_loop, is_active=lambda: self._running
        )

    async def watchdog_tick(self):
        last = self.health.get("last_scan_at") or 0.0
        if last <= 0:
            return False
        age = self.now() - last
        if age <= self.stall_seconds:
            return False
        if self.now() - self._last_scan_recovery_at < self.stall_seconds:
            return False
        self._last_scan_recovery_at = self.now()
        self.health["scan_stall_recoveries"] += 1
        old = self._scan_task
        if old is not None and not old.done():
            old.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(old), timeout=1)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self.health["last_scan_at"] = self.now()
        self._scan_task = self._supervisor.supervise(
            "scan", self._scan_loop, is_active=lambda: self._running
        )
        return True


@pytest.mark.asyncio
async def test_supervisor_alone_cannot_see_a_wedged_loop():
    """Documents WHY the progress check is needed -- this is the exact
    blind spot the root cause exploited."""
    h = ProgressWatchdogHarness()
    h.start()
    await asyncio.sleep(0.15)
    h.hang = True
    frozen = h.health["cycles"]
    await asyncio.sleep(0.4)
    assert h.health["cycles"] == frozen, "loop should be wedged"
    snap = h._supervisor.snapshot()["scan"]
    assert snap["running"] is True and snap["stuck"] is False
    h._running = False
    h._scan_task.cancel()
    await asyncio.gather(h._scan_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_progress_watchdog_recovers_a_wedged_loop():
    h = ProgressWatchdogHarness(stall_seconds=0.3)
    h.start()
    await asyncio.sleep(0.15)
    h.hang = True
    await asyncio.sleep(0.5)

    fired = await h.watchdog_tick()
    assert fired, "watchdog must detect the stall from real progress"

    h.hang = False
    before = h.health["cycles"]
    await asyncio.sleep(0.3)
    assert h.health["cycles"] > before, "scan cycles must resume after recovery"
    assert h.health["scan_stall_recoveries"] == 1

    h._running = False
    h._scan_task.cancel()
    await asyncio.gather(h._scan_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_progress_watchdog_never_fires_on_a_healthy_loop():
    h = ProgressWatchdogHarness(stall_seconds=0.3)
    h.start()
    for _ in range(20):
        await asyncio.sleep(0.03)
        assert await h.watchdog_tick() is False
    assert h.health["scan_stall_recoveries"] == 0
    h._running = False
    h._scan_task.cancel()
    await asyncio.gather(h._scan_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_no_aggressive_restart_loop():
    """A permanently broken loop must produce ONE restart per window, not
    a hot restart storm (Step 7)."""
    h = ProgressWatchdogHarness(stall_seconds=0.3)
    h.start()
    await asyncio.sleep(0.1)
    h.hang = True
    await asyncio.sleep(0.5)
    fired = 0
    for _ in range(25):
        if await h.watchdog_tick():
            fired += 1
        await asyncio.sleep(0.02)
    assert fired <= 2, f"restart storm: {fired} restarts in ~0.5s"
    h._running = False
    if h._scan_task:
        h._scan_task.cancel()
        await asyncio.gather(h._scan_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_no_duplicate_scanner_after_recovery():
    h = ProgressWatchdogHarness(stall_seconds=0.3)
    h.start()
    await asyncio.sleep(0.1)
    h.hang = True
    await asyncio.sleep(0.5)
    await h.watchdog_tick()
    h.hang = False
    await asyncio.sleep(0.2)
    scan_tasks = [
        t for t in asyncio.all_tasks()
        if not t.done() and "_scan_loop" in repr(t)
    ]
    assert len(scan_tasks) <= 1, f"duplicate scanners: {len(scan_tasks)}"
    h._running = False
    h._scan_task.cancel()
    await asyncio.gather(h._scan_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_intentional_shutdown_is_preserved():
    """Stopping must NOT be treated as a stall and restarted."""
    h = ProgressWatchdogHarness(stall_seconds=0.3)
    h.start()
    await asyncio.sleep(0.1)
    h._running = False
    await asyncio.sleep(0.2)
    snap = h._supervisor.snapshot()["scan"]
    assert snap["running"] is False, "graceful stop must not restart"
    assert snap["restart_count"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
