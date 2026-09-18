"""Merge verification — exercises the behaviour that the 3-way merge had to
reconcile, not just that the files import.

Covers the two change sets whose supervision implementations were folded
into one (pipeline-fix's restart budget / "stuck" verdict into
reliability-fixes' TaskSupervisor), plus the market-init READY gate in
front of the scan loop.

    cd backend && python3 test_merge_verification.py
"""
import asyncio
import types

from app.engine.task_supervisor import TaskSupervisor

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


async def t1_restarts_on_crash():
    print("\n=== T1 — a crashing loop is restarted automatically ===")
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        await asyncio.sleep(5)

    sup = TaskSupervisor("t1")
    task = sup.supervise("flaky", flaky, base_delay=0.01, max_delay=0.05)
    await asyncio.sleep(0.6)
    snap = sup.snapshot()["flaky"]
    check("crashed loop was restarted", calls["n"] >= 3, f"attempts={calls['n']}")
    check("restart_count tracked", snap["restart_count"] == 2, str(snap))
    check("not marked stuck under budget", snap["stuck"] is False, str(snap))
    sup.stop(); task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def t2_budget_then_stuck():
    print("\n=== T2 — blowing the restart budget stops restarting + fires on_stuck ===")
    calls = {"n": 0}
    stuck_names = []

    async def always_dies():
        calls["n"] += 1
        raise RuntimeError("always")

    sup = TaskSupervisor("t2", on_stuck=stuck_names.append)
    task = sup.supervise("bad", always_dies, base_delay=0.001, max_delay=0.005,
                         max_restarts=3, restart_window=60.0)
    await asyncio.sleep(0.5)
    snap = sup.snapshot()["bad"]
    check("gave up after budget", snap["stuck"] is True, str(snap))
    check("on_stuck callback fired with name", stuck_names == ["bad"], str(stuck_names))
    check("stopped restarting (bounded attempts)", calls["n"] <= 6, f"attempts={calls['n']}")
    check("any_stuck() reports it", sup.any_stuck() is True)
    check("stuck_names() reports it", sup.stuck_names() == ["bad"])
    n_after = calls["n"]
    await asyncio.sleep(0.2)
    check("truly stopped (no further attempts)", calls["n"] == n_after,
          f"{n_after} -> {calls['n']}")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def t3_is_active_clean_return():
    print("\n=== T3 — clean return while still active is treated as a crash ===")
    calls = {"n": 0}
    active = {"v": True}

    async def returns_early():
        calls["n"] += 1
        return  # bug: returns while owner still considers itself running

    sup = TaskSupervisor("t3")
    task = sup.supervise("early", returns_early, base_delay=0.001, max_delay=0.005,
                         max_restarts=4, restart_window=60.0,
                         is_active=lambda: active["v"])
    await asyncio.sleep(0.3)
    check("early return was restarted, not trusted", calls["n"] > 1, f"attempts={calls['n']}")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # And the inverse: a clean return while NOT active must NOT restart.
    calls2 = {"n": 0}

    async def graceful():
        calls2["n"] += 1
        return

    sup2 = TaskSupervisor("t3b")
    task2 = sup2.supervise("graceful", graceful, base_delay=0.001,
                           is_active=lambda: False)
    await asyncio.sleep(0.2)
    check("graceful stop is NOT restarted", calls2["n"] == 1, f"attempts={calls2['n']}")
    check("graceful task reports not running", sup2.snapshot()["graceful"]["running"] is False)
    task2.cancel()
    try:
        await task2
    except asyncio.CancelledError:
        pass


async def t4_stop_prevents_restart():
    print("\n=== T4 — stop() prevents any further restart ===")
    calls = {"n": 0}

    async def dies():
        calls["n"] += 1
        await asyncio.sleep(0.02)
        raise RuntimeError("x")

    sup = TaskSupervisor("t4")
    task = sup.supervise("d", dies, base_delay=0.005, max_delay=0.01)
    await asyncio.sleep(0.1)
    sup.stop()
    n = calls["n"]
    await asyncio.sleep(0.25)
    check("no restarts after stop()", calls["n"] <= n + 1, f"{n} -> {calls['n']}")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def t5_ready_gating():
    print("\n=== T5 — scan loop only sees assets MarketInitManager reports READY ===")
    from app.orchestrator import Orchestrator

    class A:
        def __init__(self, symbol, payout):
            self.symbol, self.payout = symbol, payout
            self.is_open = True

    assets = [A(f"S{i}", 90 - i) for i in range(20)]
    ready = {"S0", "S1", "S5", "S9"}

    stub = types.SimpleNamespace()

    async def _eligible():
        return list(assets)

    stub._eligible_assets = _eligible
    stub.market_init = types.SimpleNamespace(is_ready=lambda s: s in ready)

    got = await Orchestrator._candidate_assets(stub)
    syms = {a.symbol for a in got}
    check("only READY assets are handed to the scanner", syms == ready, str(syms))
    check("non-READY assets are skipped, not blocked on", len(got) == len(ready))
    check("still payout-sorted", [a.payout for a in got] == sorted([a.payout for a in got], reverse=True))

    # Nothing READY yet (cold start) must yield an empty list, not an error
    # and not the unfiltered universe.
    ready.clear()
    got2 = await Orchestrator._candidate_assets(stub)
    check("cold start yields no candidates rather than ungated ones", got2 == [], str(got2))


def t6_single_supervision_mechanism():
    print("\n=== T6 — exactly one supervision mechanism, no duplicate loops ===")
    import inspect
    from app import orchestrator as orch_mod

    src = inspect.getsource(orch_mod)
    check("pipeline-fix's inline _run_supervised is gone",
          "async def _run_supervised" not in src)
    check("pipeline-fix's duplicate _task_health dict is gone",
          "self._task_health: Dict" not in src)
    check("pipeline_health() exists (API contract preserved)",
          hasattr(orch_mod.Orchestrator, "pipeline_health"))
    check("_on_task_stuck wired", hasattr(orch_mod.Orchestrator, "_on_task_stuck"))

    start_src = inspect.getsource(orch_mod.Orchestrator.start)
    check("each core loop started exactly once",
          start_src.count(".supervise(") == 5 and "create_task(self._scan_loop" not in start_src,
          f"supervise calls={start_src.count('.supervise(')}")

    from app.services import market_init as mi_mod
    mi_src = inspect.getsource(mi_mod)
    check("market_init background tasks are supervised too",
          "self._supervisor.supervise(" in mi_src and "asyncio.create_task(self._run())" not in mi_src)


async def main():
    await t1_restarts_on_crash()
    await t2_budget_then_stuck()
    await t3_is_active_clean_return()
    await t4_stop_prevents_restart()
    await t5_ready_gating()
    t6_single_supervision_mechanism()
    print("\n" + "=" * 60)
    print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
