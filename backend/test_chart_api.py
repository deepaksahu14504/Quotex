"""Tests for the chart HTTP endpoints.

These hit the real FastAPI routes through TestClient, with only the auth and
orchestrator dependencies overridden. The provider is a stub, so nothing here
touches the network -- but the route, the validation and the serialization are
the real ones.
"""

from __future__ import annotations

from typing import List

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_orch
from app.schemas import AssetInfo, Candle
from app.services.chart_service import CHART_TIMEFRAMES

BASE = 1759201200  # a 15m-aligned epoch second


def _candle(ts: float, close: float = 1.1440, vol: float = 0.0,
            src: str = "synthetic") -> Candle:
    return Candle(timestamp=ts, open=1.1430, high=1.1450, low=1.1420,
                  close=close, volume=vol, volume_source=src)


class StubProvider:
    """Stands in for the market provider: records the call, returns fixtures."""

    def __init__(self, candles: List[Candle] | None = None,
                 assets: List[AssetInfo] | None = None,
                 raise_on_candles: bool = False,
                 raise_on_assets: bool = False):
        self._candles = candles if candles is not None else []
        self._assets = assets if assets is not None else [
            AssetInfo(symbol="EURUSD_otc", name="EUR/USD OTC", payout=93.0,
                      is_open=True, is_otc=True),
        ]
        self._raise_candles = raise_on_candles
        self._raise_assets = raise_on_assets
        self.calls: List[tuple] = []

    async def get_candles(self, asset, timeframe, count=120):
        self.calls.append(("candles", asset, timeframe, count))
        if self._raise_candles:
            raise RuntimeError("broker unavailable")
        return self._candles

    async def get_assets(self):
        if self._raise_assets:
            raise RuntimeError("broker unavailable")
        return self._assets


class StubOrch:
    def __init__(self, provider):
        self.provider = provider


@pytest.fixture
def client():
    return TestClient(app)


def _override(client, provider):
    """Point the real routes at a stub orchestrator, bypassing auth."""
    orch = StubOrch(provider)
    app.dependency_overrides[get_orch] = lambda: orch
    return orch


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# ── happy path ─────────────────────────────────────────────────────────────

def test_chart_candles_returns_the_envelope(client):
    provider = StubProvider(candles=[_candle(BASE + i * 60) for i in range(3)])
    _override(client, provider)

    r = client.get("/api/chart/candles",
                   params={"asset": "EURUSD_otc", "timeframe": "15m", "limit": 10})

    assert r.status_code == 200
    body = r.json()
    assert body["asset"] == "EURUSD_otc"
    assert body["timeframe"] == "15m"
    assert len(body["candles"]) == 3
    assert set(body["candles"][0]) == {
        "timestamp", "open", "high", "low", "close", "volume"}


def test_the_provider_is_called_with_the_normalized_timeframe(client):
    """The route must translate UI labels, not pass them through raw."""
    provider = StubProvider(candles=[_candle(BASE)])
    _override(client, provider)

    r = client.get("/api/chart/candles",
                   params={"asset": "EURUSD_otc", "timeframe": "1D", "limit": 5})

    assert r.status_code == 200
    assert provider.calls == [("candles", "EURUSD_otc", "1day", 5)]
    assert r.json()["timeframe"] == "1day"


@pytest.mark.parametrize("tf", CHART_TIMEFRAMES)
def test_every_chart_timeframe_is_accepted(client, tf):
    provider = StubProvider(candles=[_candle(BASE)])
    _override(client, provider)

    r = client.get("/api/chart/candles",
                   params={"asset": "EURUSD_otc", "timeframe": tf})

    assert r.status_code == 200, f"{tf} was rejected"


def test_timestamps_are_integer_seconds(client):
    provider = StubProvider(candles=[_candle(BASE + 0.5)])
    _override(client, provider)

    r = client.get("/api/chart/candles", params={"asset": "EURUSD_otc"})
    ts = r.json()["candles"][0]["timestamp"]

    assert isinstance(ts, int), "timestamps must be int seconds, not floats or ms"
    assert ts == BASE


def test_synthetic_volume_is_null_and_real_volume_is_a_number(client):
    provider = StubProvider(candles=[
        _candle(BASE, vol=7.0, src="synthetic"),
        _candle(BASE + 60, vol=3.0, src="real"),
    ])
    _override(client, provider)

    r = client.get("/api/chart/candles", params={"asset": "EURUSD_otc"})
    candles = r.json()["candles"]

    assert candles[0]["volume"] is None, "a tick count is not volume"
    assert candles[1]["volume"] == 3.0


# ── validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["7m", "1w", "1minute", "", "99m"])
def test_an_unknown_timeframe_is_rejected_with_400(client, bad):
    provider = StubProvider()
    _override(client, provider)

    r = client.get("/api/chart/candles",
                   params={"asset": "EURUSD_otc", "timeframe": bad})

    assert r.status_code == 400
    assert "unsupported timeframe" in r.json()["detail"]
    assert provider.calls == [], "the broker must not be asked for a bad timeframe"


def test_invalid_candles_are_dropped_and_counted(client):
    good = [_candle(BASE), _candle(BASE + 60)]
    bad = [Candle(timestamp=BASE + 120, open=1.14, high=0.5, low=1.13, close=1.14)]
    provider = StubProvider(candles=good + bad)
    _override(client, provider)

    r = client.get("/api/chart/candles", params={"asset": "EURUSD_otc"})
    body = r.json()

    assert len(body["candles"]) == 2
    assert body["rejected"] == 1


def test_candles_are_sorted_and_unique(client):
    provider = StubProvider(candles=[
        _candle(BASE + 120), _candle(BASE), _candle(BASE + 60), _candle(BASE + 60),
    ])
    _override(client, provider)

    r = client.get("/api/chart/candles", params={"asset": "EURUSD_otc"})
    ts = [c["timestamp"] for c in r.json()["candles"]]

    assert ts == sorted(ts)
    assert len(ts) == len(set(ts)), "duplicate timestamps reached the client"


def test_limit_trims_to_the_most_recent(client):
    provider = StubProvider(candles=[_candle(BASE + i * 60) for i in range(50)])
    _override(client, provider)

    r = client.get("/api/chart/candles",
                   params={"asset": "EURUSD_otc", "limit": 5})
    candles = r.json()["candles"]

    assert len(candles) == 5
    assert candles[-1]["timestamp"] == BASE + 49 * 60, "must keep the newest"


def test_an_absurd_limit_is_capped(client):
    provider = StubProvider(candles=[_candle(BASE)])
    _override(client, provider)

    r = client.get("/api/chart/candles",
                   params={"asset": "EURUSD_otc", "limit": 999999})

    assert r.status_code == 200
    assert provider.calls[0][3] == 1000, "limit must be capped before the fetch"


# ── error states ───────────────────────────────────────────────────────────

def test_a_provider_failure_is_502_not_an_empty_chart(client):
    """An empty 200 would render as 'no market data' and hide a real outage."""
    provider = StubProvider(raise_on_candles=True)
    _override(client, provider)

    r = client.get("/api/chart/candles", params={"asset": "EURUSD_otc"})

    assert r.status_code == 502
    assert r.json()["detail"] == "market data unavailable"


def test_an_empty_result_is_a_valid_empty_chart(client):
    provider = StubProvider(candles=[])
    _override(client, provider)

    r = client.get("/api/chart/candles", params={"asset": "EURUSD_otc"})

    assert r.status_code == 200
    assert r.json()["candles"] == []


# ── security ───────────────────────────────────────────────────────────────

def test_no_credentials_or_session_material_in_the_response(client):
    provider = StubProvider(candles=[_candle(BASE)])
    _override(client, provider)

    r = client.get("/api/chart/candles", params={"asset": "EURUSD_otc"})
    blob = r.text.lower()

    for leak in ("ssid", "cookie", "password", "token", "email",
                 "authorization", "session_data"):
        assert leak not in blob, f"leaked {leak!r} into the chart response"


def test_chart_meta_carries_no_credentials(client):
    provider = StubProvider()
    _override(client, provider)

    r = client.get("/api/chart/meta")
    blob = r.text.lower()

    for leak in ("ssid", "cookie", "password", "token", "authorization"):
        assert leak not in blob, f"leaked {leak!r} into chart meta"


# ── meta ───────────────────────────────────────────────────────────────────

def test_chart_meta_lists_timeframes_and_provider_assets(client):
    provider = StubProvider()
    _override(client, provider)

    r = client.get("/api/chart/meta")
    body = r.json()

    assert body["timeframes"] == CHART_TIMEFRAMES
    assert body["assets"][0]["symbol"] == "EURUSD_otc"
    assert body["assets"][0]["payout"] == 93.0


def test_chart_meta_surfaces_a_provider_failure(client):
    provider = StubProvider(raise_on_assets=True)
    _override(client, provider)

    r = client.get("/api/chart/meta")

    assert r.status_code == 502


# ── the existing endpoint must be untouched ────────────────────────────────

def test_the_legacy_candles_endpoint_still_works(client):
    """/api/candles has existing callers; the chart must not change it."""
    provider = StubProvider(candles=[_candle(BASE)])
    _override(client, provider)

    r = client.get("/api/candles", params={"asset": "EURUSD_otc", "timeframe": "1m"})

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list), "legacy shape is a bare list"
    assert body[0]["volume_source"] == "synthetic", "legacy shape keeps volume_source"
