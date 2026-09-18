"""Standalone verification for MarketInitManager — no live Quotex/network
connection needed or used. Run with:

    cd backend && python3 test_market_init.py

Uses a FakeProvider that implements just the surface MarketInitManager
actually calls (`.connected`, `.get_candles`), so this exercises the real
state machine, worker pool, retry/backoff, validation, duplicate
protection, background recovery, and reconnect-handling logic end to end,
against the real IndicatorCache and ErrorRecovery this app already ships.

Follows this repo's existing ad-hoc test_*.py convention (see
test_market_provider.py) rather than pytest, which isn't a project
dependency.
"""
import asyncio
import time
from typing import Callable, Dict, List

from app.engine.error_recovery import ErrorRecovery, RetryConfig
from app.engine.incremental import IndicatorCache
from app.schemas import Candle
from app.services.market import TIMEFRAMES
from app.services.market_init import MarketInitManager, AssetInitState, MARKET_INIT_SERVICE_NAME

TF = "1m"
INTERVAL = TIMEFRAMES[TF]

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def make_candles(n: int, gap_at: int = None, dup_at: int = None) -> List[Candle]:
    start = time.time() - n * INTERVAL
    out = []
    ts = start
    for i in range(n):
        if gap_at is not None and i == gap_at:
            ts += INTERVAL * 5  # inject a gap far bigger than 1.5x interval
        if dup_at is not None and i == dup_at:
            ts -= INTERVAL  # duplicate/out-of-order timestamp
        price = 1.0 + 0.001 * (i % 7)  # tiny deterministic variation, not flat
        out.append(Candle(timestamp=ts, open=price, high=price * 1.001,
                           low=price * 0.999, close=price, volume=10.0))
        ts += INTERVAL
    return out


class FakeProvider:
    """Implements only what MarketInitManager actually touches on a
    provider: `.connected` and `.get_candles(asset, timeframe, count)`.
    Per-symbol behavior is scriptable via `self.behaviors`."""

    def __init__(self) -> None:
        self.connected = False
        self._streamed: set = set()
        self.behaviors: Dict[str, Callable] = {}
        self.call_counts: Dict[str, int] = {}
        self.concurrent = 0
        self.max_concurrent = 0

    async def get_candles(self, asset: str, timeframe: str, count: int = 120) -> List[Candle]:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.call_counts[asset] = self.call_counts.get(asset, 0) + 1
        try:
            behavior = self.behaviors.get(asset, _ok)
            return await behavior(asset, self.call_counts[asset])
        finally:
            self.concurrent -= 1
            self._streamed.add((asset, TIMEFRAMES.get(timeframe, 60)))


async def _ok(asset, call_no):
    return make_candles(120)


async def _always_fail(asset, call_no):
    raise ConnectionError(f"simulated broker failure for {asset}")


async def _too_few_candles(asset, call_no):
    return make_candles(10)  # under the 55-bar warm threshold, but well-formed


async def _gappy(asset, call_no):
    return make_candles(120, gap_at=60)


async def _slow(asset, call_no):
    await asyncio.sleep(2.0)
    return make_candles(120)


def make_manager(provider: FakeProvider, **settings_overrides) -> MarketInitManager:
    class _Settings:
        workers = settings_overrides.get("workers", 4)
        max_retries = settings_overrides.get("max_retries", 3)
        retry_backoff_base_seconds = settings_overrides.get("retry_backoff_base_seconds", 0.05)
        recovery_interval_seconds = settings_overrides.get("recovery_interval_seconds", 0.3)
        history_timeout_seconds = settings_overrides.get("history_timeout_seconds", 0.5)
        history_count = 120
        gap_multiplier = 1.5

    class _FakeCandleStore:
        def upsert_many(self, *a, **kw):
            return 0

    error_recovery = ErrorRecovery()
    error_recovery.retry_configs[MARKET_INIT_SERVICE_NAME] = RetryConfig(
        max_retries=0,
        circuit_breaker=error_recovery.create_circuit_breaker(
            f"{MARKET_INIT_SERVICE_NAME}:test", failure_threshold=50, timeout_seconds=5.0,
        ),
    )
    mgr = MarketInitManager(
        user_id="test-user",
        # MERGE: constructor now takes a getter, not an instance (see
        # market_init.py -- switch_provider() rebinds Orchestrator.provider).
        get_provider=lambda: provider,
        indicators=IndicatorCache(),
        candle_store=_FakeCandleStore(),
        error_recovery=error_recovery,
        get_timeframe=lambda: TF,
        get_settings=lambda: _Settings(),
    )
    return mgr


async def wait_until(cond: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if cond():
            return True
        await asyncio.sleep(interval)
    return cond()


async def main() -> None:
    print("=" * 70)
    print("TEST 1 — healthy assets reach READY; worker pool stays bounded")
    print("=" * 70)
    provider = FakeProvider()
    mgr = make_manager(provider, workers=3)
    symbols = [f"HEALTHY{i}" for i in range(10)]

    async def enabled():
        return symbols

    provider.connected = False
    await mgr.start(enabled)
    await asyncio.sleep(0.1)
    check("scanner sees nothing ready before connect", all(not mgr.is_ready(s) for s in symbols))
    provider.connected = True

    ok = await wait_until(lambda: all(mgr.is_ready(s) for s in symbols), timeout=5.0)
    check("all healthy assets reach READY", ok, str({s: mgr.state_of(s).value for s in symbols}))
    check("worker pool never exceeded configured size (3)", provider.max_concurrent <= 3,
          f"max_concurrent={provider.max_concurrent}")
    check("each healthy asset queried at most once (no duplicate scheduling)",
          all(provider.call_counts.get(s, 0) == 1 for s in symbols),
          str(provider.call_counts))

    snap = mgr.snapshot()
    check("snapshot reports queue_length == 0 once settled", snap["queue_length"] == 0, str(snap["queue_length"]))
    check("snapshot by_state counts all 10 as ready", snap["by_state"].get("ready") == 10, str(snap["by_state"]))
    await mgr.stop()

    print()
    print("=" * 70)
    print("TEST 2 — permanently failing asset retries 3x then FAILED")
    print("=" * 70)
    provider = FakeProvider()
    provider.connected = True
    provider.behaviors["BADASSET"] = _always_fail
    mgr = make_manager(provider, max_retries=3, retry_backoff_base_seconds=0.05)

    async def enabled2():
        return ["BADASSET"]

    await mgr.start(enabled2)
    ok = await wait_until(lambda: mgr.state_of("BADASSET") == AssetInitState.FAILED, timeout=5.0)
    check("asset reaches FAILED after exhausting retries", ok, mgr.state_of("BADASSET").value)
    check("exactly 3 attempts were made", provider.call_counts.get("BADASSET") == 3,
          str(provider.call_counts.get("BADASSET")))
    check("retries counter reflects 3 attempts", mgr._records["BADASSET"].retries == 3,
          str(mgr._records["BADASSET"].retries))
    check("scanner correctly excludes the failed asset", not mgr.is_ready("BADASSET"))

    print()
    print("=" * 70)
    print("TEST 3 — background recovery brings a FAILED asset back to READY")
    print("=" * 70)
    # Same manager/provider continues from Test 2 — BADASSET is FAILED.
    # Simulate the underlying condition clearing (e.g. broker recovers).
    provider.behaviors["BADASSET"] = _ok
    ok = await wait_until(lambda: mgr.is_ready("BADASSET"), timeout=5.0)
    check("recovery loop retries FAILED asset and it becomes READY", ok, mgr.state_of("BADASSET").value)
    check("recovery_count was incremented", mgr._records["BADASSET"].recovery_count >= 1,
          str(mgr._records["BADASSET"].recovery_count))
    await mgr.stop()

    print()
    print("=" * 70)
    print("TEST 4 — validation: too-few-candles and gappy data both fail cleanly")
    print("=" * 70)
    provider = FakeProvider()
    provider.connected = True
    provider.behaviors["THINDATA"] = _too_few_candles
    provider.behaviors["GAPPY"] = _gappy
    mgr = make_manager(provider, max_retries=2, retry_backoff_base_seconds=0.02)

    async def enabled4():
        return ["THINDATA", "GAPPY"]

    await mgr.start(enabled4)
    ok = await wait_until(
        lambda: mgr.state_of("THINDATA") == AssetInitState.FAILED
        and mgr.state_of("GAPPY") == AssetInitState.FAILED, timeout=5.0,
    )
    check("insufficient-history asset (< 55 bars) ends FAILED, not stuck forever", ok,
          f"THINDATA={mgr.state_of('THINDATA').value} GAPPY={mgr.state_of('GAPPY').value}")
    check("THINDATA's failure reason mentions warmth", "warm" in (mgr._records["THINDATA"].last_error or ""),
          mgr._records["THINDATA"].last_error)
    check("GAPPY's failure reason mentions the gap", "gap" in (mgr._records["GAPPY"].last_error or ""),
          mgr._records["GAPPY"].last_error)
    await mgr.stop()

    print()
    print("=" * 70)
    print("TEST 5 — slow provider trips the per-attempt timeout, then recovers")
    print("=" * 70)
    provider = FakeProvider()
    provider.connected = True
    provider.behaviors["SLOWASSET"] = _slow
    mgr = make_manager(provider, max_retries=3, retry_backoff_base_seconds=0.05, history_timeout_seconds=0.3)

    async def enabled5():
        return ["SLOWASSET"]

    await mgr.start(enabled5)
    await asyncio.sleep(0.6)
    check("still not ready while provider is slow", not mgr.is_ready("SLOWASSET"))
    provider.behaviors["SLOWASSET"] = _ok
    ok = await wait_until(lambda: mgr.is_ready("SLOWASSET"), timeout=5.0)
    check("recovers to READY once the provider responds promptly again", ok, mgr.state_of("SLOWASSET").value)
    await mgr.stop()

    print()
    print("=" * 70)
    print("TEST 6 — duplicate protection: overlapping ensure_enqueued calls")
    print("=" * 70)
    provider = FakeProvider()
    provider.connected = True

    async def enabled6():
        return []  # manual ensure_enqueued calls below, not the auto-refresh loop

    mgr = make_manager(provider, workers=2)
    await mgr.start(enabled6)
    await asyncio.gather(
        mgr.ensure_enqueued(["A", "B", "C"]),
        mgr.ensure_enqueued(["B", "C", "D"]),
        mgr.ensure_enqueued(["A", "D", "E"]),
    )
    ok = await wait_until(lambda: all(mgr.is_ready(s) for s in "ABCDE"), timeout=5.0)
    check("all 5 symbols reach READY despite overlapping enqueue calls", ok,
          str({s: mgr.state_of(s).value for s in "ABCDE"}))
    check("every symbol was only ever fetched once — no duplicate scheduling",
          all(provider.call_counts.get(s) == 1 for s in "ABCDE"), str(provider.call_counts))
    await mgr.stop()

    print()
    print("=" * 70)
    print("TEST 7 — websocket reconnect re-validates READY assets via the bounded pool")
    print("=" * 70)
    provider = FakeProvider()
    provider.connected = True

    async def enabled7():
        return ["R1", "R2", "R3"]

    mgr = make_manager(provider, workers=2)
    await mgr.start(enabled7)
    ok = await wait_until(lambda: all(mgr.is_ready(s) for s in ("R1", "R2", "R3")), timeout=5.0)
    check("assets ready before simulated reconnect", ok)
    calls_before = dict(provider.call_counts)
    await mgr.handle_reconnect()
    ok = await wait_until(
        lambda: all(mgr.is_ready(s) for s in ("R1", "R2", "R3"))
        and all(provider.call_counts.get(s, 0) > calls_before.get(s, 0) for s in ("R1", "R2", "R3")),
        timeout=5.0,
    )
    check("all assets re-validated and back to READY after reconnect", ok,
          str({s: mgr.state_of(s).value for s in ("R1", "R2", "R3")}))
    check("re-validation went through the pool (never more than configured workers concurrently)",
          provider.max_concurrent <= 2, f"max_concurrent={provider.max_concurrent}")
    await mgr.stop()

    print()
    print("=" * 70)
    print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
    print("=" * 70)
    if FAIL:
        print("FAILED CHECKS:")
        for name in FAIL:
            print(f"  - {name}")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
