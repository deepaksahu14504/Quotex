"""Tests for the history/load request channel (ws/channels/candles.py).

The central compatibility test is test_wire_format_is_byte_identical:
it compares this channel's output against the ORIGINAL implementation
reproduced inline, so any drift in the frame the broker receives fails
the suite.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from pyquotex.utils import json_utils as json  # noqa: E402
from pyquotex.ws.channels import candles as mod  # noqa: E402
from pyquotex.ws.channels.candles import (  # noqa: E402
    CandleRequestError,
    GetCandles,
    inflight_snapshot,
)


class FakeApi:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    async def send_websocket_request(self, data):
        if self.fail is not None:
            raise self.fail
        self.sent.append(data)


@pytest.fixture(autouse=True)
def _clear_inflight():
    mod._inflight.clear()
    yield
    mod._inflight.clear()


def original_frame(asset, index, time, offset, period):
    """The pre-change implementation, verbatim."""
    payload = {"asset": asset, "index": index, "time": time,
               "offset": offset, "period": period}
    return f'42["history/load",{json.dumps_str(payload)}]'


# ---------------- compatibility ---------------- #
@pytest.mark.asyncio
async def test_wire_format_is_byte_identical():
    api = FakeApi()
    ch = GetCandles(api)
    cases = [
        ("EURUSD", 1700000000, 1700000000, 360, 60),
        ("GBPUSD_otc", 1700000000123, 1699999999.5, 12000, 300),
        ("USDJPY", 42, 1650000000, 60, 1),
    ]
    for asset, index, t, offset, period in cases:
        await ch(asset, index, t, offset, period)
    assert api.sent == [original_frame(*c) for c in cases]


@pytest.mark.asyncio
async def test_signature_and_return_unchanged():
    api = FakeApi()
    ch = GetCandles(api)
    assert await ch("EURUSD", 1, 1700000000, 360, 60) is None
    assert GetCandles.name == "candles"


@pytest.mark.asyncio
async def test_positional_and_keyword_calls_both_work():
    api = FakeApi()
    ch = GetCandles(api)
    await ch("EURUSD", 1, 1700000000, 360, 60)
    await ch(asset="EURUSD", index=2, time=1700000000, offset=360, period=60)
    assert len(api.sent) == 2


# ---------------- validation (the silent-timeout source) ---------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,field", [
    ({"asset": ""}, "asset"),
    ({"asset": "   "}, "asset"),
    ({"asset": None}, "asset"),
    ({"index": None}, "index"),
    ({"period": 0}, "period"),
    ({"period": -60}, "period"),
    ({"period": "60"}, "period"),
    ({"period": True}, "period"),
    ({"offset": 0}, "offset"),
    ({"offset": -1}, "offset"),
    ({"time": 0}, "time"),
    ({"time": -5}, "time"),
    ({"time": "now"}, "time"),
])
async def test_unanswerable_requests_fail_fast(kwargs, field):
    """Each of these was previously serialised and sent; the broker
    answered with nothing and the caller waited out its full timeout,
    logging 'candles_ready_<asset> was never set'."""
    api = FakeApi()
    ch = GetCandles(api)
    base = dict(asset="EURUSD", index=1, time=1700000000, offset=360, period=60)
    base.update(kwargs)
    with pytest.raises(CandleRequestError) as exc:
        await ch(**base)
    assert field in str(exc.value)
    assert api.sent == [], "a rejected request must never reach the socket"


@pytest.mark.asyncio
async def test_validation_error_is_a_valueerror():
    """Backward compatibility: callers catching ValueError still work."""
    api = FakeApi()
    ch = GetCandles(api)
    with pytest.raises(ValueError):
        await ch("EURUSD", 1, 1700000000, 360, 0)


@pytest.mark.asyncio
async def test_unusual_but_legitimate_values_are_accepted():
    """Validation must reject only the impossible, never the merely odd."""
    api = FakeApi()
    ch = GetCandles(api)
    await ch("EURUSD", 1, 1_000_000_000, 5_000_000, 86400)   # very old, huge offset, 1d
    await ch("EURUSD_otc", 999, 1700000000.75, 1, 1)          # float time, 1s period
    assert len(api.sent) == 2


# ---------------- send failure ---------------- #
@pytest.mark.asyncio
async def test_send_failure_is_reraised_not_swallowed():
    api = FakeApi(fail=TimeoutError("transport considered dead"))
    ch = GetCandles(api)
    with pytest.raises(TimeoutError):
        await ch("EURUSD", 1, 1700000000, 360, 60)


@pytest.mark.asyncio
async def test_send_failure_clears_the_inflight_entry():
    """A retry with the same index must not be misreported as a duplicate."""
    api = FakeApi(fail=TimeoutError("dead"))
    ch = GetCandles(api)
    with pytest.raises(TimeoutError):
        await ch("EURUSD", 7, 1700000000, 360, 60)
    assert inflight_snapshot()["count"] == 0


@pytest.mark.asyncio
async def test_cancellation_propagates():
    api = FakeApi(fail=asyncio.CancelledError())
    ch = GetCandles(api)
    with pytest.raises(asyncio.CancelledError):
        await ch("EURUSD", 1, 1700000000, 360, 60)


# ---------------- duplicate detection ---------------- #
@pytest.mark.asyncio
async def test_duplicate_inflight_is_detected_and_logged(caplog):
    """get_candles() passes expiration.get_timestamp() -- a WHOLE-SECOND
    value -- so two same-asset requests inside one second share an index."""
    api = FakeApi()
    ch = GetCandles(api)
    idx = 1700000000
    with caplog.at_level("WARNING"):
        await ch("EURUSD", idx, 1700000000, 360, 60)
        await ch("EURUSD", idx, 1700000000, 360, 60)
    # getMessage() applies the %-args; the earlier version had broken
    # operator precedence and would have passed on almost anything.
    assert any("DUPLICATE_INFLIGHT" in r.getMessage() for r in caplog.records)
    assert len(api.sent) == 2, "detection must not suppress the request"


@pytest.mark.asyncio
async def test_different_assets_same_index_is_not_a_duplicate(caplog):
    api = FakeApi()
    ch = GetCandles(api)
    with caplog.at_level("WARNING"):
        await ch("EURUSD", 1700000000, 1700000000, 360, 60)
        await ch("GBPUSD", 1700000000, 1700000000, 360, 60)
    assert not any("DUPLICATE_INFLIGHT" in r.getMessage() for r in caplog.records)


# ---------------- bounded memory ---------------- #
@pytest.mark.asyncio
async def test_inflight_table_is_hard_capped():
    api = FakeApi()
    ch = GetCandles(api)
    for i in range(mod._INFLIGHT_MAX * 3):
        await ch(f"A{i % 50}", i, 1700000000, 360, 60)
    assert len(mod._inflight) <= mod._INFLIGHT_MAX


@pytest.mark.asyncio
async def test_stale_entries_are_pruned_by_ttl(monkeypatch):
    api = FakeApi()
    ch = GetCandles(api)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod._time, "monotonic", lambda: clock["t"])
    await ch("EURUSD", 1, 1700000000, 360, 60)
    assert len(mod._inflight) == 1
    clock["t"] += mod._INFLIGHT_TTL_SECONDS + 1
    await ch("GBPUSD", 2, 1700000000, 360, 60)
    assert len(mod._inflight) == 1, "expired entry should have been pruned"


@pytest.mark.asyncio
async def test_inflight_snapshot_shape():
    api = FakeApi()
    ch = GetCandles(api)
    await ch("EURUSD", 1, 1700000000, 360, 60)
    snap = inflight_snapshot()
    assert snap["count"] == 1
    assert snap["requests"][0]["asset"] == "EURUSD"
    assert "oldest_age_s" in snap


# ---------------- non-blocking ---------------- #
@pytest.mark.asyncio
async def test_channel_does_not_block_the_event_loop():
    api = FakeApi()
    ch = GetCandles(api)
    ticks = {"n": 0}

    async def ticker():
        for _ in range(50):
            ticks["n"] += 1
            await asyncio.sleep(0)

    async def sender():
        for i in range(200):
            await ch(f"A{i % 10}", i, 1700000000, 360, 60)

    await asyncio.gather(ticker(), sender())
    assert ticks["n"] == 50


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
