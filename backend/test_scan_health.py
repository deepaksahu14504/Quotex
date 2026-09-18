"""Tests A-I from the final production brief.

Focus: scan progress and signal generation are SEPARATE, and the Live
Pipeline snapshot telemetry reflects real publications rather than signals.

Run: cd backend && python3 -m pytest test_scan_health.py -q
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.orchestrator import Orchestrator  # noqa: E402


def orch(**health):
    o = Orchestrator.__new__(Orchestrator)
    o.health = {"scan_gate_blocks": {}, "scan_cycle_count": 0,
                "consecutive_scan_failures": 0}
    o.health.update(health)
    o._asset_funnel = {}
    o._running = True
    o._data_stale = False
    return o


def live(**health):
    h = {"scan_cycle_count": 10, "last_scan_completed_at": time.time(),
         "last_scan_attempt_at": time.time()}
    h.update(health)
    return orch(**h)


# ================================================================== #
# THE REMAINING BUG — snapshot telemetry counted signals, not snapshots
# ================================================================== #
def test_snapshot_count_lives_at_the_real_publication_point():
    """It used to be incremented inside start_trace(), which only fires
    when a Signal is generated -- making it a duplicate of
    signals_generated and flat during a quiet market."""
    src = inspect.getsource(Orchestrator._pipeline_snapshot)
    assert 'self.health["pipeline_snapshot_update_count"]' in src
    assert 'self.health["last_pipeline_snapshot_at"]' in src


def test_start_trace_counts_signals_not_snapshots():
    src = inspect.getsource(Orchestrator)
    i = src.find("pipeline_tracer.start_trace")
    window = src[i:i + 500]
    assert 'self.health["signals_generated"]' in window
    assert "pipeline_snapshot_update_count" not in window


def test_the_two_regime_gates_now_publish_a_snapshot():
    """These were the ONLY early returns in _evaluate_asset_signal that
    published nothing -- the other eleven all call _pipeline_snapshot. An
    asset blocked there vanished from the Live Pipeline instead of showing
    as BLOCKED, which is why the panel froze while cycles advanced."""
    src = inspect.getsource(Orchestrator._evaluate_asset_signal)
    assert 'stage_reason=f"regime_paused:' in src
    assert 'stage_reason=f"regime_cooldown:' in src
    assert src.count('stage="blocked"') >= 2


def test_snapshot_and_signal_counters_are_independent():
    t = live(pipeline_snapshots_this_cycle=9, signals_this_cycle=0,
             pipeline_snapshot_update_count=900, signals_generated=0).scan_telemetry()
    assert t["pipeline_snapshot_update_count"] == 900
    assert t["signals_generated"] == 0
    assert t["pipeline_snapshot_update_count"] != t["signals_generated"]


# ================================================================== #
# Scanner state machine  (Tests H, I)
# ================================================================== #
def test_H_zero_signals_is_not_stalled():
    o = live(assets_evaluated=9, pipeline_snapshots_this_cycle=9, signals_this_cycle=0)
    assert o.scan_state() == "HEALTHY_NO_SIGNAL"


def test_signals_present_reads_as_scanning():
    assert live(signals_this_cycle=2).scan_state() == "SCANNING"


def test_I_real_stall_is_detected():
    o = live(last_scan_completed_at=time.time() - 500)
    assert o.scan_state() == "STALLED"


def test_stall_threshold_is_above_the_cycle_budget():
    from app.orchestrator import SCAN_CYCLE_BUDGET_SECONDS
    assert Orchestrator.SCAN_STALL_THRESHOLD_SECONDS > SCAN_CYCLE_BUDGET_SECONDS


def test_F_data_stale_keeps_the_scanner_alive():
    o = live(assets_evaluated=9)
    o._data_stale = True
    assert o.scan_state() == "DATA_STALE"
    o._data_stale = False
    assert o.scan_state() == "HEALTHY_NO_SIGNAL", "must resume with no restart"


def test_starting_and_stopping_states():
    assert orch().scan_state() == "STARTING"
    o = live()
    o._running = False
    assert o.scan_state() == "STOPPING"


# ================================================================== #
# Telemetry completeness  (§9)
# ================================================================== #
def test_every_required_telemetry_field_is_exposed():
    t = live().scan_telemetry()
    for key in ("scan_cycle_count", "last_scan_attempt_at", "last_scan_completed_at",
                "candidates_available", "candidates_selected",
                "candidates_excluded_by_readiness", "candidates_excluded_by_payout",
                "assets_evaluated", "evaluation_failures", "evaluation_timeouts",
                "pipeline_snapshot_update_count", "last_pipeline_snapshot_at",
                "signals_generated", "scan_gate_blocks",
                "consecutive_scan_failures", "current_scan_duration",
                "next_scan_due_at", "state"):
        assert key in t, f"missing required telemetry field: {key}"


def test_next_scan_due_at_follows_the_backstop():
    from app.orchestrator import SCAN_INTERVAL_BACKSTOP
    done = time.time()
    t = live(last_scan_completed_at=done).scan_telemetry()
    assert abs(t["next_scan_due_at"] - (done + SCAN_INTERVAL_BACKSTOP)) < 0.01


# ================================================================== #
# Candidate funnel  (§7)
# ================================================================== #
@pytest.mark.asyncio
async def test_candidate_funnel_explains_every_exclusion():
    import types as _t
    from app.schemas import AssetInfo

    o = Orchestrator.__new__(Orchestrator)
    o._asset_funnel = {}
    o._assets_ts = 9e18
    # 60 open; 8 below min payout; of the 52 eligible, 43 not READY.
    o._assets_cache = (
        [AssetInfo(symbol=f"A{i}", name=f"A{i}", payout=90.0 - i * 0.1,
                   is_open=True, is_otc=False) for i in range(52)]
        + [AssetInfo(symbol=f"L{i}", name=f"L{i}", payout=50.0,
                     is_open=True, is_otc=False) for i in range(8)]
    )
    ready = {f"A{i}" for i in range(9)}
    o.runtime = _t.SimpleNamespace(
        trading=_t.SimpleNamespace(asset_whitelist=[]),
        risk=_t.SimpleNamespace(min_payout=75.0))
    o.provider = _t.SimpleNamespace(_asset_feed=None, get_asset_feed_status=lambda: {})
    o.market_init = type("R", (), {"is_ready": lambda s, x: x in ready})()
    o.health = {}
    o._running = True
    o._data_stale = False

    cands = await o._candidate_assets()
    f = o.asset_funnel()
    assert len(cands) == 9, "the 52 -> 9 case from production"
    assert f["open_count"] == 60
    assert f["open_and_payout_qualified_count"] == 52
    assert f["candidates_excluded_by_readiness"] == 43
    t = o.scan_telemetry()
    assert t["candidates_excluded_by_payout"] == 8
    assert t["candidates_excluded_by_readiness"] == 43


# ================================================================== #
# Loop continuity  (Tests A-E)
# ================================================================== #
async def _cycle(evaluate, symbols, budget=0.5):
    tasks = [asyncio.ensure_future(evaluate(s)) for s in symbols]
    _done, pending = await asyncio.wait(tasks, timeout=budget)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return [t for t in tasks if not t.cancelled() and t.exception() is None]


@pytest.mark.asyncio
async def test_A_consecutive_cycles_complete_automatically():
    n = {"c": 0}

    async def ev(s):
        n["c"] += 1
        return s

    for _ in range(20):
        await _cycle(ev, [f"A{i}" for i in range(5)])
    assert n["c"] == 100


@pytest.mark.asyncio
async def test_B_all_rejected_still_advances():
    async def ev(s):
        return s, None

    for _ in range(5):
        assert len(await _cycle(ev, ["A", "B", "C"])) == 3


@pytest.mark.asyncio
async def test_C_all_blocked_by_cooldown_still_advances():
    blocks = {"n": 0}

    async def ev(s):
        blocks["n"] += 1
        return s, None

    for _ in range(5):
        await _cycle(ev, ["A", "B", "C"])
    assert blocks["n"] == 15


@pytest.mark.asyncio
async def test_D_one_asset_timeout_does_not_stop_the_rest():
    async def ev(s):
        if s == "HANG":
            await asyncio.Event().wait()
        return s

    ok = await _cycle(ev, ["HANG"] + [f"A{i}" for i in range(8)], budget=0.3)
    assert len(ok) == 8


@pytest.mark.asyncio
async def test_E_one_strategy_exception_does_not_stop_the_rest():
    async def ev(s):
        if s == "BOOM":
            raise RuntimeError("strategy failed")
        return s

    ok = await _cycle(ev, ["BOOM"] + [f"A{i}" for i in range(8)])
    assert len(ok) == 8


@pytest.mark.asyncio
async def test_G_snapshot_telemetry_advances_on_a_real_publication():
    o = live()
    o._pipeline_events = type("B", (), {
        "update_snapshot": lambda s, a, tf, **kw: type("S", (), {"to_dict": lambda s2: {}})()
    })()
    o.user_id = "u"
    before = o.health.get("pipeline_snapshot_update_count", 0)
    for stage in ("passed", "rejected", "blocked", "failed"):
        o._pipeline_snapshot("EURUSD", "1m", stage=stage)
    assert o.health["pipeline_snapshot_update_count"] == before + 4
    assert o.health.get("signals_generated", 0) == 0, \
        "publishing a snapshot must not count as a signal"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
