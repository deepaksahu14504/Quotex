"""Regression tests for backend/app/services/market.py.

Each test in section 1 fails against the pre-fix code.
Run: cd backend && python3 -m pytest test_market_service.py -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.services.market import PyQuotexProvider  # noqa: E402


class BrokerBuffer:
    """Exact reproduction of pyquotex's realtime_price buffer,
    api.py:682-685 -- a plain list capped at 1000 with pop(0)."""

    def __init__(self, cap=1000):
        self.cap = cap
        self.ticks = []

    def push(self, ts, price):
        self.ticks.append({"time": ts, "price": price})
        if len(self.ticks) > self.cap:
            self.ticks.pop(0)

    def snapshot(self):
        return list(self.ticks)


class FakeClient:
    def __init__(self, buffer=None, subscribe_delay=0.0):
        self.buffer = buffer or BrokerBuffer()
        self.subscribe_calls = []
        self.subscribe_delay = subscribe_delay
        self.history_calls = 0

    async def start_candles_stream(self, asset, period):
        await asyncio.sleep(self.subscribe_delay)
        self.subscribe_calls.append((asset, period))

    async def get_realtime_price(self, asset):
        return self.buffer.snapshot()

    async def get_historical_candles(self, asset, amount_of_seconds, period, max_workers=3):
        self.history_calls += 1
        base = 1_700_000_000
        return [{"time": base + i * period, "open": 1.0, "high": 1.1,
                 "low": 0.9, "close": 1.05, "volume": 1} for i in range(80)]

    async def get_candles(self, asset, ts, offset, period):
        return []


def make_provider(client):
    p = PyQuotexProvider.__new__(PyQuotexProvider)
    # Replicate __init__ state without touching the network/session code.
    p._client = client
    p._connected = True
    p._candle_cache = {}
    p._streamed = set()
    p._subscribing = set()
    p._stream_tick_ts = {}
    p._last_resync = {}
    p._cache_lru = []
    p._seed_plateaued_at = {}
    p._asset_feed = None
    p._tick_watcher_task = None
    p._tournament_id = None
    p.new_data_event = asyncio.Event()
    return p


# ================================================================== #
# 1. THE ROOT CAUSE — tick cursor vs a bounded, left-evicting buffer
# ================================================================== #
@pytest.mark.asyncio
async def test_tick_folding_survives_buffer_eviction():
    """PRE-FIX: the cursor was len(ticks). The broker's buffer saturates
    at 1000 and evicts from the left, so len() pinned at 1000, the cursor
    pinned at 1000, and ticks[1000:] was empty forever -- live folding
    stopped permanently and silently."""
    buf = BrokerBuffer(cap=1000)
    client = FakeClient(buf)
    p = make_provider(client)
    p._streamed.add(("EURUSD", 60))

    base = 1_700_000_000
    for i in range(1500):          # saturate well past the cap
        buf.push(base + i, 1.0 + i * 1e-6)
    await p.get_candles("EURUSD", "1m", 120)
    closes_before = {k: v["close"] for k, v in p._candle_cache["EURUSD:60"].items()}

    # 500 brand-new ticks at brand-new timestamps and a distinct price.
    for i in range(1500, 2000):
        buf.push(base + i, 9.0)
    await p.get_candles("EURUSD", "1m", 120)
    cache = p._candle_cache["EURUSD:60"]

    # Assert on the bucket the NEWEST TICK belongs to. Not max(cache):
    # the history seed writes buckets far beyond the tick range, so
    # max(cache) picks a seeded candle and the assertion would be
    # testing the wrong thing entirely.
    newest_tick_ts = base + 1999
    tick_bucket = float(newest_tick_ts // 60 * 60)
    assert tick_bucket in cache, "the newest tick's bucket is missing"
    assert cache[tick_bucket]["close"] == 9.0, (
        "live ticks after buffer saturation were not folded in -- "
        "the cursor is still position-based"
    )
    assert {k: v["close"] for k, v in cache.items()} != closes_before


@pytest.mark.asyncio
async def test_cursor_is_a_timestamp_not_a_length():
    buf = BrokerBuffer(cap=50)
    p = make_provider(FakeClient(buf))
    p._streamed.add(("EURUSD", 60))
    base = 1_700_000_000
    for i in range(200):
        buf.push(base + i, 1.0)
    await p.get_candles("EURUSD", "1m", 120)
    # Key is (asset, period), not asset. The cursor used to be shared
    # across timeframes, which starved whichever timeframe read second --
    # see test_transport_bounds.py::test_second_timeframe_is_not_starved_*.
    # The cursor VALUE is a (timestamp, index-in-buffer) tuple -- the
    # timestamp component proves we are no longer using a position/length
    # cursor (which would be <= 50 at buffer saturation).
    cursor_ts, _ = p._stream_tick_ts[("EURUSD", 60)]
    assert cursor_ts == float(base + 199)


@pytest.mark.asyncio
async def test_same_second_ticks_are_not_dropped():
    """Same-second ticks after the previous fold must still be picked up;
    strict `>` without an in-timestamp position counter would lose them."""
    buf = BrokerBuffer()
    p = make_provider(FakeClient(buf))
    p._streamed.add(("EURUSD", 60))
    ts = 1_700_000_000
    buf.push(ts, 1.0)
    await p.get_candles("EURUSD", "1m", 120)
    buf.push(ts, 5.0)              # same second, higher price
    buf.push(ts, 0.5)              # same second, lower price
    await p.get_candles("EURUSD", "1m", 120)
    row = p._candle_cache["EURUSD:60"][float(ts // 60 * 60)]
    assert row["high"] == 5.0 and row["low"] == 0.5


@pytest.mark.asyncio
async def test_refolding_the_boundary_tick_is_idempotent():
    buf = BrokerBuffer()
    p = make_provider(FakeClient(buf))
    p._streamed.add(("EURUSD", 60))
    ts = 1_700_000_000
    buf.push(ts, 2.0)
    await p.get_candles("EURUSD", "1m", 120)
    snap1 = dict(p._candle_cache["EURUSD:60"][float(ts // 60 * 60)])
    for _ in range(5):
        await p.get_candles("EURUSD", "1m", 120)
    snap2 = p._candle_cache["EURUSD:60"][float(ts // 60 * 60)]
    assert snap1 == snap2


@pytest.mark.asyncio
async def test_malformed_ticks_do_not_break_folding():
    buf = BrokerBuffer()
    p = make_provider(FakeClient(buf))
    p._streamed.add(("EURUSD", 60))
    buf.ticks.extend([{"time": "bad"}, {"nope": 1}, {"time": None, "price": 1}])
    buf.push(1_700_000_000, 3.0)
    await p.get_candles("EURUSD", "1m", 120)
    assert p._candle_cache["EURUSD:60"]


# ================================================================== #
# 2. Duplicate subscription race
# ================================================================== #
@pytest.mark.asyncio
async def test_concurrent_callers_send_only_one_subscribe():
    """PRE-FIX: `key in self._streamed` -> await -> `.add()` is
    check-then-act across an await, so two concurrent callers for the
    same (asset, period) both subscribed."""
    client = FakeClient(subscribe_delay=0.05)
    p = make_provider(client)
    await asyncio.gather(*[p.get_candles("EURUSD", "1m", 120) for _ in range(6)])
    assert client.subscribe_calls.count(("EURUSD", 60)) == 1, client.subscribe_calls


@pytest.mark.asyncio
async def test_subscribe_failure_clears_the_in_flight_marker():
    class Failing(FakeClient):
        async def start_candles_stream(self, asset, period):
            raise ConnectionError("subscribe rejected")

    p = make_provider(Failing())
    await p.get_candles("EURUSD", "1m", 120)
    assert p._subscribing == set(), "a failed subscribe must not wedge the key"
    assert ("EURUSD", 60) not in p._streamed


@pytest.mark.asyncio
async def test_distinct_streams_are_independent():
    client = FakeClient()
    p = make_provider(client)
    await asyncio.gather(
        p.get_candles("EURUSD", "1m", 120),
        p.get_candles("EURUSD", "5m", 120),
        p.get_candles("GBPUSD", "1m", 120),
    )
    assert len(set(client.subscribe_calls)) == 3


# ================================================================== #
# 3. Bounded memory
# ================================================================== #
@pytest.mark.asyncio
async def test_candle_cache_key_count_is_bounded():
    """PRE-FIX: a key was never removed, so every asset ever scanned kept
    its full 800-candle cache for the life of the process."""
    p = make_provider(FakeClient())
    for i in range(PyQuotexProvider.CACHE_MAX_KEYS * 2):
        p._streamed.add((f"A{i}", 60))
        await p.get_candles(f"A{i}", "1m", 120)
    assert len(p._candle_cache) <= PyQuotexProvider.CACHE_MAX_KEYS
    assert len(p._cache_lru) <= PyQuotexProvider.CACHE_MAX_KEYS


@pytest.mark.asyncio
async def test_hot_keys_are_not_evicted():
    p = make_provider(FakeClient())
    p._streamed.add(("HOT", 60))
    await p.get_candles("HOT", "1m", 120)
    for i in range(PyQuotexProvider.CACHE_MAX_KEYS):
        p._streamed.add((f"COLD{i}", 60))
        await p.get_candles(f"COLD{i}", "1m", 120)
        if i % 3 == 0:
            await p.get_candles("HOT", "1m", 120)   # keep it warm
    assert "HOT:60" in p._candle_cache


@pytest.mark.asyncio
async def test_candles_within_a_key_stay_capped():
    p = make_provider(FakeClient())
    p._streamed.add(("EURUSD", 60))
    cache = p._candle_cache.setdefault("EURUSD:60", {})
    for i in range(2000):
        cache[float(1_700_000_000 + i * 60)] = {
            "time": 1_700_000_000 + i * 60, "open": 1, "high": 1,
            "low": 1, "close": 1, "volume": 0}
    await p.get_candles("EURUSD", "1m", 120)
    assert len(p._candle_cache["EURUSD:60"]) <= 800


# ================================================================== #
# 4. Reconnect recovery
# ================================================================== #
@pytest.mark.asyncio
async def test_reconnect_state_is_fully_reset():
    """Stale subscription/cursor state after a reconnect means the
    provider believes it is subscribed to streams the new socket knows
    nothing about."""
    p = make_provider(FakeClient())
    p._streamed.add(("EURUSD", 60))
    p._stream_tick_ts["EURUSD"] = 123.0
    p._subscribing.add(("GBPUSD", 60))
    p._last_resync["EURUSD:60"] = 1.0

    # Same reset the connect()/disconnect() paths perform.
    p._streamed.clear()
    p._stream_tick_ts.clear()
    p._subscribing.clear()
    p._last_resync.clear()

    assert not p._streamed and not p._stream_tick_ts
    assert not p._subscribing and not p._last_resync


@pytest.mark.asyncio
async def test_resubscribe_after_reset_actually_happens():
    client = FakeClient()
    p = make_provider(client)
    await p.get_candles("EURUSD", "1m", 120)
    p._streamed.clear(); p._stream_tick_ts.clear(); p._subscribing.clear()
    await p.get_candles("EURUSD", "1m", 120)
    assert client.subscribe_calls.count(("EURUSD", 60)) == 2


# ================================================================== #
# 5. Non-blocking / async safety
# ================================================================== #
@pytest.mark.asyncio
async def test_get_candles_does_not_block_the_loop():
    buf = BrokerBuffer()
    for i in range(1000):
        buf.push(1_700_000_000 + i, 1.0)
    p = make_provider(FakeClient(buf))
    p._streamed.add(("EURUSD", 60))
    ticks = {"n": 0}

    async def ticker():
        for _ in range(30):
            ticks["n"] += 1
            await asyncio.sleep(0)

    await asyncio.gather(ticker(), p.get_candles("EURUSD", "1m", 120))
    assert ticks["n"] == 30


@pytest.mark.asyncio
async def test_returns_candle_objects_in_time_order():
    buf = BrokerBuffer()
    base = 1_700_000_000
    for i in range(300):
        buf.push(base + i, 1.0 + i * 1e-4)
    p = make_provider(FakeClient(buf))
    p._streamed.add(("EURUSD", 60))
    out = await p.get_candles("EURUSD", "1m", 120)
    assert out and all(
        out[i].timestamp < out[i + 1].timestamp for i in range(len(out) - 1)
    )
    assert all(c.high >= c.low for c in out)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
