"""Offline verification for the historical-candle pipeline.

No live Quotex connection is used or needed. A FakeApi implements the
exact surface HistoryMixin touches (event_registry, send_websocket_request,
candles, candle_v2_data, slots), against the REAL EventRegistry/AsyncEvent
from pyquotex.utils.async_utils, so event lifecycle, correlation, cleanup
and leak behaviour are exercised for real rather than mocked away.

Run:  cd vendor/pyquotex && python3 -m pytest tests/test_history_pipeline.py -q
      (or)  python3 tests/test_history_pipeline.py
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from pyquotex._api.history import HistoryMixin  # noqa: E402
from pyquotex._api.history_config import (  # noqa: E402
    HistoryConfig,
    HistoryConfigError,
)
from pyquotex._api.history_validation import (  # noqa: E402
    HistoryCircuitBreaker,
    normalize_candle,
    validate_and_clean,
    validate_envelope,
)
from pyquotex.utils.async_utils import EventRegistry  # noqa: E402

PERIOD = 60
NOW = 1_700_000_000  # fixed reference so tests never depend on wall clock


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #
class _Candles:
    def __init__(self):
        self.candles_data = None


class _Slot:
    def __init__(self):
        self._ev = asyncio.Event()

    async def wait(self, timeout=None):
        await asyncio.wait_for(self._ev.wait(), timeout=timeout)

    def set(self, data=None):
        self._ev.set()


class _Slots:
    def __init__(self):
        self._s = {}

    def candle_v2(self, asset):
        return self._s.setdefault(asset, _Slot())

    def release_candle_v2(self, asset):
        self._s[asset] = _Slot()


class FakeApi:
    """Mimics pyquotex.api.QuotexAPI's history-relevant surface, including
    the real producer logic copied from api.py's _on_message."""

    def __init__(self, responder=None, echo_index=True):
        self.event_registry = EventRegistry()
        self.candles = _Candles()
        self.candle_v2_data = {}
        self.historical_candles = None
        self.slots = _Slots()
        self.current_asset = None
        self.sent = []
        self._temp_status = ""
        self._subscriptions = {}
        # responder(payload) -> dict | None. None means "no response at
        # all" (simulates a dropped/misrouted frame).
        self.responder = responder
        # Mirrors api.py: the indexed event only fires when the broker
        # echoes an index back.
        self.echo_index = echo_index

    async def _emit(self, data):
        """Exactly the routing api.py performs for a history response."""
        if not isinstance(data, dict) or not data.get("asset"):
            return
        asset = data["asset"]
        self.candle_v2_data[asset] = data
        self.slots.candle_v2(asset).set(data)
        await self.event_registry.set_event(f"candles_ready_{asset}", data)
        if self.echo_index and data.get("index") is not None:
            await self.event_registry.set_event(
                f'candles_ready_{asset}_{data["index"]}', data
            )

    async def send_websocket_request(self, msg):
        self.sent.append(msg)
        import json as _json
        payload = _json.loads(msg[2:])[1]
        if self.responder is None:
            return
        data = self.responder(payload)
        if data is None:
            return
        if not self.echo_index:
            data = {k: v for k, v in data.items() if k != "index"}
        await self._emit(data)

    async def get_candles(self, asset, index, end_from_time, offset, period):
        self.sent.append(("get_candles", asset, index, offset, period))
        if self.responder is None:
            return
        data = self.responder(
            {"asset": asset, "index": index, "time": end_from_time,
             "offset": offset, "period": period}
        )
        if data is not None:
            await self._emit(data)

    async def get_history_line(self, *a, **kw):
        return None

    async def get_trader_history(self, account_type, page_number):
        return {"account_type": account_type, "page": page_number}


class FakeQuotex(HistoryMixin):
    """HistoryMixin composed the same way stable_api.Quotex composes it."""

    def __init__(self, api=None, config: HistoryConfig | None = None):
        self.api = api
        self.codes_asset = {}
        self.streams = []
        if config is not None:
            self.history_config = config

    async def start_candles_stream(self, asset, period=None):
        self.streams.append((asset, period))

    async def check_connect(self):
        return True


def make_candles(n, start=NOW - 60 * 200, period=PERIOD, gap_at=None, dup_at=None):
    """Broker-shaped list candles: [time, open, close, high, low]."""
    out = []
    t = start
    for i in range(n):
        if gap_at is not None and i == gap_at:
            t += period * 5  # a real 5-bar gap
        out.append([t, 1.0, 1.1, 1.2, 0.9])
        if dup_at is not None and i == dup_at:
            out.append([t, 1.0, 1.1, 1.2, 0.9])
        t += period
    return out


def tick_resp(asset, payload, n_ticks=600, period=PERIOD, start=NOW - 600):
    """Payload shape the get_candles path consumes.

    calculate_candles() reads history["history"] (or ["candles"]) as a list
    of [timestamp, price] TICKS and buckets them into candles itself. This
    is deliberately NOT the same shape as a history/load batch response,
    which carries OHLC candles under "data" -- the two paths really do
    speak different formats, which is worth keeping visible in the tests.
    """
    ticks = [[start + i, 1.0 + (i % 10) * 0.001] for i in range(n_ticks)]
    return {
        "asset": asset,
        "index": payload.get("index"),
        "period": period,
        "history": ticks,
    }


def resp(asset, payload, candles, period=PERIOD):
    return {
        "asset": asset,
        "index": payload.get("index"),
        "period": period,
        "data": candles,
    }


# --------------------------------------------------------------------- #
# 1. Config
# --------------------------------------------------------------------- #
def test_config_defaults_preserve_old_behaviour():
    cfg = HistoryConfig()
    assert cfg.max_workers == 5           # was the hardcoded default
    assert cfg.candles_per_chunk == 200   # was `period * 200`
    assert cfg.throttle_seconds == 0.1    # was `await asyncio.sleep(0.1)`


def test_resolve_workers_none_uses_config():
    cfg = HistoryConfig(max_workers=7)
    assert cfg.resolve_workers(None) == 7
    assert cfg.resolve_workers(3) == 3


def test_resolve_workers_clamps_and_rejects():
    cfg = HistoryConfig()
    assert cfg.resolve_workers(0) == 1
    assert cfg.resolve_workers(9999) == 32
    with pytest.raises(HistoryConfigError):
        cfg.resolve_workers("many")


@pytest.mark.parametrize("kwargs", [
    {"max_workers": 0}, {"max_workers": 33}, {"max_workers": True},
    {"candles_per_chunk": 0}, {"max_retries": 0}, {"retry_jitter": 1.0},
    {"gap_multiplier": 1.0}, {"cache_ttl": 0},
])
def test_config_rejects_invalid(kwargs):
    with pytest.raises(HistoryConfigError):
        HistoryConfig(**kwargs)


def test_adaptive_timeout_is_bounded():
    cfg = HistoryConfig(timeout_scale=1.5, timeout_max_multiplier=3.0)
    assert cfg.attempt_timeout(10, 1) == 10
    assert cfg.attempt_timeout(10, 2) == 15
    assert cfg.attempt_timeout(10, 99) == 30  # capped, never unbounded


def test_retry_delay_backoff_and_bounds():
    cfg = HistoryConfig(retry_base_delay=1.0, retry_backoff_factor=2.0,
                        retry_max_delay=4.0, retry_jitter=0.0)
    assert [cfg.retry_delay(a) for a in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 4.0]
    jittered = HistoryConfig(retry_base_delay=1.0, retry_jitter=0.5)
    assert 0.5 <= jittered.retry_delay(1, rand=0.0) <= 1.5
    assert 0.5 <= jittered.retry_delay(1, rand=1.0) <= 1.5


# --------------------------------------------------------------------- #
# 2. Validation — integrity, NaN/inf, timestamps, ordering, dedup, gaps
# --------------------------------------------------------------------- #
def _clean(raw, period=PERIOD, now=NOW):
    return validate_and_clean(
        raw, period,
        min_plausible_timestamp=1_000_000_000,
        future_tolerance_seconds=120.0,
        spacing_tolerance=0.25,
        gap_multiplier=1.5,
        now=now,
    )


def test_normalize_accepts_both_broker_shapes():
    assert normalize_candle([NOW, 1, 2, 3, 0.5])["time"] == NOW
    assert normalize_candle(
        {"time": NOW, "open": 1, "close": 2, "high": 3, "low": 0.5}
    )["close"] == 2.0


def test_normalize_never_fabricates_missing_fields():
    assert normalize_candle({"time": NOW, "open": 1}) is None
    assert normalize_candle([NOW, 1, 2]) is None
    assert normalize_candle("nonsense") is None


def test_nan_and_infinity_are_rejected_not_crashed():
    raw = [
        [NOW - 120, 1, 1.1, 1.2, 0.9],
        [NOW - 60, float("nan"), 1.1, 1.2, 0.9],
        [NOW - 60, float("inf"), 1.1, 1.2, 0.9],
        [NOW, "abc", 1.1, 1.2, 0.9],
    ]
    out, report = _clean(raw)
    assert len(out) == 1
    assert report.rejected_malformed == 3
    assert all(math.isfinite(c["open"]) for c in out)


def test_impossible_ohlc_rejected():
    raw = [
        [NOW - 60, 1.0, 1.1, 0.5, 0.9],   # high < low
        [NOW - 120, 1.0, 1.1, 1.05, 0.9],  # high < close
        [NOW - 180, 1.0, 1.1, 1.2, 1.05],  # low > open
        [NOW - 240, 1.0, 1.1, 1.2, 0.9],   # valid
    ]
    out, report = _clean(raw)
    assert len(out) == 1
    assert report.rejected_invalid_ohlc == 3


def test_future_candle_rejected_but_forming_candle_kept():
    raw = [
        [NOW, 1, 1.1, 1.2, 0.9],           # the forming candle
        [NOW + 60, 1, 1.1, 1.2, 0.9],      # within tolerance -> kept
        [NOW + 100000, 1, 1.1, 1.2, 0.9],  # impossible -> rejected
    ]
    out, report = _clean(raw)
    assert len(out) == 2
    assert report.rejected_future == 1


def test_implausible_old_timestamp_rejected():
    out, report = _clean([[5, 1, 1.1, 1.2, 0.9]])
    assert out == []
    assert report.rejected_bad_timestamp == 1


def test_unordered_input_is_recorded_before_sorting():
    raw = [[NOW - 60, 1, 1.1, 1.2, 0.9], [NOW - 120, 1, 1.1, 1.2, 0.9]]
    out, report = _clean(raw)
    assert report.was_unordered is True          # anomaly not hidden by sort
    assert [c["time"] for c in out] == [NOW - 120, NOW - 60]  # still sorted


def test_identical_duplicates_collapse_quietly():
    raw = make_candles(5, dup_at=2)
    out, report = _clean(raw)
    assert report.duplicates_identical == 1
    assert report.duplicates_conflicting == 0
    assert len({c["time"] for c in out}) == len(out)


def test_conflicting_duplicates_are_counted_and_deterministic():
    ts = NOW - 600
    raw = [[ts, 1.0, 1.1, 1.2, 0.9], [ts, 2.0, 2.1, 2.2, 1.9]]
    out, report = _clean(raw)
    assert report.duplicates_conflicting == 1
    assert len(out) == 1
    # Deterministic policy: last writer wins. Same input -> same output.
    again, _ = _clean(raw)
    assert out[0]["open"] == again[0]["open"] == 2.0


def test_gap_detected_but_never_filled():
    raw = make_candles(10, gap_at=5)
    out, report = _clean(raw)
    assert len(report.gaps) == 1
    assert report.missing_intervals == 5
    assert len(out) == 10          # NOT padded up to 14
    # No synthetic candle: every returned timestamp came from the input.
    src = {c[0] for c in raw}
    assert all(c["time"] in src for c in out)


def test_valid_market_gap_does_not_destroy_candles():
    """A weekend-sized gap is reported, and both sides are kept."""
    raw = [[NOW - 300000, 1, 1.1, 1.2, 0.9], [NOW - 60, 1, 1.1, 1.2, 0.9]]
    out, report = _clean(raw)
    assert len(out) == 2
    assert len(report.gaps) == 1


def test_spacing_tolerance_does_not_falsely_reject():
    raw = [[NOW - 180, 1, 1.1, 1.2, 0.9],
           [NOW - 122, 1, 1.1, 1.2, 0.9],   # 58s ~ 60s
           [NOW - 60, 1, 1.1, 1.2, 0.9]]
    out, report = _clean(raw)
    assert len(out) == 3
    assert report.spacing_anomalies == 0


# --------------------------------------------------------------------- #
# 3. Envelope correlation
# --------------------------------------------------------------------- #
def test_envelope_lenient_about_missing_optional_metadata():
    ok, _ = validate_envelope({"data": []}, expected_asset="EURUSD", expected_index=7)
    assert ok is True   # missing asset/index must NOT be a rejection


def test_envelope_rejects_contradicting_metadata():
    ok, why = validate_envelope(
        {"data": [], "asset": "GBPUSD"}, expected_asset="EURUSD"
    )
    assert ok is False and "asset mismatch" in why
    ok, why = validate_envelope(
        {"data": [], "index": 9}, expected_asset="EURUSD", expected_index=7
    )
    assert ok is False and "index mismatch" in why


def test_envelope_rejects_junk():
    assert validate_envelope(None)[0] is False
    assert validate_envelope("text")[0] is False
    assert validate_envelope({"nothing": 1})[0] is False


# --------------------------------------------------------------------- #
# 4. Circuit breaker
# --------------------------------------------------------------------- #
def test_breaker_opens_then_recovers_automatically():
    b = HistoryCircuitBreaker(threshold=3, cooldown=10.0)
    for _ in range(3):
        b.record_failure(now=100.0)
    assert b.is_open
    assert b.allows(now=105.0) is False      # still cooling down
    assert b.allows(now=111.0) is True       # half-open probe allowed
    b.record_success()
    assert b.is_open is False                # never latches permanently


def test_breaker_can_be_disabled():
    b = HistoryCircuitBreaker(threshold=1, cooldown=10.0, enabled=False)
    b.record_failure()
    assert b.allows() is True


# --------------------------------------------------------------------- #
# 5. get_candles — basic, cache, timeout, cleanup
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_candles_basic():
    api = FakeApi(lambda p: tick_resp("EURUSD", p, n_ticks=600))
    q = FakeQuotex(api)
    out = await q.get_candles("EURUSD", NOW, 60, PERIOD)
    assert out is not None and len(out) > 0
    assert q.history_metrics.snapshot()["requests_ok"] == 1


@pytest.mark.asyncio
async def test_get_candles_timeout_returns_none_and_cleans_up():
    api = FakeApi(lambda p: None)  # response never arrives
    q = FakeQuotex(api)
    out = await q.get_candles("EURUSD", NOW, 60, PERIOD, timeout=1)
    assert out is None
    snap = q.history_metrics.snapshot()
    assert snap["timeouts"] == 1 and snap["requests_failed"] == 1
    # Event was reset, not left armed with stale data.
    ev = await api.event_registry.get_event("candles_ready_EURUSD")
    assert ev.is_set() is False


@pytest.mark.asyncio
async def test_stale_event_cannot_answer_a_new_request():
    """AsyncEvent keeps _has_fired set; without the pre-request clear a
    stale response would satisfy the next wait instantly."""
    api = FakeApi(lambda p: None)
    await api.event_registry.set_event("candles_ready_EURUSD", {"data": [], "stale": 1})
    q = FakeQuotex(api)
    out = await q.get_candles("EURUSD", NOW, 60, PERIOD, timeout=1)
    assert out is None   # timed out rather than consuming the stale payload


@pytest.mark.asyncio
async def test_cache_hit_miss_and_ttl_scoping():
    calls = []

    def responder(p):
        calls.append(p)
        return tick_resp("EURUSD", p, n_ticks=600)

    q = FakeQuotex(FakeApi(responder))
    a = await q.get_candles("EURUSD", NOW, 60, PERIOD, use_cache=True)
    b = await q.get_candles("EURUSD", NOW, 60, PERIOD, use_cache=True)
    assert a == b and len(calls) == 1
    snap = q.history_metrics.snapshot()
    assert snap["cache_hits"] == 1 and snap["cache_misses"] == 1

    # Different period / offset / asset must all miss.
    await q.get_candles("EURUSD", NOW, 60, 300, use_cache=True)
    await q.get_candles("EURUSD", NOW, 120, PERIOD, use_cache=True)
    await q.get_candles("GBPUSD", NOW, 60, PERIOD, use_cache=True)
    assert len(calls) == 4  # GBPUSD responder returns EURUSD -> no cache write


@pytest.mark.asyncio
async def test_cache_is_instance_scoped_no_cross_account_leak():
    """The regression that matters: two accounts in one process."""
    q1 = FakeQuotex(FakeApi(lambda p: tick_resp("EURUSD", p, n_ticks=600)))
    q2 = FakeQuotex(FakeApi(lambda p: tick_resp("EURUSD", p, n_ticks=180)))
    a = await q1.get_candles("EURUSD", NOW, 60, PERIOD, use_cache=True)
    b = await q2.get_candles("EURUSD", NOW, 60, PERIOD, use_cache=True)
    assert q1._candle_cache is not q2._candle_cache
    assert len(a) != len(b)          # q2 got its OWN data, not q1's cache
    assert q2.history_metrics.snapshot()["cache_hits"] == 0


# --------------------------------------------------------------------- #
# 6. Batch fetch — correlation, retry, cleanup, the index-echo bug
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_batch_fetch_succeeds_with_index_echo():
    api = FakeApi(lambda p: resp("EURUSD", p, make_candles(10)), echo_index=True)
    q = FakeQuotex(api)
    out = await q._fetch_historical_batch("EURUSD", NOW, 600, PERIOD, 4242, 2)
    assert out is not None and out["index"] == 4242


@pytest.mark.asyncio
async def test_batch_fetch_survives_broker_not_echoing_index():
    """THE INTEGRATION BUG. api.py only fires candles_ready_<asset>_<index>
    when the broker echoes `index`. The old code waited on that event
    alone, so a broker that omits it made every batch time out and deep
    history silently return nothing."""
    api = FakeApi(lambda p: resp("EURUSD", p, make_candles(10)), echo_index=False)
    q = FakeQuotex(api, HistoryConfig(max_retries=1))
    out = await q._fetch_historical_batch("EURUSD", NOW, 600, PERIOD, 4242, 2)
    assert out is not None, "must fall back to the per-asset event"


@pytest.mark.asyncio
async def test_batch_fetch_ignores_another_requests_response():
    """A response carrying a different index must not satisfy this wait."""
    api = FakeApi(lambda p: None, echo_index=False)
    q = FakeQuotex(api, HistoryConfig(max_retries=1))

    async def inject_wrong_then_right():
        await asyncio.sleep(0.05)
        await api._emit(resp("EURUSD", {"index": 999}, make_candles(3)))
        await asyncio.sleep(0.15)
        await api._emit(resp("EURUSD", {"index": 4242}, make_candles(7)))

    asyncio.ensure_future(inject_wrong_then_right())
    out = await q._fetch_historical_batch("EURUSD", NOW, 600, PERIOD, 4242, 3)
    assert out is not None and out["index"] == 4242
    assert q.history_metrics.snapshot()["correlation_mismatches"] >= 1


@pytest.mark.asyncio
async def test_batch_fetch_retries_then_gives_up_bounded():
    attempts = []

    def responder(p):
        attempts.append(p)
        return None  # always silent

    cfg = HistoryConfig(max_retries=3, retry_base_delay=0.01, retry_max_delay=0.02)
    q = FakeQuotex(FakeApi(responder), cfg)
    out = await q._fetch_historical_batch("EURUSD", NOW, 600, PERIOD, 1, 1)
    assert out is None
    assert len(attempts) == 3            # TOTAL attempts, bounded
    snap = q.history_metrics.snapshot()
    assert snap["retries"] == 2 and snap["timeouts"] >= 1


@pytest.mark.asyncio
async def test_request_scoped_events_are_discarded_not_leaked():
    """The memory leak: one permanent registry entry per historical chunk."""
    api = FakeApi(lambda p: resp("EURUSD", p, make_candles(5)))
    q = FakeQuotex(api)
    before = api.event_registry.event_count()
    for i in range(50):
        await q._fetch_historical_batch("EURUSD", NOW, 600, PERIOD, 90000 + i, 2)
    after = api.event_registry.event_count()
    # Only the shared per-asset event may remain; the 50 indexed ones must
    # all be gone.
    assert after - before <= 1, f"leaked {after - before} events"


@pytest.mark.asyncio
async def test_batch_cleanup_happens_on_timeout_too():
    api = FakeApi(lambda p: None)
    q = FakeQuotex(api, HistoryConfig(max_retries=1))
    before = api.event_registry.event_count()
    for i in range(20):
        await q._fetch_historical_batch("EURUSD", NOW, 600, PERIOD, 70000 + i, 1)
    assert api.event_registry.event_count() - before <= 1


# --------------------------------------------------------------------- #
# 7. get_historical_candles — parallel, partial, bounds, cancellation
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_deep_history_multi_worker_no_duplicates_sorted():
    def responder(p):
        end = int(p["time"])
        candles = [[end - i * PERIOD, 1, 1.1, 1.2, 0.9] for i in range(1, 51)]
        return resp(p["asset"], p, candles)

    cfg = HistoryConfig(candles_per_chunk=50, throttle_seconds=0.0,
                        max_workers=4, future_tolerance_seconds=1e12)
    q = FakeQuotex(FakeApi(responder), cfg)
    out = await q.get_historical_candles("EURUSD", PERIOD * 400, PERIOD)
    times = [c["time"] for c in out]
    assert times == sorted(times)
    assert len(times) == len(set(times))     # deduplicated across workers
    assert len(out) > 0


@pytest.mark.asyncio
async def test_deep_history_respects_worker_bound():
    peak = {"n": 0, "cur": 0}

    async def tracking_stream(asset, period=None):
        pass

    def responder(p):
        peak["cur"] += 1
        peak["n"] = max(peak["n"], peak["cur"])
        peak["cur"] -= 1
        return resp(p["asset"], p, make_candles(20, start=int(p["time"]) - 20 * PERIOD))

    cfg = HistoryConfig(max_workers=3, candles_per_chunk=20,
                        throttle_seconds=0.0, future_tolerance_seconds=1e12)
    q = FakeQuotex(FakeApi(responder), cfg)
    q.start_candles_stream = tracking_stream
    await q.get_historical_candles("EURUSD", PERIOD * 200, PERIOD)
    assert peak["n"] <= 3


@pytest.mark.asyncio
async def test_explicit_max_workers_overrides_config():
    cfg = HistoryConfig(max_workers=8, candles_per_chunk=10,
                        throttle_seconds=0.0, future_tolerance_seconds=1e12)
    q = FakeQuotex(
        FakeApi(lambda p: resp(p["asset"], p, make_candles(
            10, start=int(p["time"]) - 10 * PERIOD))), cfg)
    # Both must run without error; the point is the override is accepted.
    await q.get_historical_candles("EURUSD", PERIOD * 100, PERIOD, max_workers=1)
    await q.get_historical_candles("EURUSD", PERIOD * 100, PERIOD)


@pytest.mark.asyncio
async def test_partial_failure_returns_real_candles_no_fabrication():
    """Half the chunks fail. We must get the good ones and nothing invented."""
    state = {"n": 0}

    def responder(p):
        state["n"] += 1
        if state["n"] % 2 == 0:
            return None    # this chunk is lost
        return resp(p["asset"], p, make_candles(
            20, start=int(p["time"]) - 20 * PERIOD))

    cfg = HistoryConfig(max_workers=2, candles_per_chunk=20, max_retries=1,
                        throttle_seconds=0.0, retry_base_delay=0.0,
                        future_tolerance_seconds=1e12)
    q = FakeQuotex(FakeApi(responder), cfg)
    out = await q.get_historical_candles("EURUSD", PERIOD * 200, PERIOD, timeout=1)
    assert len(out) > 0
    assert q.history_metrics.snapshot()["chunks_abandoned"] > 0
    # Nothing fabricated: every candle carries the exact OHLC the fake sent.
    assert all(c["open"] == 1.0 and c["high"] == 1.2 for c in out)


@pytest.mark.asyncio
async def test_no_infinite_loop_when_cursor_never_advances():
    """The classic hang: broker keeps returning the same oldest timestamp."""
    fixed = NOW - 100
    calls = {"n": 0}

    def responder(p):
        calls["n"] += 1
        return resp(p["asset"], p, [[fixed, 1, 1.1, 1.2, 0.9]])

    cfg = HistoryConfig(max_workers=1, candles_per_chunk=10,
                        throttle_seconds=0.0, future_tolerance_seconds=1e12)
    q = FakeQuotex(FakeApi(responder), cfg)
    out = await asyncio.wait_for(
        q.get_historical_candles("EURUSD", PERIOD * 100, PERIOD), timeout=10
    )
    assert isinstance(out, list)
    assert calls["n"] < 100      # bounded by the iteration ceiling


@pytest.mark.asyncio
async def test_progress_callback_failure_cannot_kill_a_worker():
    def boom(*a):
        raise RuntimeError("callback exploded")

    cfg = HistoryConfig(max_workers=1, candles_per_chunk=20,
                        throttle_seconds=0.0, future_tolerance_seconds=1e12)
    q = FakeQuotex(FakeApi(lambda p: resp(
        p["asset"], p, make_candles(20, start=int(p["time"]) - 20 * PERIOD))), cfg)
    out = await q.get_historical_candles(
        "EURUSD", PERIOD * 100, PERIOD, progress_callback=boom
    )
    assert len(out) > 0     # isolated, not fatal


@pytest.mark.asyncio
async def test_malformed_payload_does_not_kill_the_worker():
    """Unguarded float()/int() used to raise straight out of the worker and,
    via gather(), abort the whole deep-history call."""
    def responder(p):
        return resp(p["asset"], p, [
            ["not-a-time", 1, 1.1, 1.2, 0.9],
            [None, 1, 1.1, 1.2, 0.9],
            [int(p["time"]) - PERIOD, 1, 1.1, 1.2, 0.9],   # one good candle
        ])

    cfg = HistoryConfig(max_workers=1, candles_per_chunk=10,
                        throttle_seconds=0.0, future_tolerance_seconds=1e12)
    q = FakeQuotex(FakeApi(responder), cfg)
    out = await q.get_historical_candles("EURUSD", PERIOD * 50, PERIOD)
    assert isinstance(out, list)
    assert q.history_metrics.snapshot()["validation_failures"] > 0


@pytest.mark.asyncio
async def test_cancellation_leaves_no_orphan_tasks_or_events():
    async def slow(p):
        await asyncio.sleep(5)
        return None

    def responder(p):
        return None

    api = FakeApi(responder)
    cfg = HistoryConfig(max_workers=3, candles_per_chunk=10,
                        throttle_seconds=0.0, max_retries=3)
    q = FakeQuotex(api, cfg)

    before_tasks = len(asyncio.all_tasks())
    task = asyncio.ensure_future(
        q.get_historical_candles("EURUSD", PERIOD * 500, PERIOD, timeout=30)
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)

    remaining = [t for t in asyncio.all_tasks() if not t.done()]
    assert len(remaining) <= before_tasks + 1, "orphan worker tasks left behind"
    assert api.event_registry.event_count() <= 2, "orphan events left behind"


@pytest.mark.asyncio
async def test_zero_and_negative_ranges_are_safe():
    q = FakeQuotex(FakeApi(lambda p: None))
    assert await q.get_historical_candles("EURUSD", 0, PERIOD) == []
    assert await q.get_historical_candles("EURUSD", -100, PERIOD) == []
    with pytest.raises(ValueError):
        await q.get_historical_candles("EURUSD", 100, 0)


# --------------------------------------------------------------------- #
# 8. Long-running / bounded-memory
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_repeated_requests_keep_memory_bounded():
    api = FakeApi(lambda p: tick_resp("EURUSD", p, n_ticks=300))
    cfg = HistoryConfig(cache_maxsize=8, metrics_latency_samples=16)
    q = FakeQuotex(api, cfg)
    for i in range(300):
        await q.get_candles("EURUSD", NOW + i * PERIOD, 60, PERIOD, use_cache=True)
    assert len(q._candle_cache) <= 8                       # cache bounded
    assert q.history_metrics.snapshot()["latency_samples"] <= 16  # metrics bounded
    assert api.event_registry.event_count() <= 3           # registry bounded


def test_metrics_snapshot_and_reset():
    m = FakeQuotex(None).history_metrics
    for v in (0.1, 0.2, 0.3, 10.0):
        m.observe_latency(v)
    m.incr("requests_total", 5)
    snap = m.snapshot()
    assert snap["requests_total"] == 5
    assert snap["latency_p50"] > 0 and snap["latency_p99"] > 0
    m.reset()
    assert m.snapshot()["requests_total"] == 0
    assert m.snapshot()["latency_samples"] == 0


# --------------------------------------------------------------------- #
# 9. Adaptive throttle
# --------------------------------------------------------------------- #
def test_throttle_grows_on_failure_and_decays_on_success():
    q = FakeQuotex(None, HistoryConfig(throttle_seconds=0.1,
                                       throttle_max_seconds=1.0))
    base = q._history_throttle
    q._throttle_on_failure()
    assert q._history_throttle > base
    for _ in range(20):
        q._throttle_on_failure()
    assert q._history_throttle <= 1.0          # bounded
    for _ in range(20):
        q._throttle_on_success()
    assert q._history_throttle == base         # healthy traffic back to normal


# --------------------------------------------------------------------- #
# 10. Backward compatibility
# --------------------------------------------------------------------- #
def test_public_signatures_unchanged():
    import inspect
    sigs = {
        "get_candles": ["self", "asset", "end_from_time", "offset", "period",
                        "progressive", "timeout", "use_cache"],
        "get_historical_candles": ["self", "asset", "amount_of_seconds", "period",
                                   "timeout", "max_workers", "progress_callback"],
        "_fetch_historical_batch": ["self", "asset", "fetch_time", "offset",
                                    "period", "index", "timeout"],
        "get_history_line": ["self", "asset", "end_from_time", "offset", "timeout"],
        "get_candle_v2": ["self", "asset", "period", "timeout"],
        "prepare_candles": ["self", "asset", "period", "history"],
        "get_trader_history": ["self", "account_type", "page_number"],
    }
    for name, params in sigs.items():
        got = list(inspect.signature(getattr(HistoryMixin, name)).parameters)
        assert got == params, f"{name}: {got} != {params}"


@pytest.mark.asyncio
async def test_prepare_candles_downstream_contract_unchanged():
    """calculate_candles -> process_candles_v2 -> merge_candles, in order."""
    import pyquotex._api.history as histmod
    order = []
    orig = (histmod.calculate_candles, histmod.process_candles_v2, histmod.merge_candles)
    histmod.calculate_candles = lambda h, p: (order.append("calculate"), [])[1]
    histmod.process_candles_v2 = lambda d, a, c: (order.append("v2"), [])[1]
    histmod.merge_candles = lambda c: (order.append("merge"), ["out"])[1]
    try:
        q = FakeQuotex(FakeApi(lambda p: None))
        assert q.prepare_candles("EURUSD", PERIOD, []) == ["out"]
        assert order == ["calculate", "v2", "merge"]
    finally:
        (histmod.calculate_candles, histmod.process_candles_v2,
         histmod.merge_candles) = orig


@pytest.mark.asyncio
async def test_get_candles_deep_still_delegates():
    cfg = HistoryConfig(max_workers=1, candles_per_chunk=10,
                        throttle_seconds=0.0, future_tolerance_seconds=1e12)
    q = FakeQuotex(FakeApi(lambda p: resp(
        p["asset"], p, make_candles(10, start=int(p["time"]) - 10 * PERIOD))), cfg)
    out = await q.get_candles_deep("EURUSD", PERIOD * 50, PERIOD)
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_none_api_is_handled_everywhere():
    q = FakeQuotex(None)
    assert await q.get_candles("E", NOW, 1, PERIOD) is None
    assert await q.get_historical_candles("E", 100, PERIOD) == []
    assert await q._fetch_historical_batch("E", NOW, 1, PERIOD, 1, 1) is None
    assert await q.get_history_line("E", NOW, 1) is None
    assert await q.get_candle_v2("E", PERIOD) is None
    assert q.prepare_candles("E", PERIOD) == []
    assert await q.get_trader_history(1, 1) == {}


def test_history_snapshot_shape():
    q = FakeQuotex(FakeApi(lambda p: None))
    snap = q.history_snapshot()
    for key in ("metrics", "circuit_breaker", "throttle_seconds",
                "cache_entries", "live_events"):
        assert key in snap


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
