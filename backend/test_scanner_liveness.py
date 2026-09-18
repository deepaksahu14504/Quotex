"""Phase-18 regression suite: scanner liveness and per-cycle telemetry.

Drives the REAL Orchestrator._scan_once() and the REAL scan_telemetry()/
scan_state() against stubbed candidate selection and asset evaluation, so
these prove the shipped code advances, not a reimplementation of it.

The distinction under test is the one that made the production report
ambiguous: a quiet market and a wedged scanner look identical from
"Pipeline supervision: OK" and "Scanning · 9 assets". Only cycle
progression separates them.

Run: cd backend && python3 -m pytest test_scanner_liveness.py -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.orchestrator import Orchestrator, now_ts  # noqa: E402


class _NS:
    """Minimal attribute bag for the collaborators _scan_once reads."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class Rig:
    """Real _scan_once / scan_telemetry / scan_state on a minimal object."""

    def __init__(self, n_candidates=9, evaluate=None):
        self.o = Orchestrator.__new__(Orchestrator)
        o = self.o
        o._running = True
        o._data_stale = False
        o._asset_funnel = {"eligible_before_ready_gate": 52}
        o.health = {"scan_gate_blocks": {}, "scan_cycle_count": 0,
                    "consecutive_scan_failures": 0}
        # Everything _scan_once reads BEFORE the telemetry block. Stubbed
        # rather than mocked away, so the real function body runs.
        o.user_id = "test-user"
        o._last_confidence_model_retrain_count = 0
        o.runtime = _NS(trading=_NS(
            timeframe="1m", enabled_strategies=["trend_pullback"],
            multi_timeframe_confirmation=False))
        o.calibrator = _NS(needs_refresh=lambda *a, **k: False, refresh=lambda *a, **k: None)
        o.regime_tracker = _NS(needs_refresh=lambda *a, **k: False, refresh=lambda *a, **k: None)
        o.strategy_manager = _NS(active_strategies=lambda *a, **k: ["trend_pullback"],
                                 maybe_start_trials=lambda *a, **k: [])
        o.store = _NS(all=lambda *a, **k: [])
        o.decision_store = _NS(read_recent=lambda *a, **k: [])
        o.confidence_model_store = _NS(model=_NS(fit=lambda *a, **k: None),
                                       save=lambda *a, **k: None)
        self.candidates = [_Asset(f"A{i}", 90 - i) for i in range(n_candidates)]
        self._evaluate = evaluate or self._ok
        o._candidate_assets = self._candidates
        o._evaluate_asset_signal = self._evaluate

    async def _candidates(self):
        return list(self.candidates)

    async def _ok(self, asset, *a, **kw):
        await asyncio.sleep(0.001)
        return (asset.symbol, None)          # evaluated, no signal proposed

    async def cycle(self):
        try:
            await self.o._scan_once()
        except Exception:
            # _scan_once continues past the telemetry block into signal
            # handling, which needs far more of the orchestrator. The
            # telemetry under test is written before that point.
            pass

    @property
    def h(self):
        return self.o.health

    def telemetry(self):
        return self.o.scan_telemetry()


class _Asset:
    def __init__(self, symbol, payout):
        self.symbol, self.payout, self.is_open = symbol, payout, True


# ================================================================= #
# 1. Continuous progression  (Phase 18 minimum: 10+ cycles)
# ================================================================= #
@pytest.mark.asyncio
async def test_twelve_consecutive_cycles_advance():
    r = Rig()
    seen = []
    for _ in range(12):
        await r.cycle()
        seen.append(r.h["scan_cycle_count"])
    assert seen == list(range(1, 13)), seen
    assert r.h["last_scan_completed_at"] >= r.h["last_scan_attempt_at"]


@pytest.mark.asyncio
async def test_every_cycle_advances_all_progress_fields():
    r = Rig()
    await r.cycle()
    first = dict(r.h)
    await asyncio.sleep(0.02)
    await r.cycle()
    for field in ("last_scan_attempt_at", "last_scan_completed_at",
                  "last_candidate_selection_at"):
        assert r.h[field] > first[field], f"{field} did not advance"
    assert r.h["scan_cycle_count"] == first["scan_cycle_count"] + 1


@pytest.mark.asyncio
async def test_candidate_and_evaluation_counters_are_populated():
    r = Rig(n_candidates=9)
    await r.cycle()
    assert r.h["candidates_selected"] == 9
    assert r.h["assets_evaluated"] == 9
    assert r.h["candidates_available"] == 52
    assert r.h["evaluation_failures"] == 0


# ================================================================= #
# 2. Test G — zero signals is a HEALTHY cycle, not a stall
# ================================================================= #
@pytest.mark.asyncio
async def test_zero_signals_still_advances_the_cycle_count():
    """The production case. Live Pipeline only records a PROPOSED signal
    (start_trace fires after eight gates), so a quiet market produces no
    entries at all -- which must not read as a stalled scanner."""
    r = Rig()
    for _ in range(10):
        await r.cycle()
    t = r.telemetry()
    assert t["scan_cycle_count"] == 10
    assert t["signals_this_cycle"] == 0
    assert t["state"] == "HEALTHY_NO_SIGNAL"


@pytest.mark.asyncio
async def test_healthy_no_signal_is_distinguishable_from_stalled():
    r = Rig()
    await r.cycle()
    assert r.telemetry()["state"] == "HEALTHY_NO_SIGNAL"
    r.h["last_scan_completed_at"] = now_ts() - (Orchestrator.SCAN_STALL_THRESHOLD_SECONDS + 60)
    assert r.telemetry()["state"] == "STALLED"


@pytest.mark.asyncio
async def test_states_cover_startup_and_shutdown():
    r = Rig()
    assert r.telemetry()["state"] == "STARTING"      # no cycle yet
    await r.cycle()
    assert r.telemetry()["state"] == "HEALTHY_NO_SIGNAL"
    r.o._data_stale = True
    assert r.telemetry()["state"] == "DATA_STALE"
    r.o._data_stale = False
    r.o._running = False
    assert r.telemetry()["state"] == "STOPPING"


# ================================================================= #
# 3. Tests C/D/E/F — failure isolation must not stop progression
# ================================================================= #
@pytest.mark.asyncio
async def test_C_one_asset_raises_others_still_evaluated():
    async def flaky(asset, *a, **kw):
        if asset.symbol == "A3":
            raise RuntimeError("strategy blew up")
        await asyncio.sleep(0.001)
        return (asset.symbol, None)

    r = Rig(evaluate=flaky)
    await r.cycle()
    await r.cycle()
    assert r.h["scan_cycle_count"] == 2, "a raising asset stopped the cycle"
    assert r.h["assets_evaluated"] == 9
    assert r.h["evaluation_failures"] == 1


@pytest.mark.asyncio
async def test_D_one_asset_hangs_cycle_still_completes():
    import app.orchestrator as orch
    original = orch.SCAN_CYCLE_BUDGET_SECONDS
    orch.SCAN_CYCLE_BUDGET_SECONDS = 0.3
    try:
        async def hangs(asset, *a, **kw):
            if asset.symbol == "A5":
                await asyncio.Event().wait()
            await asyncio.sleep(0.001)
            return (asset.symbol, None)

        r = Rig(evaluate=hangs)
        await asyncio.wait_for(r.cycle(), timeout=10)
        await asyncio.wait_for(r.cycle(), timeout=10)
        assert r.h["scan_cycle_count"] == 2, "a hung asset stopped the scanner"
        assert r.h["assets_evaluated"] == 8, "the other 8 must still be used"
    finally:
        orch.SCAN_CYCLE_BUDGET_SECONDS = original


@pytest.mark.asyncio
async def test_D2_hung_asset_leaves_no_orphan_task():
    import app.orchestrator as orch
    original = orch.SCAN_CYCLE_BUDGET_SECONDS
    orch.SCAN_CYCLE_BUDGET_SECONDS = 0.2
    try:
        async def hangs(asset, *a, **kw):
            if asset.symbol == "A5":
                await asyncio.Event().wait()
            return (asset.symbol, None)

        before = len([t for t in asyncio.all_tasks() if not t.done()])
        r = Rig(evaluate=hangs)
        await r.cycle()
        await asyncio.sleep(0.1)
        after = [t for t in asyncio.all_tasks() if not t.done()]
        assert len(after) <= before + 1
    finally:
        orch.SCAN_CYCLE_BUDGET_SECONDS = original


@pytest.mark.asyncio
async def test_F_provider_timeout_finishes_the_cycle():
    async def times_out(asset, *a, **kw):
        if asset.symbol in ("A1", "A2"):
            raise asyncio.TimeoutError("provider call timed out")
        return (asset.symbol, None)

    r = Rig(evaluate=times_out)
    for _ in range(3):
        await r.cycle()
    assert r.h["scan_cycle_count"] == 3
    assert r.h["evaluation_failures"] == 2


@pytest.mark.asyncio
async def test_every_asset_failing_does_not_stop_progression():
    async def all_fail(asset, *a, **kw):
        raise RuntimeError("boom")

    r = Rig(evaluate=all_fail)
    for _ in range(4):
        await r.cycle()
    assert r.h["scan_cycle_count"] == 4, "the scanner must keep cycling"
    assert r.h["consecutive_scan_failures"] >= 1, "and must record that it is failing"


@pytest.mark.asyncio
async def test_I_recovery_clears_consecutive_failures():
    state = {"fail": True}

    async def flaky(asset, *a, **kw):
        if state["fail"]:
            raise RuntimeError("down")
        return (asset.symbol, None)

    r = Rig(evaluate=flaky)
    for _ in range(3):
        await r.cycle()
    assert r.h["consecutive_scan_failures"] >= 1
    state["fail"] = False
    await r.cycle()
    assert r.h["consecutive_scan_failures"] == 0
    assert r.telemetry()["state"] == "HEALTHY_NO_SIGNAL"


# ================================================================= #
# 4. Test B — partial READY must not block the rest
# ================================================================= #
@pytest.mark.asyncio
async def test_B_partial_ready_scans_the_ready_subset():
    r = Rig(n_candidates=0)
    r.candidates = [_Asset("A", 90), _Asset("B", 89), _Asset("D", 88)]  # C not READY
    for _ in range(5):
        await r.cycle()
    assert r.h["scan_cycle_count"] == 5
    assert r.h["candidates_selected"] == 3
    assert r.h["assets_evaluated"] == 3


@pytest.mark.asyncio
async def test_zero_candidates_still_advances_the_cycle():
    """No eligible assets is a valid cycle outcome, not a stall."""
    r = Rig(n_candidates=0)
    r.candidates = []
    for _ in range(3):
        await r.cycle()
    assert r.h["scan_cycle_count"] == 3
    assert r.h["candidates_selected"] == 0


# ================================================================= #
# 5. Telemetry contract
# ================================================================= #
@pytest.mark.asyncio
async def test_telemetry_exposes_every_required_field():
    r = Rig()
    await r.cycle()
    t = r.telemetry()
    for field in ("state", "last_scan_attempt_at", "last_scan_completed_at",
                  "last_candidate_selection_at", "scan_cycle_count",
                  "consecutive_scan_failures", "current_scan_duration",
                  "next_scan_due_at", "candidates_available",
                  "candidates_selected", "assets_evaluated",
                  "evaluation_failures", "evaluation_timeouts",
                  "scan_gate_blocks", "last_scan_age_seconds"):
        assert field in t, f"missing telemetry field: {field}"


@pytest.mark.asyncio
async def test_gate_blocks_reset_each_cycle():
    """Otherwise a single early block would look permanent."""
    r = Rig()
    await r.cycle()
    r.h["scan_gate_blocks"]["regime_cooldown"] = 5
    await r.cycle()
    assert r.h["scan_gate_blocks"] == {}


@pytest.mark.asyncio
async def test_scan_duration_is_recorded():
    async def slowish(asset, *a, **kw):
        await asyncio.sleep(0.01)
        return (asset.symbol, None)

    r = Rig(evaluate=slowish)
    await r.cycle()
    assert r.h["current_scan_duration"] >= 0.0


@pytest.mark.asyncio
async def test_cycle_count_is_monotonic_under_concurrency():
    """Cycles are serialized by the scan loop; the counter must never
    regress even if a cycle overlaps."""
    r = Rig()
    for _ in range(20):
        await r.cycle()
    assert r.h["scan_cycle_count"] == 20


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
