"""Revalidation must not veto a trade just because a network fetch was slow."""
import asyncio, sys, types, logging
sys.path.insert(0, '.')
logging.disable(logging.CRITICAL)
from app.orchestrator import Orchestrator, REVALIDATION_FETCH_TIMEOUT_SECONDS
from app.schemas import Candle

def mk(n):
    return [Candle(timestamp=i*60, open=1.1, high=1.11, low=1.09, close=1.1, volume=1.0)
            for i in range(n)]

class FakeProvider:
    def __init__(self, mode): self.mode = mode
    async def get_candles(self, a, tf, n):
        if self.mode == "hang":
            await asyncio.sleep(REVALIDATION_FETCH_TIMEOUT_SECONDS + 5)
        if self.mode == "empty":
            return []
        return mk(120)

class FakeStore:
    def __init__(self, n): self.n = n
    def get_latest_n(self, a, tf, c): return mk(self.n)

async def main():
    o = object.__new__(Orchestrator)          # no __init__: we only need the method

    # 1. healthy live fetch -> live candles
    o.provider, o.candle_store = FakeProvider("ok"), FakeStore(0)
    r = await o._revalidation_candles("EURUSD", "1m")
    print(f"live fetch ok        -> {len(r)} bars (from network)")
    assert len(r) == 120

    # 2. live fetch hangs -> falls back to the store instead of vetoing
    o.provider, o.candle_store = FakeProvider("hang"), FakeStore(120)
    import time; t0 = time.time()
    r = await o._revalidation_candles("EURUSD", "1m")
    el = time.time() - t0
    print(f"live fetch hangs     -> {len(r)} bars (from store) in {el:.1f}s")
    assert len(r) == 120 and el < REVALIDATION_FETCH_TIMEOUT_SECONDS + 2

    # 3. live empty -> store
    o.provider, o.candle_store = FakeProvider("empty"), FakeStore(90)
    r = await o._revalidation_candles("EURUSD", "1m")
    print(f"live fetch empty     -> {len(r)} bars (from store)")
    assert len(r) == 90

    # 4. both empty -> nothing, and the veto is then correct
    o.provider, o.candle_store = FakeProvider("empty"), FakeStore(0)
    r = await o._revalidation_candles("EURUSD", "1m")
    print(f"both unavailable     -> {len(r)} bars (veto is correct here)")
    assert r == []

    print("\nBEFORE: a slow fetch returned None -> 'data_insufficient' -> trade vetoed")
    print("AFTER : a slow fetch falls back to candles already on disk")
    print("\nREVALIDATION FALLBACK CHECKS PASSED")

asyncio.run(main())
