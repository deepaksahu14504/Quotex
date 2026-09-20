"""SOAK TEST — continuous pipeline progress under realistic degradation.

Drives the fixed scan-cycle structure continuously and injects the exact
failure modes that used to wedge it, asserting that progress keeps
advancing throughout and afterwards.

Run:  cd backend && python3 soak_pipeline.py [seconds]
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# RCA F1: resolve the vendored broker library instead of assuming
# vendor/pyquotex exists (it is empty in a fresh clone).
from app.pyquotex_vendor import ensure_pyquotex_on_path
ensure_pyquotex_on_path()

from app.engine.task_supervisor import TaskSupervisor  # noqa: E402

SCAN_CYCLE_BUDGET = 0.6          # scaled down from prod 90s
SCAN_PROGRESS_STALL = 2.0        # scaled down from prod 180s
CANDIDATES = [f"ASSET-{i}" for i in range(12)]


class Rig:
    """Mirrors the fixed structures: bounded provider calls, budgeted
    gather, real-progress watchdog, TaskSupervisor."""

    def __init__(self):
        self._running = True
        self.health = {
            "last_scan_at": 0.0, "cycles": 0, "assets_evaluated": 0,
            "scan_stalled_assets": 0, "scan_stall_recoveries": 0, "scan_errors": 0,
        }
        self.mode = "healthy"        # healthy | dead_socket | slow | flaky | reconnecting
        self._supervisor = TaskSupervisor("soak")
        self._scan_task = None
        self._watchdog_task = None
        self._last_scan_recovery_at = 0.0
        self.max_concurrent_scans = 0
        self._in_scan = 0

    # ---- provider stand-in: every call bounded, as in the fix ----
    async def _provider_get_candles(self, symbol):
        if self.mode == "dead_socket":
            # Post-fix send_websocket_request raises rather than hanging.
            await asyncio.sleep(0.05)
            raise TimeoutError("transport considered dead")
        if self.mode == "slow" and symbol == "ASSET-7":
            await asyncio.sleep(5.0)          # exceeds the cycle budget
        if self.mode == "flaky" and symbol in ("ASSET-2", "ASSET-9"):
            raise ConnectionError("transient provider failure")
        if self.mode == "reconnecting":
            await asyncio.sleep(0.02)
            raise ConnectionError("not connected")
        await asyncio.sleep(0.002)
        return [symbol]

    async def _evaluate_asset_signal(self, symbol):
        candles = await self._provider_get_candles(symbol)
        self.health["assets_evaluated"] += 1
        return symbol, candles

    async def _scan_once(self):
        self._in_scan += 1
        self.max_concurrent_scans = max(self.max_concurrent_scans, self._in_scan)
        try:
            tasks = [asyncio.ensure_future(self._evaluate_asset_signal(a)) for a in CANDIDATES]
            _done, pending = await asyncio.wait(tasks, timeout=SCAN_CYCLE_BUDGET)
            if pending:
                self.health["scan_stalled_assets"] = len(pending)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            else:
                self.health["scan_stalled_assets"] = 0
            for t in tasks:
                if t.cancelled():
                    continue
                if t.exception() is not None:
                    self.health["scan_errors"] += 1
        finally:
            self._in_scan -= 1

    async def _scan_loop(self):
        while self._running:
            try:
                await self._scan_once()
                self.health["last_scan_at"] = time.monotonic()
                self.health["cycles"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health["scan_errors"] += 1
            await asyncio.sleep(0.01)

    async def _watchdog_loop(self):
        while self._running:
            await asyncio.sleep(0.1)
            last = self.health.get("last_scan_at") or 0.0
            if last <= 0:
                continue
            age = time.monotonic() - last
            if age <= SCAN_PROGRESS_STALL:
                continue
            if time.monotonic() - self._last_scan_recovery_at < SCAN_PROGRESS_STALL:
                continue
            self._last_scan_recovery_at = time.monotonic()
            self.health["scan_stall_recoveries"] += 1
            old = self._scan_task
            if old is not None and not old.done():
                old.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(old), timeout=1)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            self.health["last_scan_at"] = time.monotonic()
            self._scan_task = self._supervisor.supervise(
                "scan", self._scan_loop, is_active=lambda: self._running
            )

    def start(self):
        self._scan_task = self._supervisor.supervise(
            "scan", self._scan_loop, is_active=lambda: self._running
        )
        self._watchdog_task = self._supervisor.supervise(
            "watchdog", self._watchdog_loop, is_active=lambda: self._running
        )


def scan_task_count():
    return len([t for t in asyncio.all_tasks() if not t.done() and "_scan_loop" in repr(t)])


async def main(duration=30.0):
    rig = Rig()
    rig.start()
    print("=" * 72)
    print(f"SOAK TEST — {duration:.0f}s continuous run with injected degradation")
    print("=" * 72)

    phases = [
        ("healthy",      0.20, "normal continuous scanning"),
        ("flaky",        0.12, "transient provider failures on 2 assets"),
        ("healthy",      0.10, "recovery"),
        ("slow",         0.18, "one asset exceeds the cycle budget"),
        ("healthy",      0.10, "recovery"),
        ("dead_socket",  0.12, "half-open socket — every send times out"),
        ("reconnecting", 0.08, "reconnect in progress"),
        ("healthy",      0.10, "final recovery"),
    ]

    samples = []
    failures = []
    t_start = time.monotonic()

    for mode, frac, label in phases:
        rig.mode = mode
        phase_len = duration * frac
        c0 = rig.health["cycles"]
        t0 = time.monotonic()
        last_seen = rig.health["last_scan_at"]
        max_gap = 0.0
        while time.monotonic() - t0 < phase_len:
            await asyncio.sleep(0.1)
            cur = rig.health["last_scan_at"]
            if cur != last_seen:
                last_seen = cur
            gap = time.monotonic() - last_seen
            max_gap = max(max_gap, gap)
            samples.append((mode, rig.health["cycles"]))
        advanced = rig.health["cycles"] - c0
        dup = scan_task_count()
        ok = advanced > 0 and dup <= 1
        if not ok:
            failures.append(f"{mode}: advanced={advanced} scan_tasks={dup}")
        print(f"  {'OK  ' if ok else 'FAIL'}  {mode:13s} {label:45s} "
              f"cycles+{advanced:4d}  max_progress_gap={max_gap:5.2f}s  scan_tasks={dup}")

    elapsed = time.monotonic() - t_start
    snap = rig._supervisor.snapshot()

    print("\n" + "-" * 72)
    print(f"  total cycles              : {rig.health['cycles']}")
    print(f"  assets evaluated          : {rig.health['assets_evaluated']}")
    print(f"  scan errors (handled)     : {rig.health['scan_errors']}")
    print(f"  stall recoveries          : {rig.health['scan_stall_recoveries']}")
    print(f"  supervisor restarts       : {snap['scan']['restart_count']} scan, "
          f"{snap['watchdog']['restart_count']} watchdog")
    print(f"  supervisor stuck flags    : scan={snap['scan']['stuck']} "
          f"watchdog={snap['watchdog']['stuck']}")
    print(f"  live _scan_loop tasks     : {scan_task_count()}")
    print(f"  max concurrent _scan_once : {rig.max_concurrent_scans}")
    print("-" * 72)

    # ---- assertions ----
    checks = [
        ("scan cycles kept advancing through every phase", not failures),
        ("no duplicate scanner tasks ever appeared", rig.max_concurrent_scans <= 1),
        ("exactly one live scan loop at the end", scan_task_count() <= 1),
        ("no uncontrolled restart loop", snap["scan"]["restart_count"] <= 3),
        ("scan loop never declared permanently stuck", snap["scan"]["stuck"] is False),
        ("watchdog stayed healthy throughout", snap["watchdog"]["running"] is True),
        ("progress marker is fresh at the end",
         time.monotonic() - rig.health["last_scan_at"] < 2.0),
        ("errors were surfaced, not swallowed", rig.health["scan_errors"] > 0),
    ]
    print()
    bad = 0
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            bad += 1

    # graceful shutdown must NOT be treated as a stall
    rig._running = False
    await asyncio.sleep(0.3)
    final = rig._supervisor.snapshot()
    graceful = final["scan"]["restart_count"] == snap["scan"]["restart_count"]
    print(f"  {'PASS' if graceful else 'FAIL'}  intentional shutdown did not trigger a restart")
    if not graceful:
        bad += 1
    for t in (rig._scan_task, rig._watchdog_task):
        if t:
            t.cancel()
    await asyncio.gather(rig._scan_task, rig._watchdog_task, return_exceptions=True)

    print("\n" + "=" * 72)
    print(f"SOAK RESULT: {len(checks) + 1 - bad}/{len(checks) + 1} checks passed "
          f"over {elapsed:.0f}s")
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    raise SystemExit(asyncio.run(main(dur)))
