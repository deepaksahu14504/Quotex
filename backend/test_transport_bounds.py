"""Regression tests for the three E2E-audit findings (D1, D2, D3).

Every test in here fails against the pre-fix code.
Run: cd backend && python3 -m pytest test_transport_bounds.py -q
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[1] / "vendor" / "pyquotex"))

import pytest  # noqa: E402

from app.services.market import PyQuotexProvider  # noqa: E402
import app.ws_hub as hubmod  # noqa: E402
from app.ws_hub import WSHub  # noqa: E402

# Defensive: the constant only exists after the fix. Importing it directly
# would make this module fail to COLLECT against pre-fix code, which proves
# nothing -- the assertions below have to be what catches the bug.
BROADCAST_TIMEOUT = getattr(hubmod, "BROADCAST_TIMEOUT", 5.0)
from pyquotex.global_value import WebsocketStatus  # noqa: E402
from pyquotex.ws import client as wsclient  # noqa: E402


# =================================================================== #
# D1 — ws/client.py::send() is the last line of defence
# =================================================================== #
class _DeadTransport:
    """Half-open TCP: send() never returns and never raises."""
    def __init__(self):
        self.state = wsclient.State.OPEN

    async def send(self, data):
        await asyncio.Event().wait()


class _LiveTransport:
    def __init__(self):
        self.state = wsclient.State.OPEN
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class _FakeApi:
    def __init__(self):
        self.state = type("_S", (), {"status": WebsocketStatus.CONNECTED})()


def make_client(transport, api=None, timeout=0.3):
    c = wsclient.WebsocketClient.__new__(wsclient.WebsocketClient)
    c._ws = transport
    c.api = api if api is not None else _FakeApi()
    wsclient.WS_SEND_TIMEOUT = timeout   # no-op against pre-fix code
    return c


@pytest.fixture(autouse=True)
def _restore_timeout():
    original = getattr(wsclient, "WS_SEND_TIMEOUT", None)
    yield
    if original is not None:
        wsclient.WS_SEND_TIMEOUT = original


@pytest.mark.asyncio
async def test_send_on_a_half_open_socket_does_not_hang():
    """PRE-FIX: blocked forever. Neither `except ConnectionClosed` nor
    `except Exception` can catch it, because nothing is raised."""
    c = make_client(_DeadTransport())
    task = asyncio.ensure_future(c.send('42["tick"]'))
    await asyncio.sleep(1.0)                 # 3x the ceiling
    assert task.done(), "send() never returned -- still unbounded"
    with pytest.raises(TimeoutError):
        await task


@pytest.mark.asyncio
async def test_dead_send_marks_the_transport_so_reconnect_engages():
    api = _FakeApi()
    c = make_client(_DeadTransport(), api)
    with pytest.raises(TimeoutError):
        await c.send("x")
    assert api.state.status == WebsocketStatus.ERROR


@pytest.mark.asyncio
async def test_the_heartbeat_loop_can_now_exit():
    """The heartbeat's `except Exception: break` was unreachable, so the
    task stayed alive forever doing nothing on a dead socket."""
    c = make_client(_DeadTransport())
    iterations = {"n": 0}

    async def heartbeat():
        while True:
            iterations["n"] += 1
            try:
                await c.send('42["tick"]')
            except Exception:
                break
            await asyncio.sleep(0.05)

    await asyncio.wait_for(heartbeat(), timeout=5)
    assert iterations["n"] == 1, "the loop must exit on the first dead send"


@pytest.mark.asyncio
async def test_healthy_sends_are_untouched():
    t = _LiveTransport()
    c = make_client(t, timeout=1.0)
    for i in range(50):
        await c.send(f"m{i}")
    assert len(t.sent) == 50
    assert c.api.state.status == WebsocketStatus.CONNECTED


@pytest.mark.asyncio
async def test_closed_socket_still_logs_instead_of_raising():
    """Existing behaviour preserved: a closed socket is not an error."""
    t = _LiveTransport()
    t.state = wsclient.State.CLOSED
    c = make_client(t)
    await c.send("x")                        # must not raise
    assert t.sent == []


# =================================================================== #
# D2 — tick cursor identity is (asset, timeframe)
# =================================================================== #
class _Buf:
    def __init__(self):
        self.ticks = []

    def push(self, ts, p):
        self.ticks.append({"time": ts, "price": p})
        if len(self.ticks) > 5000:
            self.ticks.pop(0)


class _TickClient:
    def __init__(self, buf):
        self.buf = buf

    async def start_candles_stream(self, a, p):
        pass

    async def get_realtime_price(self, a):
        return list(self.buf.ticks)

    async def get_historical_candles(self, *a, **k):
        return []

    async def get_candles(self, *a, **k):
        return []


def make_provider(buf):
    p = PyQuotexProvider.__new__(PyQuotexProvider)
    p._client = _TickClient(buf)
    p._connected = True
    p._candle_cache = {}
    p._streamed = {("EURUSD", 60), ("EURUSD", 300)}
    p._subscribing = set()
    p._stream_tick_ts = {}
    p._last_resync = {}
    p._cache_lru = []
    p.new_data_event = asyncio.Event()
    return p


def thirty_minutes():
    buf = _Buf()
    base = 1_700_000_000
    for i in range(1800):
        buf.push(base + i, 1.0 + i * 1e-5)
    return buf


@pytest.mark.asyncio
async def test_second_timeframe_is_not_starved_1m_first():
    """PRE-FIX: 1m got 31 buckets, 5m got 1 (should be 6)."""
    p = make_provider(thirty_minutes())
    await p.get_candles("EURUSD", "1m", 500)
    await p.get_candles("EURUSD", "5m", 500)
    assert len(p._candle_cache["EURUSD:60"]) >= 30
    assert len(p._candle_cache["EURUSD:300"]) >= 6


@pytest.mark.asyncio
async def test_second_timeframe_is_not_starved_5m_first():
    """PRE-FIX: 5m got 7 buckets, 1m got 1 (should be 30). Order must not
    matter."""
    p = make_provider(thirty_minutes())
    await p.get_candles("EURUSD", "5m", 500)
    await p.get_candles("EURUSD", "1m", 500)
    assert len(p._candle_cache["EURUSD:300"]) >= 6
    assert len(p._candle_cache["EURUSD:60"]) >= 30


@pytest.mark.asyncio
async def test_cursor_keys_are_asset_and_period_pairs():
    p = make_provider(thirty_minutes())
    await p.get_candles("EURUSD", "1m", 500)
    await p.get_candles("EURUSD", "5m", 500)
    assert set(p._stream_tick_ts) == {("EURUSD", 60), ("EURUSD", 300)}


@pytest.mark.asyncio
async def test_timeframes_advance_independently():
    buf = thirty_minutes()
    p = make_provider(buf)
    await p.get_candles("EURUSD", "1m", 500)
    only_1m = dict(p._stream_tick_ts)
    assert list(only_1m) == [("EURUSD", 60)]
    await p.get_candles("EURUSD", "5m", 500)
    assert p._stream_tick_ts[("EURUSD", 60)] == only_1m[("EURUSD", 60)]


@pytest.mark.asyncio
async def test_different_assets_stay_isolated():
    p = make_provider(thirty_minutes())
    p._streamed.add(("GBPUSD", 60))
    await p.get_candles("EURUSD", "1m", 500)
    await p.get_candles("GBPUSD", "1m", 500)
    assert len(p._candle_cache["EURUSD:60"]) >= 30
    assert len(p._candle_cache["GBPUSD:60"]) >= 30


# =================================================================== #
# D3 — ws_hub.broadcast() is bounded
# =================================================================== #
class _HungWS:
    async def send_text(self, m):
        await asyncio.Event().wait()

    async def close(self):
        pass


class _OkWS:
    def __init__(self):
        self.n = 0

    async def send_text(self, m):
        self.n += 1

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_broadcast_does_not_hang_on_a_dead_client():
    """PRE-FIX: broadcast() never returned, and it is awaited from 51
    places in the orchestrator including inside the scan loop."""
    hubmod.BROADCAST_TIMEOUT = 0.3
    try:
        hub = WSHub()
        hub._clients["u"] = {_OkWS(), _HungWS(), _OkWS()}
        t0 = time.monotonic()
        await asyncio.wait_for(hub.broadcast("u", "e", {"x": 1}), timeout=5)
        assert time.monotonic() - t0 < 2.0
    finally:
        hubmod.BROADCAST_TIMEOUT = BROADCAST_TIMEOUT


@pytest.mark.asyncio
async def test_timed_out_connections_are_unregistered():
    hubmod.BROADCAST_TIMEOUT = 0.3
    try:
        hub = WSHub()
        hub._clients["u"] = {_HungWS()}
        await asyncio.wait_for(hub.broadcast("u", "e", {}), timeout=5)
        assert not hub._clients.get("u"), "a hung client must be dropped"
    finally:
        hubmod.BROADCAST_TIMEOUT = BROADCAST_TIMEOUT


@pytest.mark.asyncio
async def test_healthy_broadcast_is_unaffected():
    hub = WSHub()
    a, b = _OkWS(), _OkWS()
    hub._clients["u"] = {a, b}
    t0 = time.monotonic()
    await hub.broadcast("u", "e", {"x": 1})
    assert a.n == 1 and b.n == 1
    assert time.monotonic() - t0 < 0.5
    assert len(hub._clients["u"]) == 2


@pytest.mark.asyncio
async def test_broadcast_to_no_clients_is_a_noop():
    hub = WSHub()
    await hub.broadcast("nobody", "e", {})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
