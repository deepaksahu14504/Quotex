"""Tests for the live chart candle streamer.

Everything here runs on injected callables, so no test touches the network. The
loop under test is the real one, including its rate limiting, dedupe and
cancellation.
"""

from __future__ import annotations

import asyncio
from typing import List, Tuple

import pytest

from app.schemas import Candle
from app.services.chart_stream import ChartStreamer

BASE = 1759201200  # 15m-aligned epoch second


def C(ts, cl=1.1440, o=1.1430, h=None, l=None) -> Candle:
    """A candle whose high/low are derived from open/close, so the fixture is
    always valid and only the behaviour under test can fail."""
    hi = h if h is not None else max(o, cl) + 0.0002
    lo = l if l is not None else min(o, cl) - 0.0002
    return Candle(timestamp=ts, open=o, high=hi, low=lo, close=cl,
                  volume=0.0, volume_source="synthetic")


class Harness:
    """A streamer wired to in-memory candles and a recorded broadcast sink."""

    def __init__(self, candles=None, event=None):
        self.candles: List[Candle] = list(candles) if candles else [C(BASE)]
        self.sent: List[Tuple[str, dict]] = []
        self.fetches: List[tuple] = []
        self.event = event if event is not None else asyncio.Event()

        self.streamer = ChartStreamer(
            "u1", self._get_candles, self._broadcast, self.event,
            min_interval=0.01, idle_timeout=0.05,
        )

    async def _get_candles(self, asset, timeframe, count):
        self.fetches.append((asset, timeframe, count))
        return list(self.candles)

    async def _broadcast(self, event_name, data):
        self.sent.append((event_name, data))

    async def tick(self, times=1, wait=0.12):
        for _ in range(times):
            self.event.set()
            await asyncio.sleep(wait)

    async def stop(self):
        await self.streamer.stop()


@pytest.fixture
def harness():
    return Harness()


# ── 1. one loop per pair, shared by every watcher ──────────────────────────

@pytest.mark.asyncio
async def test_multiple_watchers_share_one_loop(harness):
    """Ten tabs on one pair must not mean ten fetch loops."""
    for _ in range(3):
        await harness.streamer.subscribe("EURUSD_otc", "15m")

    assert harness.streamer.watcher_count("EURUSD_otc", "15m") == 3
    assert len(harness.streamer._tasks) == 1
    assert harness.streamer.subscriptions() == [("EURUSD_otc", "15m")]
    await harness.stop()


@pytest.mark.asyncio
async def test_distinct_pairs_get_distinct_loops(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")
    await harness.streamer.subscribe("GBPUSD_otc", "5m")

    assert len(harness.streamer._tasks) == 2
    assert harness.streamer.subscriptions() == [
        ("EURUSD_otc", "15m"), ("GBPUSD_otc", "5m")]
    await harness.stop()


# ── 2. a data event produces an update ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_data_event_emits_candle_update(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")

    await harness.tick()

    assert len(harness.sent) == 1
    name, data = harness.sent[0]
    assert name == "candle_update"
    assert data["asset"] == "EURUSD_otc"
    assert data["timeframe"] == "15m"
    assert set(data["candle"]) == {
        "timestamp", "open", "high", "low", "close", "volume"}
    await harness.stop()


@pytest.mark.asyncio
async def test_synthetic_volume_is_null_on_the_wire(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")

    await harness.tick()

    assert harness.sent[0][1]["candle"]["volume"] is None
    await harness.stop()


# ── 3. dedupe ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unchanged_candle_is_not_resent(harness):
    """A busy market ticks constantly; the chart needs one bar, not a flood."""
    await harness.streamer.subscribe("EURUSD_otc", "15m")
    await harness.tick()
    assert len(harness.sent) == 1

    harness.sent.clear()
    await harness.tick(times=4, wait=0.08)

    assert harness.sent == [], "re-sent a candle that had not changed"
    await harness.stop()


@pytest.mark.asyncio
async def test_a_price_change_is_sent(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")
    await harness.tick()

    harness.candles = [C(BASE, cl=1.1455)]
    harness.sent.clear()
    await harness.tick()

    assert len(harness.sent) == 1
    assert harness.sent[0][1]["candle"]["close"] == 1.1455
    await harness.stop()


# ── 4. rollover ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_new_bucket_is_streamed_without_duplicating_the_old_one(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")
    await harness.tick()

    harness.candles = [C(BASE, cl=1.1455), C(BASE + 900, cl=1.1460)]
    harness.sent.clear()
    await harness.tick()

    stamps = [d["candle"]["timestamp"] for _n, d in harness.sent]
    assert BASE + 900 in stamps, "the new candle never reached the client"
    assert len(stamps) == len(set(stamps)), "duplicate timestamp in one batch"


@pytest.mark.asyncio
async def test_nothing_is_resent_after_a_rollover(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")
    await harness.tick()
    harness.candles = [C(BASE, cl=1.1455), C(BASE + 900, cl=1.1460)]
    await harness.tick()

    harness.sent.clear()
    await harness.tick()

    assert harness.sent == []
    await harness.stop()


# ── 5. unsubscribe ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_watcher_leaving_keeps_the_loop_alive(harness):
    for _ in range(3):
        await harness.streamer.subscribe("EURUSD_otc", "15m")

    await harness.streamer.unsubscribe("EURUSD_otc", "15m")

    assert harness.streamer.watcher_count("EURUSD_otc", "15m") == 2
    assert len(harness.streamer._tasks) == 1
    await harness.stop()


@pytest.mark.asyncio
async def test_the_last_watcher_stops_the_loop(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")

    await harness.streamer.unsubscribe("EURUSD_otc", "15m")
    await asyncio.sleep(0.05)

    assert harness.streamer.watcher_count("EURUSD_otc", "15m") == 0
    assert len(harness.streamer._tasks) == 0


@pytest.mark.asyncio
async def test_unsubscribing_an_unknown_pair_is_safe(harness):
    await harness.streamer.unsubscribe("NOPE", "1m")  # must not raise
    assert harness.streamer.subscriptions() == []


# ── 6. resilience ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failed_fetch_does_not_kill_the_stream():
    calls = {"n": 0}
    sent: List[Tuple[str, dict]] = []
    event = asyncio.Event()

    async def flaky(asset, tf, count):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("broker unavailable")
        return [C(BASE, cl=1.1470)]

    async def broadcast(name, data):
        sent.append((name, data))

    s = ChartStreamer("u1", flaky, broadcast, event,
                      min_interval=0.01, idle_timeout=0.05)
    await s.subscribe("GBPUSD_otc", "5m")

    event.set(); await asyncio.sleep(0.10)   # the failing fetch
    event.set(); await asyncio.sleep(0.12)   # the recovering one

    assert len(sent) == 1, "the stream died on the first failure"
    assert len(s._tasks) == 1
    await s.stop()


@pytest.mark.asyncio
async def test_an_empty_fetch_emits_nothing(harness):
    harness.candles = []
    await harness.streamer.subscribe("EURUSD_otc", "15m")

    await harness.tick()

    assert harness.sent == []
    await harness.stop()


@pytest.mark.asyncio
async def test_invalid_candles_never_reach_the_client(harness):
    harness.candles = [C(BASE, h=1.1450, l=1.1420, cl=9.99)]  # close out of range
    await harness.streamer.subscribe("EURUSD_otc", "15m")

    await harness.tick()

    assert harness.sent == []
    await harness.stop()


# ── 7. shutdown ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_clears_everything_and_refuses_to_restart(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")
    await harness.streamer.subscribe("GBPUSD_otc", "5m")

    await harness.stop()
    await asyncio.sleep(0.05)

    assert len(harness.streamer._tasks) == 0
    assert await harness.streamer.subscribe("X", "1m") is False


@pytest.mark.asyncio
async def test_unsubscribe_all_stops_every_pair(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")
    await harness.streamer.subscribe("GBPUSD_otc", "5m")

    await harness.streamer.unsubscribe_all()
    await asyncio.sleep(0.05)

    assert harness.streamer.subscriptions() == []
    assert len(harness.streamer._tasks) == 0


# ── 8. no credential material on the wire ──────────────────────────────────

@pytest.mark.asyncio
async def test_no_secrets_in_a_broadcast_frame(harness):
    await harness.streamer.subscribe("EURUSD_otc", "15m")

    await harness.tick()

    import json
    blob = json.dumps(harness.sent).lower()
    for leak in ("ssid", "cookie", "password", "token", "email", "authorization"):
        assert leak not in blob, f"leaked {leak!r}"
    await harness.stop()


# ── 9. the streamer works without a data event (idle-timeout fallback) ─────

@pytest.mark.asyncio
async def test_it_still_streams_without_a_data_event():
    """A provider that never signals must not leave the chart frozen."""
    sent: List[Tuple[str, dict]] = []

    async def get_candles(asset, tf, count):
        return [C(BASE)]

    async def broadcast(name, data):
        sent.append((name, data))

    s = ChartStreamer("u1", get_candles, broadcast, None,
                      min_interval=0.01, idle_timeout=0.05)
    await s.subscribe("EURUSD_otc", "15m")
    await asyncio.sleep(0.2)

    assert len(sent) >= 1, "no event object meant no updates at all"
    await s.stop()
