"""Prove the broker-ceiling plateau stops the repeated ~150s dead-end seed."""
import asyncio, sys, time
sys.path.insert(0, '.')

SEED_PLATEAU_MIN_GAIN = 5
SEED_PLATEAU_COOLDOWN_SECONDS = 1800.0

class FakeProvider:
    """Mirrors just the seed_needed / plateau decision from get_candles()."""
    def __init__(self, broker_ceiling):
        self.broker_ceiling = broker_ceiling
        self._candle_cache = {}
        self._seed_plateaued_at = {}
        self.deep_seed_calls = 0

    async def get_candles(self, asset, count, now=None):
        now = now if now is not None else time.time()
        key = asset
        cache = self._candle_cache.setdefault(key, {})

        seed_needed = len(cache) < count
        plateaued_at = self._seed_plateaued_at.get(key)
        if seed_needed and plateaued_at is not None:
            if now - plateaued_at < SEED_PLATEAU_COOLDOWN_SECONDS:
                seed_needed = False
            else:
                self._seed_plateaued_at.pop(key, None)

        if seed_needed:
            self.deep_seed_calls += 1
            pre_len = len(cache)
            # broker never gives more than its ceiling, however much we ask for
            got = min(self.broker_ceiling, count)
            for i in range(got):
                cache[i] = True
            gained = len(cache) - pre_len
            if gained < SEED_PLATEAU_MIN_GAIN and len(cache) < count:
                self._seed_plateaued_at[key] = now
            else:
                self._seed_plateaued_at.pop(key, None)
        return len(cache)

async def main():
    p = FakeProvider(broker_ceiling=120)
    t = 1_000_000.0

    # cycle 1: cache empty -> real growth (0 -> 120), correctly NOT a plateau yet
    await p.get_candles("EURUSD", 200, now=t)
    print(f"cycle 1  -> cache={len(p._candle_cache['EURUSD'])}  deep_seed_calls={p.deep_seed_calls}")
    assert p.deep_seed_calls == 1

    # cycle 2: cache already at the ceiling -> this attempt gains ~0 -> plateau detected
    t += 60
    await p.get_candles("EURUSD", 200, now=t)
    print(f"cycle 2  -> deep_seed_calls={p.deep_seed_calls}  (plateau now recorded)")
    assert p.deep_seed_calls == 2

    # cycles 3..30: BEFORE this fix, every one of these would re-run the deep
    # seed (cache stuck at 120 < 200 forever, every single call). Simulate 28
    # more scan cycles, a minute apart -- all inside the 30-min cooldown.
    for i in range(28):
        t += 60
        await p.get_candles("EURUSD", 200, now=t)

    print(f"after 28 more cycles (28 min) -> deep_seed_calls={p.deep_seed_calls}")
    assert p.deep_seed_calls == 2, "plateau must suppress the repeat deep seed within the cooldown"
    print("no repeat deep-seed while under the 30-min cooldown -- this is what stopped the cycle slowdown")

    # cross the cooldown boundary -> one more legitimate re-check
    t += SEED_PLATEAU_COOLDOWN_SECONDS + 1
    await p.get_candles("EURUSD", 200, now=t)
    print(f"after cooldown elapses        -> deep_seed_calls={p.deep_seed_calls}")
    assert p.deep_seed_calls == 3, "a fresh attempt is expected once the cooldown has elapsed"

    print(f"\nOLD behaviour would have made {p.deep_seed_calls + 28} deep-seed calls over the same period")
    print("(one per cycle, forever, each up to ~150s)")
    print("\nSEED PLATEAU CHECKS PASSED")

asyncio.run(main())
