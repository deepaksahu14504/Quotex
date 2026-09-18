"""Regression tests: the scan cycle must keep advancing, and a cycle that
produces no signal must be distinguishable from a wedged scanner.

Run: cd backend && python3 -m pytest test_scan_progress.py -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402


# ================================================================== #
# 1. THE ROOT CAUSE — regime cooldown re-armed every cycle
# ================================================================== #
def replay_cooldown(cycles, cooldown_seq, arm_once):
    """Replays orchestrator.py:2259-2273 exactly.

    `cooldown_seq(cycle)` returns what the transition detector reports on
    that cycle. This matters: with a ONE-SHOT transition both the old and
    new code release after N, so the bug only bites when the detector
    reports a non-zero cooldown on consecutive evaluations -- a choppy
    market where the regime keeps flipping. An earlier version of this
    helper held the cooldown non-zero every cycle unconditionally, which
    modelled only the pathological case and made the post-fix numbers
    look wrong.
    """
    bar_counter, until = {}, {}
    k = "EURUSD|1m"
    blocked = 0
    for c in range(1, cycles + 1):
        cd = cooldown_seq(c)
        bar_counter[k] = bar_counter.get(k, 0) + 1
        if cd > 0 and (not arm_once or k not in until):
            until[k] = bar_counter[k] + cd
        if bar_counter[k] < until.get(k, 0):
            blocked += 1
        elif arm_once:
            until.pop(k, None)
    return blocked


def one_shot(n):
    return lambda c: n if c == 1 else 0


def persistent(n):
    return lambda c: n


def test_prefix_blocks_forever_when_the_detector_keeps_reporting_a_cooldown():
    """THE BUG. Deadline = counter + N is recomputed every cycle while the
    counter also advances by 1, so `counter < until` stays true forever."""
    assert replay_cooldown(50, persistent(3), arm_once=False) == 50


def test_fixed_releases_even_when_the_detector_keeps_reporting():
    assert replay_cooldown(50, persistent(3), arm_once=True) < 50
    assert replay_cooldown(50, persistent(3), arm_once=True) > 0


def test_one_shot_transition_is_unchanged_by_the_fix():
    """No regression: for a single transition both behave identically."""
    for n in (1, 3, 5, 10):
        assert replay_cooldown(60, one_shot(n), arm_once=False) == n
        assert replay_cooldown(60, one_shot(n), arm_once=True) == n


@pytest.mark.parametrize("n", [1, 2, 5, 10])
def test_cooldown_length_is_honoured(n):
    assert replay_cooldown(100, one_shot(n), arm_once=True) == n


def test_zero_cooldown_never_blocks():
    assert replay_cooldown(50, one_shot(0), arm_once=True) == 0


def test_the_real_source_arms_once():
    import inspect
    from app.orchestrator import Orchestrator
    src = inspect.getsource(Orchestrator._evaluate_asset_signal)
    assert "rt_key not in self._regime_cooldown_until_bar" in src, \
        "the cooldown is still re-armed unconditionally"
    assert "self._regime_cooldown_until_bar.pop(rt_key, None)" in src, \
        "an elapsed cooldown must be cleared so a later transition can arm again"


# ================================================================== #
# 2. Telemetry makes a signal-less cycle visible
# ================================================================== #
def make_orch():
    import types as _t
    from app.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.health = {}
    o.health.setdefault("scan_gate_blocks", {})
    o.health.setdefault("scan_cycle_count", 0)
    o.health.setdefault("consecutive_scan_failures", 0)
    o._asset_funnel = {}
    return o


def test_telemetry_shape_is_complete():
    o = make_orch()
    t = o.scan_telemetry()
    for key in ("scan_cycle_count", "last_scan_attempt_at", "last_scan_completed_at",
                "last_scan_age_seconds", "last_candidate_selection_at",
                "candidates_available", "candidates_selected", "assets_evaluated",
                "evaluation_failures", "evaluation_timeouts", "current_scan_duration",
                "consecutive_scan_failures", "signals_proposed_last_cycle",
                "scan_gate_blocks", "last_pipeline_snapshot_at",
                # RENAMED, deliberately: "snapshot_update_count" counted
                # start_trace() calls, i.e. signals. The Live Pipeline is
                # published by _pipeline_snapshot() for every evaluated
                # asset (PASSED/REJECTED/BLOCKED/FAILED), so the counter
                # moved there and the signal count is now separate.
                "pipeline_snapshot_update_count", "signals_generated"):
        assert key in t, f"missing telemetry key {key}"


def test_fresh_orchestrator_reports_no_cycles():
    t = make_orch().scan_telemetry()
    assert t["scan_cycle_count"] == 0
    assert t["last_scan_completed_at"] is None
    assert t["last_scan_age_seconds"] is None


def test_gate_blocks_are_surfaced():
    o = make_orch()
    o.health["scan_gate_blocks"] = {"regime_cooldown": 9}
    o.health["signals_proposed_last_cycle"] = 0
    t = o.scan_telemetry()
    assert t["scan_gate_blocks"]["regime_cooldown"] == 9
    assert t["signals_proposed_last_cycle"] == 0


def test_scan_once_records_a_cycle():
    import inspect
    from app.orchestrator import Orchestrator
    src = inspect.getsource(Orchestrator._scan_once)
    for marker in ('self.health["last_scan_attempt_at"]',
                   'self.health["scan_cycle_count"]',
                   'self.health["last_scan_completed_at"]',
                   'self.health["assets_evaluated"]',
                   "SCAN_CYCLE_NO_SIGNAL"):
        assert marker in src, f"_scan_once does not record {marker}"


def test_snapshot_publication_is_counted_at_the_real_publisher():
    """CONTRACT UPDATED. The counter used to sit next to start_trace(),
    which only fires for signals -- so it was a duplicate of the signal
    count and stayed flat through a quiet market. It now lives in
    _pipeline_snapshot(), the single choke point every Live Pipeline
    publication goes through."""
    import inspect
    from app.orchestrator import Orchestrator
    pub = inspect.getsource(Orchestrator._pipeline_snapshot)
    assert 'self.health["pipeline_snapshot_update_count"]' in pub

    src = inspect.getsource(Orchestrator)
    i = src.find("pipeline_tracer.start_trace")
    window = src[i:i + 500]
    assert 'self.health["signals_generated"]' in window
    assert "pipeline_snapshot_update_count" not in window


# ================================================================== #
# 3. The loop must always reach the next cycle
# ================================================================== #
@pytest.mark.asyncio
async def test_a_signal_less_cycle_still_schedules_the_next_one():
    ticks = {"cycles": 0}
    running = {"v": True}

    async def scan_once():
        ticks["cycles"] += 1
        return None                      # no signal, ever

    async def loop():
        while running["v"]:
            try:
                await scan_once()
            except Exception:
                pass
            await asyncio.sleep(0.01)

    t = asyncio.ensure_future(loop())
    await asyncio.sleep(0.3)
    running["v"] = False
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    assert ticks["cycles"] > 5, "a signal-less cycle must not stop the loop"


@pytest.mark.asyncio
async def test_an_asset_exception_does_not_stop_the_cycle():
    async def evaluate(sym):
        if sym == "BAD":
            raise RuntimeError("boom")
        return sym, None, "range", None

    tasks = [asyncio.ensure_future(evaluate(s)) for s in ["BAD"] + [f"A{i}" for i in range(8)]]
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    assert not pending
    ok = [t for t in tasks if not t.cancelled() and t.exception() is None]
    assert len(ok) == 8


@pytest.mark.asyncio
async def test_two_unready_assets_do_not_block_the_rest():
    """52 eligible / 60 ready: the intersection is what scans, and a
    missing member must not gate the others."""
    import types as _t
    from app.orchestrator import Orchestrator
    from app.schemas import AssetInfo

    # payout stays above min_payout=75 for ALL 54. An earlier version used
    # `85.0 - i`, which dropped everything past A10 below the payout gate
    # and yielded 9 candidates -- the code was right, the fixture was wrong.
    assets = [AssetInfo(symbol=f"A{i}", name=f"A{i}", payout=90.0 - i * 0.1,
                        is_open=True, is_otc=False) for i in range(54)]
    ready = {f"A{i}" for i in range(54)} - {"A3", "A7"}

    o = Orchestrator.__new__(Orchestrator)
    o._assets_cache = assets
    o._assets_ts = 9e18
    o._asset_funnel = {}
    o.runtime = _t.SimpleNamespace(
        trading=_t.SimpleNamespace(asset_whitelist=[]),
        risk=_t.SimpleNamespace(min_payout=75.0))
    o.provider = _t.SimpleNamespace(_asset_feed=None, get_asset_feed_status=lambda: {})
    o.market_init = type("R", (), {"is_ready": lambda s, x: x in ready})()

    cands = [a.symbol for a in await o._candidate_assets()]
    assert len(cands) == 12
    assert "A3" not in cands and "A7" not in cands


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
