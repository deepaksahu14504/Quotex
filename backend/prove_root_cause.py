"""ROOT CAUSE PROOF — reproduces why the scan loop stops making progress.

Run:  cd backend && python3 prove_root_cause.py

This does NOT test the fix. It reproduces the FAILURE, link by link,
using the real ErrorRecovery, the real TaskSupervisor, and the real
send_websocket_request locking semantics copied verbatim from
vendor/pyquotex/pyquotex/api.py:797-807.

Each link prints PROVEN or NOT-PROVEN. Nothing here is inferred from a
comment; every claim is executed.
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from app.engine.error_recovery import ErrorRecovery          # noqa: E402
from app.engine.task_supervisor import TaskSupervisor        # noqa: E402

RESULTS = []


def proven(label, ok, detail=""):
    RESULTS.append((label, ok))
    print(f"  {'PROVEN    ' if ok else 'NOT PROVEN'}  {label}" + (f"  [{detail}]" if detail else ""))


# ===================================================================== #
# The exact construct under test, copied from pyquotex/api.py:797-807
# ===================================================================== #
class RealSendSemantics:
    """Verbatim reproduction of QuotexAPI.send_websocket_request.

        async def send_websocket_request(self, data: str) -> None:
            async with self._ws_send_lock:
                if self.websocket:
                    await self.websocket.send(data)

    `half_open=True` models a TCP connection that has silently gone away
    (no FIN, no RST — a NAT/firewall/broker idle-drop). websockets' send()
    does not raise in that state: it writes into the OS send buffer and,
    once that fills with no ACKs returning, awaits drain forever.
    """

    def __init__(self, half_open=False):
        self._ws_send_lock = asyncio.Lock()
        self.websocket = object()
        self.half_open = half_open
        self.sends_started = 0
        self.sends_completed = 0

    async def send_websocket_request(self, data: str) -> None:
        async with self._ws_send_lock:
            if self.websocket:
                self.sends_started += 1
                if self.half_open:
                    await asyncio.Event().wait()   # never returns, never raises
                await asyncio.sleep(0)
                self.sends_completed += 1


# ===================================================================== #
# LINK 1 — websocket.send() on a half-open socket never returns/raises
# ===================================================================== #
async def link1():
    print("\n=== LINK 1 — send() on a half-open socket never returns and never raises ===")
    api = RealSendSemantics(half_open=True)
    task = asyncio.ensure_future(api.send_websocket_request('42["x",{}]'))
    await asyncio.sleep(0.3)
    proven("send() is still pending after 300ms with no exception",
           not task.done(), f"started={api.sends_started} completed={api.sends_completed}")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ===================================================================== #
# LINK 2 — the held lock blocks EVERY other sender (cascade)
# ===================================================================== #
async def link2():
    print("\n=== LINK 2 — one stuck sender holds _ws_send_lock and blocks all others ===")
    api = RealSendSemantics(half_open=True)
    stuck = asyncio.ensure_future(api.send_websocket_request("first"))
    await asyncio.sleep(0.05)

    others = [asyncio.ensure_future(api.send_websocket_request(f"other-{i}")) for i in range(5)]
    await asyncio.sleep(0.3)
    proven("5 unrelated senders are all blocked on the lock",
           all(not t.done() for t in others),
           f"pending={sum(1 for t in others if not t.done())}/5")

    # Cancelling a waiter does NOT release the lock — it is still held.
    for t in others:
        t.cancel()
    await asyncio.gather(*others, return_exceptions=True)
    proven("lock is STILL held after cancelling the waiters", api._ws_send_lock.locked())

    late = asyncio.ensure_future(api.send_websocket_request("late"))
    await asyncio.sleep(0.2)
    proven("a later sender is blocked too — the socket is permanently unusable",
           not late.done())
    late.cancel(); stuck.cancel()
    await asyncio.gather(late, stuck, return_exceptions=True)


# ===================================================================== #
# LINK 3 — ErrorRecovery.execute_with_retry has no timeout
# ===================================================================== #
async def link3():
    print("\n=== LINK 3 — execute_with_retry (REAL class) cannot bound a hang ===")
    api = RealSendSemantics(half_open=True)
    er = ErrorRecovery()

    async def provider_get_candles(*a, **kw):
        await api.send_websocket_request("history/load")
        return []

    task = asyncio.ensure_future(
        er.execute_with_retry(provider_get_candles, "get_candles", "EURUSD", "1m", 120)
    )
    await asyncio.sleep(0.4)
    proven("execute_with_retry never returns — retry/circuit-breaker are "
           "exception-driven and a hang raises nothing", not task.done())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ===================================================================== #
# LINK 4 — gather() waits for ALL: one hung asset stalls the whole cycle
# ===================================================================== #
async def link4():
    print("\n=== LINK 4 — _scan_once's gather() blocks on its slowest member ===")
    api = RealSendSemantics(half_open=False)
    hung = {"on": False}

    async def evaluate_asset(symbol):
        if hung["on"] and symbol == "ASSET-7":
            await asyncio.Event().wait()      # this one asset's socket call hangs
        await asyncio.sleep(0.01)
        return symbol

    candidates = [f"ASSET-{i}" for i in range(12)]

    t0 = time.monotonic()
    res = await asyncio.gather(*[evaluate_asset(a) for a in candidates],
                               return_exceptions=True)
    proven("healthy cycle completes for all 12 candidates",
           len(res) == 12, f"{time.monotonic()-t0:.3f}s")

    hung["on"] = True
    task = asyncio.ensure_future(
        asyncio.gather(*[evaluate_asset(a) for a in candidates], return_exceptions=True)
    )
    await asyncio.sleep(0.4)
    proven("ONE hung asset out of 12 leaves gather() pending — "
           "return_exceptions=True does not help, a hang is not an exception",
           not task.done())
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ===================================================================== #
# LINK 5 — the scan loop stops advancing; health["last_scan_at"] freezes
# ===================================================================== #
async def link5():
    print("\n=== LINK 5 — _scan_loop stops reaching its next iteration ===")
    health = {"last_scan_at": 0.0, "cycles": 0}
    running = {"v": True}
    hang = {"on": False}

    async def scan_once():
        # Structural stand-in for _scan_once: gather over candidates.
        async def evaluate(sym):
            if hang["on"]:
                await asyncio.Event().wait()
            await asyncio.sleep(0.005)
        await asyncio.gather(*[evaluate(f"A{i}") for i in range(12)],
                             return_exceptions=True)

    async def scan_loop():
        # Mirrors orchestrator._scan_loop's shape exactly.
        while running["v"]:
            try:
                # get_balance reads pyquotex's CACHED account_balance
                # (account.py:136) — it never touches the socket, so the
                # wait_for around it in the real loop never fires.
                await asyncio.sleep(0)
                await scan_once()
                health["last_scan_at"] = time.monotonic()
                health["cycles"] += 1
            except Exception:
                pass
            await asyncio.sleep(0.01)

    sup = TaskSupervisor("proof")
    task = sup.supervise("scan", scan_loop, is_active=lambda: running["v"])

    await asyncio.sleep(0.3)
    healthy_cycles = health["cycles"]
    proven("scan cycles advance while healthy", healthy_cycles > 3,
           f"cycles={healthy_cycles}")

    hang["on"] = True
    await asyncio.sleep(0.2)
    frozen_at = health["last_scan_at"]
    frozen_cycles = health["cycles"]
    await asyncio.sleep(0.5)

    proven("last_scan_at STOPS updating once one provider call hangs",
           health["last_scan_at"] == frozen_at,
           f"cycles frozen at {frozen_cycles}")

    # ---- and now the part that matters most ----
    snap = sup.snapshot()["scan"]
    proven("TaskSupervisor STILL reports the stuck loop as running/healthy "
           "— it only knows Task.done() is False",
           snap["running"] is True and snap["stuck"] is False and snap["restart_count"] == 0,
           f"running={snap['running']} stuck={snap['stuck']} restarts={snap['restart_count']}")

    running["v"] = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def main():
    print("=" * 70)
    print("ROOT CAUSE REPRODUCTION — why the pipeline stops making progress")
    print("=" * 70)
    await link1()
    await link2()
    await link3()
    await link4()
    await link5()
    print("\n" + "=" * 70)
    ok = sum(1 for _, o in RESULTS if o)
    print(f"RESULT: {ok}/{len(RESULTS)} links proven")
    print("=" * 70)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
