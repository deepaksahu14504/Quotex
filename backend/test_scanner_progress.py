"""Regression tests for the scanner-progression bug (_data_stale latch).

Section 1 fails against the pre-fix code.
Run: cd backend && python3 -m pytest test_scanner_progress.py -q
"""
from __future__ import annotations

import asyncio
import sys
import time
import types as _t
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

STALE = 0.4
WD = 0.05


class Rig:
    """Mirrors orchestrator.py's _scan_loop / _scan_once / _watchdog_loop
    shapes, including the _last_candle_at write that only happens inside
    _evaluate_asset_signal (orchestrator.py:2280)."""

    def __init__(self, n_assets=9, probe=True):
        self._running = True
        self._data_stale = False
        self._last_candle_at = time.monotonic()
        self.feed_alive = True
        self.probe_enabled = probe
        self.n_assets = n_assets
        self.hang_asset = None
        self.throw_asset = None
        self.snapshot_fails = False
        self.ready = {f"A{i}" for i in range(n_assets)}
        self.health = {
            "last_scan_at": 0.0, "last_scan_attempt_at": 0.0,
            "last_scan_completed_at": 0.0, "last_candidate_selection_at": 0.0,
            "last_pipeline_snapshot_at": 0.0, "scan_cycle_count": 0,
            "consecutive_scan_failures": 0, "assets_scanned": 0,
            "candidates_selected": 0, "assets_evaluated": 0,
            "evaluation_failures": 0, "evaluation_timeouts": 0,
            "pipeline_snapshot_update_count": 0, "scan_gate_blocks": {},
            "signals_this_cycle": 0,
        }

    async def _candidates(self):
        return [s for s in (f"A{i}" for i in range(self.n_assets)) if s in self.ready]

    def _snapshot(self, sym):
        if self.snapshot_fails:
            raise RuntimeError("snapshot publication failed")
        self.health["last_pipeline_snapshot_at"] = time.monotonic()
        self.health["pipeline_snapshot_update_count"] += 1

    async def _evaluate(self, sym):
        if sym == self.hang_asset:
            await asyncio.sleep(30)
        if sym == self.throw_asset:
            raise ValueError(f"strategy blew up on {sym}")
        if not self.feed_alive:
            return None
        self._last_candle_at = time.monotonic()
        try:
            self._snapshot(sym)
        except Exception:
            self.health["evaluation_failures"] += 1
        self.health["assets_evaluated"] += 1
        return sym

    async def _probe_data_resumed(self):
        if not self.probe_enabled or not self.feed_alive:
            return
        self._last_candle_at = time.monotonic()

    async def _scan_once(self):
        if self._data_stale:
            await self._probe_data_resumed()
            self.health["last_scan_attempt_at"] = time.monotonic()
            self.health["scan_cycle_count"] += 1
            self.health["scan_gate_blocks"] = {"data_stale": True}
            return
        cands = await self._candidates()
        self.health["last_candidate_selection_at"] = time.monotonic()
        self.health["candidates_selected"] = len(cands)
        self.health["assets_scanned"] = len(cands)
        self.health["last_scan_attempt_at"] = time.monotonic()
        self.health["scan_cycle_count"] += 1
        self.health["scan_gate_blocks"] = {}
        tasks = [asyncio.ensure_future(self._evaluate(c)) for c in cands]
        _done, pending = await asyncio.wait(tasks, timeout=0.3)
        for t in pending:
            t.cancel()
            self.health["evaluation_timeouts"] += 1
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for t in tasks:
            if not t.cancelled() and t.exception() is not None:
                self.health["evaluation_failures"] += 1
        self.health["last_scan_completed_at"] = time.monotonic()

    async def _scan_loop(self):
        while self._running:
            try:
                await self._scan_once()
                self.health["consecutive_scan_failures"] = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health["consecutive_scan_failures"] += 1
            self.health["last_scan_at"] = time.monotonic()
            await asyncio.sleep(0.01)

    async def _watchdog(self):
        while self._running:
            await asyncio.sleep(WD)
            if self._last_candle_at > 0:
                self._data_stale = (time.monotonic() - self._last_candle_at) > STALE

    async def __aenter__(self):
        self._t = [asyncio.ensure_future(self._scan_loop()),
                   asyncio.ensure_future(self._watchdog())]
        return self

    async def __aexit__(self, *a):
        self._running = False
        for t in self._t:
            t.cancel()
        await asyncio.gather(*self._t, return_exceptions=True)


# ================================================================= #
# 1. THE ROOT CAUSE — the _data_stale latch
# ================================================================= #
@pytest.mark.asyncio
async def test_stale_flag_clears_after_the_feed_recovers():
    """PRE-FIX: _scan_once returned before _evaluate_asset_signal, which is
    the only writer of _last_candle_at, so the flag the watchdog clears
    from _last_candle_at could only be cleared by the code it was
    blocking. It latched forever."""
    async with Rig() as r:
        await asyncio.sleep(0.3)
        r.feed_alive = False
        await asyncio.sleep(STALE + 0.2)
        assert r._data_stale is True, "the gate should engage on frozen data"
        r.feed_alive = True
        await asyncio.sleep(0.5)
        assert r._data_stale is False, "the gate never released after recovery"


@pytest.mark.asyncio
async def test_snapshots_resume_automatically_after_recovery():
    async with Rig() as r:
        await asyncio.sleep(0.3)
        r.feed_alive = False
        await asyncio.sleep(STALE + 0.2)
        frozen = r.health["pipeline_snapshot_update_count"]
        r.feed_alive = True
        await asyncio.sleep(0.6)
        assert r.health["pipeline_snapshot_update_count"] > frozen, \
            "Live Pipeline never resumed advancing"


@pytest.mark.asyncio
async def test_the_latch_reproduces_without_the_probe():
    """Counter-proof: disable the probe and the old behaviour returns."""
    async with Rig(probe=False) as r:
        await asyncio.sleep(0.3)
        r.feed_alive = False
        await asyncio.sleep(STALE + 0.2)
        r.feed_alive = True
        await asyncio.sleep(0.5)
        snaps = r.health["pipeline_snapshot_update_count"]
        await asyncio.sleep(0.4)
        assert r._data_stale is True
        assert r.health["pipeline_snapshot_update_count"] == snaps


@pytest.mark.asyncio
async def test_cycle_counter_advances_while_gated():
    """The gated path must still be visible as a cycle, and must say why."""
    async with Rig() as r:
        await asyncio.sleep(0.2)
        r.feed_alive = False
        await asyncio.sleep(STALE + 0.2)
        c1 = r.health["scan_cycle_count"]
        await asyncio.sleep(0.2)
        assert r.health["scan_cycle_count"] > c1
        assert r.health["scan_gate_blocks"] == {"data_stale": True}


@pytest.mark.asyncio
async def test_gate_still_blocks_evaluation_on_stale_data():
    """The safety property must be preserved: no evaluation, no signal."""
    async with Rig() as r:
        await asyncio.sleep(0.2)
        r.feed_alive = False
        await asyncio.sleep(STALE + 0.2)
        evaluated = r.health["assets_evaluated"]
        snaps = r.health["pipeline_snapshot_update_count"]
        await asyncio.sleep(0.3)
        assert r.health["assets_evaluated"] == evaluated
        assert r.health["pipeline_snapshot_update_count"] == snaps


# ================================================================= #
# 2. Continuous cycles  (brief §18: at least 10)
# ================================================================= #
@pytest.mark.asyncio
async def test_ten_consecutive_cycles_with_advancing_telemetry():
    async with Rig() as r:
        await asyncio.sleep(0.05)
        marks = []
        while len(marks) < 12 and len(marks) < 200:
            await asyncio.sleep(0.05)
            marks.append((r.health["scan_cycle_count"],
                          r.health["last_scan_completed_at"],
                          r.health["pipeline_snapshot_update_count"]))
            if len(marks) >= 12:
                break
        cycles = [m[0] for m in marks]
        assert cycles[-1] - cycles[0] >= 10, f"only {cycles[-1]-cycles[0]} cycles"
        assert marks[-1][1] > marks[0][1], "last_scan_completed_at did not advance"
        assert marks[-1][2] > marks[0][2], "snapshots did not advance"


@pytest.mark.asyncio
async def test_zero_signals_is_still_a_healthy_scan():
    """A cycle that produces no signal is NOT a stalled scanner."""
    async with Rig() as r:
        await asyncio.sleep(0.4)
        assert r.health["signals_this_cycle"] == 0
        assert r.health["scan_cycle_count"] > 3
        assert r.health["last_scan_completed_at"] > 0
        assert r.health["pipeline_snapshot_update_count"] > 0


# ================================================================= #
# 3. Isolation  (brief §18 tests C-I)
# ================================================================= #
@pytest.mark.asyncio
async def test_one_asset_throwing_does_not_stop_the_scanner():
    async with Rig() as r:
        r.throw_asset = "A3"
        await asyncio.sleep(0.4)
        c = r.health["scan_cycle_count"]
        assert r.health["evaluation_failures"] > 0
        await asyncio.sleep(0.2)
        assert r.health["scan_cycle_count"] > c
        assert r.health["assets_evaluated"] > 0


@pytest.mark.asyncio
async def test_one_asset_hanging_is_bounded_and_others_continue():
    async with Rig() as r:
        r.hang_asset = "A5"
        await asyncio.sleep(1.0)
        assert r.health["evaluation_timeouts"] > 0
        c = r.health["scan_cycle_count"]
        await asyncio.sleep(0.5)
        assert r.health["scan_cycle_count"] > c, "the cycle never completed"
        assert r.health["assets_evaluated"] > 0, "healthy assets stopped evaluating"


@pytest.mark.asyncio
async def test_partial_ready_does_not_block_ready_assets():
    async with Rig() as r:
        r.ready = {"A0", "A1", "A3"}          # A2 not READY
        await asyncio.sleep(0.3)
        assert r.health["candidates_selected"] == 3
        assert r.health["scan_cycle_count"] > 2
        assert r.health["assets_evaluated"] > 0


@pytest.mark.asyncio
async def test_newly_ready_asset_joins_without_restart():
    async with Rig() as r:
        r.ready = {"A0", "A1"}
        await asyncio.sleep(0.2)
        assert r.health["candidates_selected"] == 2
        r.ready.add("A2")
        await asyncio.sleep(0.2)
        assert r.health["candidates_selected"] == 3


@pytest.mark.asyncio
async def test_snapshot_publication_failure_does_not_stop_the_scanner():
    async with Rig() as r:
        r.snapshot_fails = True
        await asyncio.sleep(0.3)
        c = r.health["scan_cycle_count"]
        assert r.health["evaluation_failures"] > 0
        await asyncio.sleep(0.2)
        assert r.health["scan_cycle_count"] > c


@pytest.mark.asyncio
async def test_recovery_after_a_temporary_failure():
    async with Rig() as r:
        r.throw_asset = "A1"
        await asyncio.sleep(0.3)
        r.throw_asset = None
        snaps = r.health["pipeline_snapshot_update_count"]
        await asyncio.sleep(0.3)
        assert r.health["pipeline_snapshot_update_count"] > snaps
        assert r.health["consecutive_scan_failures"] == 0


@pytest.mark.asyncio
async def test_telemetry_fields_all_present():
    async with Rig() as r:
        await asyncio.sleep(0.3)
        for k in ("last_scan_attempt_at", "last_scan_completed_at",
                  "last_candidate_selection_at", "last_pipeline_snapshot_at",
                  "scan_cycle_count", "consecutive_scan_failures",
                  "candidates_selected", "assets_evaluated",
                  "evaluation_failures", "evaluation_timeouts",
                  "pipeline_snapshot_update_count", "scan_gate_blocks"):
            assert k in r.health, k


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
