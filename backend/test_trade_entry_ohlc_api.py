"""End-to-end tests for entry-candle OHLC reaching the client.

These go through the real FastAPI route for trade history, with only the
orchestrator dependency overridden. The point is narrow but worth pinning: the
five entry-candle fields have to survive serialization, because the chart marker
and Trade History both read them off this response and there is no other
endpoint that carries them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_orch
from app.schemas import Direction, TradeRecord, TradeStatus

# 2025-09-30 15:30:00 UTC, 15m-aligned (verified with datetime).
T1530 = 1759246200


def settled_trade(**overrides) -> TradeRecord:
    """A trade as the execution path leaves it, entry candle attached."""
    base = dict(
        id="t1", asset="EURUSD_otc", direction=Direction.CALL, amount=500.0,
        duration=900, is_demo=True, status=TradeStatus.WIN,
        open_price=1.14422, close_price=1.14510, profit=425.0, payout=85.0,
        timeframe="15m", created_at=float(T1530 + 138), closed_at=float(T1530 + 900),
        entry_candle_timestamp=float(T1530),
        entry_candle_open=1.14410, entry_candle_high=1.14435,
        entry_candle_low=1.14400, entry_candle_close=1.14422,
    )
    base.update(overrides)
    return TradeRecord(**base)


class StubStore:
    def __init__(self, trades):
        self._trades = trades

    def recent(self, limit=100):
        return list(self._trades)[:limit]


class StubOrch:
    def __init__(self, trades):
        self.store = StubStore(trades)


def _override(trades):
    app.dependency_overrides[get_orch] = lambda: StubOrch(trades)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_the_entry_ohlc_reaches_the_client(client):
    _override([settled_trade()])

    r = client.get("/api/trades", params={"limit": 10})

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    t = body[0]
    assert t["entry_candle_timestamp"] == float(T1530)
    assert t["entry_candle_open"] == 1.14410
    assert t["entry_candle_high"] == 1.14435
    assert t["entry_candle_low"] == 1.14400
    assert t["entry_candle_close"] == 1.14422


def test_the_entry_candle_and_the_chart_agree_on_one_bucket(client):
    """The requirement's core check: the trade's entry candle must be the same
    bar the chart would draw for that execution instant."""
    from app.services.candle_aggregate import bucket_start
    from app.services.chart_service import timeframe_period

    _override([settled_trade()])
    t = client.get("/api/trades", params={"limit": 10}).json()[0]

    chart_bucket = bucket_start(t["created_at"], timeframe_period(t["timeframe"]))
    assert t["entry_candle_timestamp"] == float(chart_bucket) == float(T1530)
    # And it is neither neighbouring bar.
    assert t["entry_candle_timestamp"] != float(T1530 - 900)
    assert t["entry_candle_timestamp"] != float(T1530 + 900)


def test_the_broker_result_is_carried_not_recomputed(client):
    """status and profit come from the provider's check_result. The client
    reads them as-is; IN THE MONEY / OUT OF THE MONEY is a label on that."""
    _override([settled_trade(status=TradeStatus.LOSS, profit=-500.0)])

    t = client.get("/api/trades", params={"limit": 10}).json()[0]

    assert t["status"] == "loss"
    assert t["profit"] == -500.0
    assert t["close_price"] == 1.14510
    assert t["closed_at"] == float(T1530 + 900)


def test_a_trade_without_an_entry_candle_reports_nulls(client):
    """Older records, or one whose bar could not be resolved. Nulls, not zeros
    and not a neighbouring bar's prices."""
    _override([settled_trade(
        entry_candle_timestamp=None, entry_candle_open=None,
        entry_candle_high=None, entry_candle_low=None, entry_candle_close=None,
    )])

    t = client.get("/api/trades", params={"limit": 10}).json()[0]

    for key in ("entry_candle_timestamp", "entry_candle_open", "entry_candle_high",
                "entry_candle_low", "entry_candle_close"):
        assert key in t, f"{key} should be present even when null"
        assert t[key] is None


def test_an_open_trade_carries_its_entry_candle_before_settlement(client):
    """Requirement 8: an active trade already shows its entry bar; only the
    result arrives later."""
    _override([settled_trade(
        status=TradeStatus.OPEN, profit=0.0, close_price=None, closed_at=None,
    )])

    t = client.get("/api/trades", params={"limit": 10}).json()[0]

    assert t["status"] == "open"
    assert t["entry_candle_timestamp"] == float(T1530)
    assert t["entry_candle_close"] == 1.14422
    assert t["closed_at"] is None


def test_the_entry_fields_survive_a_store_round_trip(tmp_path):
    """TradeStore is JSON, so prove the fields are written and read back --
    this is what makes the history correct after a restart."""
    from app.store import TradeStore

    store = TradeStore(scope_dir=tmp_path)
    trade = settled_trade()
    store.add(trade)
    trade.status = TradeStatus.WIN
    store.update(trade)

    reloaded = TradeStore(scope_dir=tmp_path)
    got = reloaded.recent(10)[0]

    assert got.entry_candle_timestamp == float(T1530)
    assert got.entry_candle_open == 1.14410
    assert got.entry_candle_high == 1.14435
    assert got.entry_candle_low == 1.14400
    assert got.entry_candle_close == 1.14422
    assert got.status == TradeStatus.WIN
