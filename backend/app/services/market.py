"""Market data + execution providers.

`PublicDataProvider` uses **real** public market data from Binance (with a
Bybit fallback) — no account or API key required — and executes trades on
paper (virtual balance) against real prices. Nothing here is faked/mocked.

`PyQuotexProvider` wraps the vendored `pyquotex.stable_api.Quotex` for real
demo/live Quotex connectivity (imported lazily).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple

import httpx
import websockets

from ..logging_setup import log_event
from ..schemas import AssetInfo, Candle, Direction

logger = logging.getLogger(__name__)

TIMEFRAMES = {
    "30s": 30, "1m": 60, "2m": 120, "3m": 180, "5m": 300,
    "10m": 600, "15m": 900, "30m": 1800, "1h": 3600,
    "4h": 14400, "1day": 86400,
}


class MarketProvider:
    name = "base"

    def __init__(self) -> None:
        # Set whenever fresh tick/candle data actually arrives, so the
        # orchestrator's scan loop can react immediately instead of waiting
        # out a fixed poll interval. A provider that never sets this (e.g. an
        # untouched custom provider) just falls back to the scan loop's
        # timeout — behaves exactly like the old fixed-interval polling.
        self.new_data_event: asyncio.Event = asyncio.Event()

    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    @property
    def connected(self) -> bool: ...
    async def get_balance(self) -> Tuple[float, str]: ...
    async def get_assets(self) -> List[AssetInfo]: ...

    async def get_candles(self, asset: str, timeframe: str, count: int = 120) -> List[Candle]: ...
    async def get_payout(self, asset: str, timeframe: str) -> float: ...
    async def place_order(self, asset: str, amount: float, direction: Direction, duration: int): ...
    async def check_result(self, order_id: str): ...

    async def recover_result(self, order_id: str):
        """Resolve an order from DURABLE broker state, for restart recovery.

        RCA F8: `check_result` waits on a live websocket notification. After a
        backend restart that notification is gone for good -- the slot it
        would have fired was in memory -- so an order that was open when the
        process died can never be settled through the normal path. This method
        reads whatever the broker itself still remembers instead.

        Same return contract as `check_result`: a result dict, or None when
        the broker has no record of the order (still running, or too old).
        Providers with no durable history just return None.
        """
        return None

    async def switch_account(self, account_mode: str, tournament_id: Optional[int] = None) -> bool:
        """Switch the active trading account: 'live', 'demo', or
        'tournament' (with tournament_id). Returns True if the switch is
        actually supported and applied, False otherwise (e.g. this
        provider has no concept of tournaments) -- callers must treat
        False as "graceful no-op", never as an error to surface. Default
        implementation here supports live/demo (setting nothing extra
        since providers already read is_demo) but not tournament, so any
        existing custom provider that doesn't override this keeps working
        exactly as before and simply reports tournament as unsupported."""
        return account_mode in ("live", "demo")

    def supports_tournaments(self) -> bool:
        """Whether this provider has ANY tournament capability at all --
        used by the UI to decide whether to show Tournament mode as an
        option in the first place. Default False; only PyQuotexProvider
        overrides this to True (real, code-verified capability -- not a
        guess)."""
        return False

    async def get_realtime_sentiment(self, asset: str) -> Optional[dict]:
        """Raw trader-sentiment payload for `asset`, or None if unavailable.

        Optional by design. A provider that does not publish sentiment returns
        None and the signal engine proceeds on OHLC alone -- absence of
        sentiment is never treated as evidence against a trade. The payload is
        returned UNNORMALISED: `market_features.normalize_sentiment()` owns the
        buy/sell key handling and the bias maths, so providers never have to
        agree on a shape.
        """
        return None

    async def get_realtime_ticks(self, asset: str) -> List[dict]:
        """Raw realtime price ticks for `asset`, or [] if unavailable.

        These are *price updates*, not traded volume. Nothing downstream may
        convert the count into a volume figure.
        """
        return []
    async def fetch_history(self, asset: str, timeframe: str, bars: int) -> List[Candle]:
        """Deep historical fetch for backtesting — independent of the small
        rolling cache get_candles() maintains for live scanning. Default
        implementation just delegates to get_candles(); providers that can
        pull deeper history override this."""
        return await self.get_candles(asset, timeframe, bars)


# --------------------------------------------------------------------------- #
# Public market-data provider — REAL candles from Binance / Bybit, paper trades
# --------------------------------------------------------------------------- #
_PAIRS = [
    ("BTCUSDT", "BTC/USDT"), ("ETHUSDT", "ETH/USDT"), ("BNBUSDT", "BNB/USDT"),
    ("SOLUSDT", "SOL/USDT"), ("XRPUSDT", "XRP/USDT"), ("ADAUSDT", "ADA/USDT"),
    ("DOGEUSDT", "DOGE/USDT"), ("AVAXUSDT", "AVAX/USDT"), ("LINKUSDT", "LINK/USDT"),
    ("LTCUSDT", "LTC/USDT"), ("DOTUSDT", "DOT/USDT"), ("TRXUSDT", "TRX/USDT"),
]
# app timeframe -> exchange interval in minutes (nearest supported)
_TF_MIN = {"30s": 1, "1m": 1, "2m": 3, "3m": 3, "5m": 5, "10m": 15, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1day": 1440}
_BINANCE_INT = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}
_DEFAULT_PAYOUT = 90.0
_CANDLE_TTL = 2.0  # seconds — throttle identical candle requests
_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
_WS_RECONNECT_MAX_DELAY = 30.0


class OrderNotSent(RuntimeError):
    """The order was never transmitted to the broker — a definite no-trade.

    Distinct from a bare RuntimeError, which for an order means "something
    went wrong at some point in the attempt" and leaves open the possibility
    that the broker received it. Only raise this when the failure provably
    happened before transmission."""


@dataclass
class _PaperOrder:
    order_id: str
    asset: str
    amount: float
    direction: Direction
    entry_price: float
    expiry_ts: float
    payout: float
    resolved: bool = False


class PublicDataProvider(MarketProvider):
    def __init__(self, starting_balance: float = 10000.0):
        super().__init__()
        self._balance = starting_balance
        self._connected = False
        self._orders: Dict[str, _PaperOrder] = {}
        self._source = "binance"
        self._base = "https://data-api.binance.vision"
        self._http: Optional[httpx.AsyncClient] = None
        self._cache: Dict[str, Tuple[float, List[Candle]]] = {}
        self.name = "market"
        # --- WebSocket streaming state (Binance combined-stream) -------- #
        self._ws_task: Optional[asyncio.Task] = None
        self._ws: Optional[Any] = None
        self._ws_subscribed: set = set()          # stream names e.g. "btcusdt@kline_1m"
        self._ws_cache: Dict[str, Dict[float, Candle]] = {}  # "SYMBOL:mins" -> {bucket_ts: Candle}
        self._ws_seeded: set = set()               # keys that already got one REST history seed

    async def connect(self) -> bool:
        self._http = httpx.AsyncClient(timeout=12, headers={"User-Agent": "QuotexAutoTrader/1.0"})
        for source, base, ping in (
            ("binance", "https://data-api.binance.vision", "/api/v3/ping"),
            ("binance", "https://api.binance.com", "/api/v3/ping"),
            ("bybit", "https://api.bybit.com", "/v5/market/time"),
        ):
            try:
                r = await self._http.get(base + ping)
                if r.status_code == 200:
                    self._source, self._base, self.name = source, base, source
                    self._connected = True
                    break
            except Exception:
                continue
        if not self._connected:
            return False
        if self._source == "binance":
            # Real-time streaming replaces per-scan REST polling: candles
            # update as soon as Binance pushes a kline event instead of
            # waiting for the next timed poll.
            # Guard against a duplicate listener if connect() is ever
            # called twice without an intervening disconnect() (e.g. a
            # caller retrying concurrently) -- an orphaned second listener
            # would double-process every tick with nothing to stop it.
            if self._ws_task is None or self._ws_task.done():
                self._ws_task = asyncio.create_task(self._ws_listener())
        return True

    async def disconnect(self) -> None:
        self._connected = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._ws_subscribed.clear()
        if self._http:
            await self._http.aclose()

    # --- Binance WebSocket streaming ---------------------------------- #
    async def _ws_listener(self) -> None:
        """Maintains one long-lived connection to Binance's combined stream
        endpoint, (re)subscribing to whatever (asset, timeframe) pairs the
        orchestrator has actually asked for. Replaces the old approach of
        firing a fresh REST request every scan — candles now update the
        moment Binance pushes a kline event, and reconnects with backoff
        instead of dying silently on a dropped connection."""
        delay = 1.0
        while self._connected:
            try:
                async with websockets.connect(_BINANCE_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    if self._ws_subscribed:
                        await self._ws_subscribe(list(self._ws_subscribed))
                    delay = 1.0  # reset backoff after a clean (re)connect
                    async for raw in ws:
                        self._on_ws_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Binance websocket dropped (%s) — reconnecting in %.1fs", exc, delay)
            self._ws = None
            if not self._connected:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, _WS_RECONNECT_MAX_DELAY)

    async def _ws_subscribe(self, streams: List[str]) -> None:
        if not self._ws:
            return
        msg = {"method": "SUBSCRIBE", "params": streams, "id": int(time.time() * 1000) % 1_000_000}
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            pass

    async def _ensure_stream(self, asset: str, mins: int) -> None:
        interval = _BINANCE_INT.get(mins, "1m")
        stream = f"{asset.lower()}@kline_{interval}"
        if stream in self._ws_subscribed:
            return
        self._ws_subscribed.add(stream)
        await self._ws_subscribe([stream])

    def _on_ws_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        k = msg.get("k")
        symbol = msg.get("s")
        if not k or not symbol:
            return
        mins = next((m for m, iv in _BINANCE_INT.items() if iv == k.get("i")), None)
        if mins is None:
            return
        try:
            candle = Candle(
                timestamp=float(k["t"]) / 1000.0, open=float(k["o"]), high=float(k["h"]),
                low=float(k["l"]), close=float(k["c"]), volume=float(k["v"]),
            )
        except (KeyError, TypeError, ValueError):
            return
        key = f"{symbol}:{mins}"
        bucket = self._ws_cache.setdefault(key, {})
        bucket[candle.timestamp] = candle
        if len(bucket) > 800:  # cap memory, same ceiling used elsewhere in this file
            for old_ts in sorted(bucket)[: len(bucket) - 800]:
                bucket.pop(old_ts, None)
        self.new_data_event.set()

    async def _seed_ws_history(self, asset: str, timeframe: str, count: int) -> None:
        """One-off REST pull to backfill history behind a brand-new websocket
        subscription (the stream itself only carries candles from the moment
        we subscribed onward)."""
        try:
            hist = await self.fetch_history(asset, timeframe, count)
        except Exception:
            return
        mins = _TF_MIN.get(timeframe, 1)
        key = f"{asset}:{mins}"
        bucket = self._ws_cache.setdefault(key, {})
        for c in hist:
            bucket.setdefault(c.timestamp, c)

    @property
    def connected(self) -> bool:
        return self._connected

    async def get_balance(self) -> Tuple[float, str]:
        return round(self._balance, 2), "USDT"

    async def get_assets(self) -> List[AssetInfo]:
        return [AssetInfo(symbol=s, name=n, payout=_DEFAULT_PAYOUT, is_open=True, is_otc=False)
                for s, n in _PAIRS]

    async def _price(self, asset: str) -> Optional[float]:
        try:
            if self._source == "binance":
                r = await self._http.get(self._base + "/api/v3/ticker/price", params={"symbol": asset})
                return float(r.json()["price"])
            r = await self._http.get(self._base + "/v5/market/tickers", params={"category": "spot", "symbol": asset})
            return float(r.json()["result"]["list"][0]["lastPrice"])
        except Exception:
            return None

    async def get_candles(self, asset: str, timeframe: str, count: int = 120) -> List[Candle]:
        mins = _TF_MIN.get(timeframe, 1)
        key = f"{asset}:{mins}"

        if self._source == "binance":
            await self._ensure_stream(asset, mins)
            bucket = self._ws_cache.get(key)
            if bucket and len(bucket) >= min(count, 5):
                # Streaming path: seed older history once (websocket only
                # carries candles from the moment we subscribed onward),
                # then serve entirely from memory — no REST call per scan.
                if key not in self._ws_seeded:
                    await self._seed_ws_history(asset, timeframe, count)
                    self._ws_seeded.add(key)
                rows = [bucket[k] for k in sorted(bucket)][-count:]
                if len(rows) >= min(count, 5):
                    return rows

        # Fallback: websocket not connected / not subscribed long enough yet,
        # or a non-Binance source (bybit) which has no streaming path here.
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < _CANDLE_TTL:
            return cached[1][-count:]
        limit = min(1000, max(2, count))
        out: List[Candle] = []
        try:
            if self._source == "binance":
                interval = _BINANCE_INT.get(mins, "1m")
                r = await self._http.get(self._base + "/api/v3/klines",
                                         params={"symbol": asset, "interval": interval, "limit": limit})
                for row in r.json():
                    out.append(Candle(timestamp=row[0] / 1000, open=float(row[1]), high=float(row[2]),
                                      low=float(row[3]), close=float(row[4]), volume=float(row[5])))
            else:
                r = await self._http.get(self._base + "/v5/market/kline",
                                         params={"category": "spot", "symbol": asset, "interval": str(mins), "limit": limit})
                for row in r.json()["result"]["list"]:
                    out.append(Candle(timestamp=int(row[0]) / 1000, open=float(row[1]), high=float(row[2]),
                                      low=float(row[3]), close=float(row[4]), volume=float(row[5])))
                out.sort(key=lambda c: c.timestamp)
        except Exception:
            return cached[1][-count:] if cached else []
        self._cache[key] = (now, out)
        return out[-count:]

    async def get_payout(self, asset: str, timeframe: str) -> float:
        return _DEFAULT_PAYOUT

    async def fetch_history(self, asset: str, timeframe: str, bars: int) -> List[Candle]:
        """Paginated historical fetch — Binance/Bybit cap a single request at
        1000 candles, so for a real backtest (e.g. 30 days of 1m bars =
        43,200 candles) we walk backwards in time, prepending each batch."""
        mins = _TF_MIN.get(timeframe, 1)
        interval_seconds = mins * 60
        out: List[Candle] = []
        end_time_ms: Optional[int] = None
        for _ in range(max(1, -(-bars // 1000))):  # ceil(bars / 1000) requests
            try:
                if self._source == "binance":
                    interval = _BINANCE_INT.get(mins, "1m")
                    params = {"symbol": asset, "interval": interval, "limit": 1000}
                    if end_time_ms:
                        params["endTime"] = end_time_ms
                    r = await self._http.get(self._base + "/api/v3/klines", params=params)
                    batch = [
                        Candle(timestamp=row[0] / 1000, open=float(row[1]), high=float(row[2]),
                               low=float(row[3]), close=float(row[4]), volume=float(row[5]))
                        for row in r.json()
                    ]
                else:
                    params = {"category": "spot", "symbol": asset, "interval": str(mins), "limit": 1000}
                    if end_time_ms:
                        params["end"] = end_time_ms
                    r = await self._http.get(self._base + "/v5/market/kline", params=params)
                    batch = [
                        Candle(timestamp=int(row[0]) / 1000, open=float(row[1]), high=float(row[2]),
                               low=float(row[3]), close=float(row[4]), volume=float(row[5]))
                        for row in r.json()["result"]["list"]
                    ]
                    batch.sort(key=lambda c: c.timestamp)
            except Exception:
                break
            if not batch:
                break
            out = batch + out
            end_time_ms = int((batch[0].timestamp - interval_seconds) * 1000)
            if len(out) >= bars:
                break
        return out[-bars:]

    async def place_order(self, asset: str, amount: float, direction: Direction, duration: int):
        price = await self._price(asset)
        if price is None:
            cs = await self.get_candles(asset, "1m", 2)
            price = cs[-1].close if cs else 0.0
        oid = f"paper-{int(time.time() * 1000)}-{len(self._orders)}"
        self._balance -= amount
        self._orders[oid] = _PaperOrder(oid, asset, amount, direction, price, time.time() + duration, _DEFAULT_PAYOUT)
        return oid, price

    async def check_result(self, order_id: str):
        o = self._orders.get(order_id)
        if not o or o.resolved:
            return None
        if time.time() < o.expiry_ts:
            return None
        close = await self._price(o.asset)
        if close is None:
            cs = await self.get_candles(o.asset, "1m", 2)
            close = cs[-1].close if cs else o.entry_price
        o.resolved = True
        if abs(close - o.entry_price) < 1e-12:
            status, profit = "draw", 0.0
            self._balance += o.amount
        elif (o.direction == Direction.CALL and close > o.entry_price) or \
             (o.direction == Direction.PUT and close < o.entry_price):
            status = "win"
            profit = round(o.amount * o.payout / 100.0, 2)
            self._balance += o.amount + profit
        else:
            status, profit = "loss", -o.amount
        return {"status": status, "profit": profit, "close_price": close, "open_price": o.entry_price}


# --------------------------------------------------------------------------- #
# PyQuotex provider (cleitonleonel/pyquotex, lazy import)
# --------------------------------------------------------------------------- #
# Ceilings for individual pyquotex calls made from this provider. Every
# one of these funnels into QuotexAPI.send_websocket_request, which is now
# itself bounded -- these are the second layer, so a call that stalls for
# any other reason (a response event that never fires, a slow chunked
# history walk) also cannot pin the scan loop. Sized well above a healthy
# call so normal operation is untouched.
# How long a previously-good instrument snapshot may still be used after a
# fetch failure. Bounded deliberately: `is_open` is time-sensitive, and
# trading on a stale open-flag past this window is not safe. Beyond it the
# feed reports ERROR and the scanner idles rather than acting on old data.
ASSET_SNAPSHOT_STALE_TTL = 60.0

PROVIDER_IO_TIMEOUT = 20.0        # a single subscribe / candle pull

# Ceiling for one `check_win` call (RCA F7). pyquotex waits up to 300s
# internally and then reports ("loss", 0.0), which is indistinguishable from a
# genuine loss -- so we bail out long before that and leave the trade pending.
# 30s is ample: a settled deal arrives within seconds of expiry, and the
# result loop re-polls every couple of seconds anyway.
CHECK_RESULT_TIMEOUT = 30.0
PROVIDER_SEED_TIMEOUT = 45.0      # deep-history seed: chunked, legitimately slower.
                                  # Treated as the budget for a ~60-candle pull and
                                  # scaled up from there -- see get_candles().
PROVIDER_SEED_TIMEOUT_MAX = 180.0 # absolute ceiling for one seed attempt
# If a full deep-seed attempt adds fewer than this many candles, the broker has
# plateaued -- it simply will not serve more than it just gave us, and asking
# again immediately buys nothing but a repeat of the same ~150s wait.
SEED_PLATEAU_MIN_GAIN = 5
# How long to accept the plateau before trying a full deep-seed again (the
# broker's own limit could change). Ticks keep the cache growing slowly in
# the meantime via the normal resync path.
SEED_PLATEAU_COOLDOWN_SECONDS = 1800.0   # 30 min
# One history request must not try to walk an unbounded span: on a 15m
# timeframe, 300 candles is 75 hours, and asking for all of it in a single
# chunked call is how the seed times out and the asset never warms up. Assets
# short of their target simply re-seed on the next cycle and converge.
MAX_HISTORY_SPAN_SECONDS = 60 * 60 * 24 * 5   # 5 days
PROVIDER_DISCONNECT_TIMEOUT = 10.0


def _describe_row(row: Any) -> str:
    """One-line, non-sensitive description of a payload row.

    Instrument symbols are not secrets, but this deliberately reports
    only type, length and the first two positional values so a
    misrouted payload of any shape can be identified from the status
    endpoint without dumping arbitrary broker data into logs.
    """
    if isinstance(row, (list, tuple)):
        head = ", ".join(repr(v)[:24] for v in list(row)[:2])
        return f"{type(row).__name__}(len={len(row)}) [{head}]"
    if isinstance(row, dict):
        return f"dict(keys={sorted(row)[:6]})"
    return f"{type(row).__name__}({repr(row)[:40]})"


@dataclass
class AssetFeedSnapshot:
    """Everything a caller needs to tell the FIVE distinct zero-asset
    states apart.

    Before this existed, `get_assets()` swallowed every failure into an
    empty list (`except Exception: instruments = []`), so a broker
    timeout, a disconnected socket, a malformed payload and a genuinely
    closed market all surfaced identically as "0 open assets". The
    `except` branch on /api/status's assets check was unreachable dead
    code, because get_assets() could not raise.
    """

    # fetch outcome:
    #   "never_fetched" — no valid snapshot has EVER arrived (UNAVAILABLE)
    #   "ok"            — this refresh returned a valid non-empty snapshot
    #   "empty"         — this refresh returned zero rows and we have never
    #                     had a good one; genuinely nothing to serve
    #   "stale"         — this refresh failed OR came back empty, and the
    #                     last known-good snapshot is still within TTL
    #                     (DEGRADED — the universe is preserved)
    #   "error"         — refresh unusable and no snapshot within TTL
    status: str = "never_fetched"
    error: Optional[str] = None          # exception type + message, verbatim
    fetched_at: float = 0.0              # wall clock of the last SUCCESSFUL fetch
    attempted_at: float = 0.0            # wall clock of the last attempt, ok or not

    total_instruments: int = 0
    # Counters describing the snapshot currently being SERVED, so they can
    # never disagree with each other.
    valid_rows: int = 0
    # Rows in the most recent refresh ATTEMPT, whatever its outcome. Kept
    # separate from the served snapshot so a failed refresh cannot leave a
    # malformed count sitting next to another refresh's row total.
    last_attempt_rows: int = 0
    last_attempt_malformed: int = 0
    last_attempt_short_rows: int = 0
    last_attempt_numeric_rows: int = 0
    last_attempt_shape: list = field(default_factory=list)
    consecutive_empty: int = 0
    consecutive_failures: int = 0
    open_count: int = 0
    closed_count: int = 0
    malformed_count: int = 0
    payout_qualified_count: int = 0              # payout >= min_payout, any open state
    open_and_payout_qualified_count: int = 0     # both gates
    whitelist_count: int = 0                     # after the whitelist, if one is set
    min_payout_used: Optional[float] = None

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at) if self.fetched_at else float("inf")

    def to_dict(self) -> dict:
        age = self.age_seconds
        return {
            "instrument_fetch_success": self.status in ("ok", "empty"),
            "instrument_fetch_error": self.status == "error",
            "instrument_fetch_timestamp": self.fetched_at or None,
            "instrument_data_age_seconds": None if age == float("inf") else round(age, 1),
            "last_instrument_error": self.error,
            "status": self.status,
            "total_instruments": self.total_instruments,
            "valid_rows": self.valid_rows,
            "last_attempt_rows": self.last_attempt_rows,
            "last_attempt_malformed": self.last_attempt_malformed,
            "last_attempt_short_rows": self.last_attempt_short_rows,
            "last_attempt_numeric_rows": self.last_attempt_numeric_rows,
            "last_attempt_shape": self.last_attempt_shape,
            "consecutive_empty": self.consecutive_empty,
            "consecutive_failures": self.consecutive_failures,
            "serving_last_known_good": self.status == "stale",
            "open_count": self.open_count,
            "closed_count": self.closed_count,
            "malformed_count": self.malformed_count,
            "payout_qualified_count": self.payout_qualified_count,
            "open_and_payout_qualified_count": self.open_and_payout_qualified_count,
            "whitelist_count": self.whitelist_count,
            "min_payout_used": self.min_payout_used,
            "stale_ttl_seconds": ASSET_SNAPSHOT_STALE_TTL,
        }


class PyQuotexProvider(MarketProvider):
    name = "pyquotex"
    _CF_IMPERSONATE = "firefox133"
    _CF_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:127.0) "
        "Gecko/20100101 Firefox/127.0"
    )

    def __init__(self, email: str, password: str, is_demo: bool = True,
                 otp_callback=None, ssid: Optional[str] = None, user_agent: Optional[str] = None,
                 cookies: Optional[str] = None, sessions_dir: Optional[str] = None):
        super().__init__()
        self._email = email
        self._password = password
        self._is_demo = is_demo
        self._otp_callback = otp_callback
        self._ssid = ssid
        self._cookies = cookies
        self._user_agent = user_agent
        self._sessions_dir_override = sessions_dir
        self._client = None
        self._orders: Dict[str, dict] = {}
        self._connected = False
        self._candle_cache: Dict[str, Dict[float, dict]] = {}
        # key -> epoch time a deep seed last plateaued (gained almost
        # nothing). See SEED_PLATEAU_* above -- this is what stops a
        # broker-capped asset from re-running the full ~150s deep-seed
        # walk on every single scan cycle forever.
        self._seed_plateaued_at: Dict[str, float] = {}
        self._streamed: set = set()               # (asset, period) already subscribed
        # (asset, period) whose subscribe frame is in flight right now.
        # `streamed = stream_key in self._streamed` -> await -> `.add()` in
        # get_candles() is a check-then-act across an await point: two
        # coroutines for the same (asset, period) -- the scan loop and a
        # concurrent revalidation/execution for that asset -- both read
        # False and both send a subscribe frame. This set closes that
        # window without adding a lock to the hot path.
        self._subscribing: set = set()
        # LRU order of _candle_cache keys. The cache trims candles WITHIN a
        # key (cap 800) but never removed a KEY, so an asset that drops out
        # of the payout top-12 kept its full 800-candle cache forever. With
        # a broad asset list rotating over days that is tens of MB of dead
        # state. Bounded here instead.
        self._cache_lru: List[str] = []
        # Asset-feed diagnostics + the bounded stale fallback.
        self._asset_feed = AssetFeedSnapshot()
        self._last_good_assets: List[AssetInfo] = []
        # Serializes refreshes. Two concurrent get_assets() calls could
        # otherwise interleave: a slow EMPTY response landing after a
        # newer valid one would overwrite the good snapshot with nothing.
        # Uses the project's existing asyncio-lock style rather than new
        # infrastructure.
        self._asset_refresh_lock = asyncio.Lock()
        # CRITICAL FIX -- this was `Dict[str, int]` holding the LENGTH of the
        # broker's tick list at the last fold. pyquotex caps that list at
        # 1000 entries and evicts from the left (`price_list.pop(0)`,
        # api.py:684). Once an asset reaches 1000 ticks the length is pinned
        # at 1000 forever, so `cursor` stuck at 1000 and `ticks[cursor:]`
        # returned an empty slice on EVERY subsequent call: live tick
        # folding stopped permanently and silently. Proven in
        # backend/test_market_service.py::test_tick_folding_survives_buffer_eviction.
        # Now stores the TIMESTAMP of the last tick folded in, which is
        # immune to eviction.
        # Keyed by (asset, PERIOD), not by asset.
        #
        # Every timeframe folds the SAME broker tick list into its own
        # candle cache (_candle_cache is correctly keyed "asset:period"),
        # but the cursor was shared across them. _evaluate_asset_signal
        # reads tf, then htf, then hhtf for one asset in sequence, so the
        # first read advanced the cursor past every tick and the later
        # reads got nothing. Measured over 30 minutes of ticks:
        #     1m first -> 1m=31 buckets, 5m=1  (should be 6)
        #     5m first -> 5m=7  buckets, 1m=1  (should be 30)
        # Whichever timeframe read second was starved, so HTF/HHTF candles
        # were built almost entirely from the 45s resync and the history
        # seed with essentially no live-tick contribution -- and mtf_mode
        # "hard" gates entries on that HTF bias.
        self._stream_tick_ts: Dict[Tuple[str, int], Tuple[float, int]] = {}
        self._last_resync: Dict[str, float] = {}   # key -> last time we did a real history fetch
        self._tick_watcher_task: Optional[asyncio.Task] = None
        self._tournament_id: Optional[int] = None  # currently-active tournament, if any -- None means live/demo

    def get_session_snapshot(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Returns (ssid, cookies, user_agent) from the live client so the
        caller can persist it (e.g. SessionManager) for restart recovery.
        Safe to call whether or not a session was actually established."""
        if not self._client:
            return None, None, None
        data = getattr(self._client, "session_data", None) or {}
        return data.get("token"), data.get("cookies"), data.get("user_agent")

    def _build_client(self, Quotex, sessions_dir: Path):
        client = Quotex(
            email=self._email,
            password=self._password,
            root_path=str(sessions_dir),
            on_otp_callback=self._otp_callback,
        )
        if self._ssid and self._cookies:
            try:
                client.set_session(
                    user_agent=self._user_agent or "Quotex/1.0",
                    cookies=self._cookies,
                    ssid=self._ssid,
                )
                logger.info(
                    "Resuming stored session for %s: ssid_len=%d cookies_len=%d ua=%r",
                    self._email,
                    len(self._ssid),
                    len(self._cookies),
                    self._user_agent,
                )
            except Exception as exc:
                logger.warning("set_session() failed, falling back to fresh login: %s", exc)
        elif self._ssid and not self._cookies:
            logger.warning(
                "Have a stored ssid for %s but no matching cookies - skipping resume, "
                "doing a fresh login instead (this is the fix for authorization/reject "
                "on reconnect).",
                self._email,
            )
        client.set_account_mode("PRACTICE" if self._is_demo else "REAL")
        return client

    def _should_seed_after_failure(self, reason: object, exc: Exception | None = None) -> bool:
        text = " ".join(str(part) for part in (reason, exc) if part).lower()
        return any(
            marker in text
            for marker in (
                "403",
                "forbidden",
                "just a moment",
                "cloudflare",
                "captcha",
                "access page",
                "websocket connection rejected",
            )
        )

    async def _seed_session_via_curlcffi(self, sessions_dir: Path, lang: str) -> bool:
        try:
            from curl_cffi import requests
        except Exception as exc:
            logger.warning("curl_cffi fallback unavailable: %s", exc)
            return False

        def _extract_token(html: str) -> Optional[str]:
            match = re.search(
                r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
                html,
            )
            return match.group(1) if match else None

        def _extract_ssid(html: str) -> Optional[str]:
            match = re.search(r"window\.settings\s*=\s*(\{.*?\});", html, re.S)
            if not match:
                return None
            try:
                import json

                return json.loads(match.group(1)).get("token")
            except Exception:
                return None

        def _cookies_to_header(jar: dict[str, str]) -> str:
            return "; ".join(f"{k}={v}" for k, v in jar.items())

        def _warm_and_post(session) -> tuple[Any, Optional[str], Optional[str]]:
            base = "https://qxbroker.com"
            session.headers.update(
                {
                    "User-Agent": self._CF_USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.5",
                }
            )
            home = session.get(f"{base}/{lang}")
            if home.status_code != 200:
                return home, None, None

            modal = session.get(f"{base}/{lang}/sign-in/modal/")
            token = _extract_token(modal.text)
            if modal.status_code != 200 or not token:
                return modal, token, None

            login = session.post(
                f"{base}/{lang}/sign-in/",
                data={
                    "_token": token,
                    "email": self._email,
                    "password": self._password,
                    "remember": 1,
                },
                headers={
                    "Referer": f"{base}/{lang}/sign-in",
                    "Origin": base,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            return login, token, None

        session = requests.Session(impersonate=self._CF_IMPERSONATE)
        try:
            response, token, _ = await asyncio.to_thread(_warm_and_post, session)
            if response.status_code != 200:
                logger.warning("curl_cffi warm login failed with HTTP %s", response.status_code)
                return False

            if 'name="keep_code"' in response.text:
                if not self._otp_callback:
                    logger.warning("Quotex requested OTP during curl_cffi fallback but no callback is configured.")
                    return False
                prompt = "Enter the PIN code Quotex emailed you"
                if asyncio.iscoroutinefunction(self._otp_callback):
                    code = await self._otp_callback(prompt)
                else:
                    code = self._otp_callback(prompt)
                code = str(code or "").strip()
                if not code.isdigit():
                    logger.warning("Quotex OTP fallback received an invalid code.")
                    return False

                def _submit_otp() -> Any:
                    return session.post(
                        f"https://qxbroker.com/{lang}/sign-in/modal",
                        data={
                            "_token": token,
                            "email": self._email,
                            "password": self._password,
                            "remember": 1,
                            "keep_code": 1,
                            "code": code,
                        },
                        headers={
                            "Referer": f"https://qxbroker.com/{lang}/sign-in/modal",
                            "Origin": "https://qxbroker.com",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )

                response = await asyncio.to_thread(_submit_otp)

            if "/trade" not in str(response.url):
                response = await asyncio.to_thread(session.get, f"https://qxbroker.com/{lang}/trade")

            ssid = _extract_ssid(response.text)
            if not ssid:
                def _fetch_digest() -> Any:
                    return session.get(
                        "https://qxbroker.com/api/v1/cabinets/digest",
                        headers={"Referer": f"https://qxbroker.com/{lang}/trade"},
                    )

                digest = await asyncio.to_thread(_fetch_digest)
                if digest.status_code == 200:
                    try:
                        ssid = digest.json().get("data", {}).get("token")
                    except Exception:
                        ssid = None

            cookies = session.cookies.get_dict()
            if not ssid or not cookies:
                logger.warning("curl_cffi fallback could not extract a usable Quotex session.")
                return False

            session_data = {
                "cookies": _cookies_to_header(cookies),
                "token": ssid,
                "user_agent": self._CF_USER_AGENT,
            }

            session_path = sessions_dir / "session.json"

            def _persist_session() -> None:
                import json

                payload = {}
                if session_path.exists():
                    try:
                        payload = json.loads(session_path.read_text())
                    except Exception:
                        payload = {}
                payload[self._email] = session_data
                session_path.write_text(json.dumps(payload, indent=4))

            await asyncio.to_thread(_persist_session)
            self._ssid = ssid
            self._cookies = session_data["cookies"]
            self._user_agent = session_data["user_agent"]
            logger.info("Seeded fresh Quotex session via curl_cffi for %s", self._email)
            return True
        finally:
            await asyncio.to_thread(session.close)

    async def connect(self) -> bool:
        # RCA F1/F2: this used to be
        #     vendor = Path(__file__).resolve().parents[3] / "vendor" / "pyquotex"
        #     sys.path.insert(0, str(vendor))
        # which (a) pointed at an EMPTY directory in a fresh clone, because
        # `vendor/pyquotex/` is untracked -- git cannot store an empty folder --
        # and (b) resolved outside the container's build context, where this
        # file is /app/backend/app/services/market.py. Either way the import
        # below raised ModuleNotFoundError and every real broker connection
        # silently died. The resolver searches the vendor tree (and honours
        # $PYQUOTEX_VENDOR_PATH for container layouts) instead of guessing.
        from ..pyquotex_vendor import ensure_pyquotex_on_path
        resolved = ensure_pyquotex_on_path()
        if resolved is None:
            raise RuntimeError(
                "Could not locate the vendored pyquotex library. Looked for "
                "pyquotex/stable_api.py under $PYQUOTEX_VENDOR_PATH and every "
                "<ancestor>/vendor/ directory. Real Quotex connectivity is "
                "unavailable until the library is present -- see "
                "vendor/README.md."
            )
        from pyquotex.stable_api import Quotex  # type: ignore
        from pyquotex import config as _pyquotex_config  # type: ignore

        if self._sessions_dir_override:
            sessions_dir = Path(self._sessions_dir_override)
        else:
            # Legacy shared path — only used when no per-user dir is supplied
            # (e.g. running without auth). Multi-user callers always pass
            # `sessions_dir` so each user's pyquotex cache is isolated.
            sessions_dir = Path(__file__).resolve().parents[2] / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # BUG WORKAROUND: pyquotex's own load_session()/update_session() in
        # config.py always read/write "session.json" relative to a *module
        # global* (config.base_dir = Path.cwd() at import time) — the
        # `root_path` constructor arg below is NOT used for this file (it's
        # only used for a couple of other cached resources). Left alone,
        # every user of this app would silently share ONE session.json file
        # at the process's working directory, accumulating stale tokens
        # across every login attempt ever made (this is exactly what caused
        # "authorization/reject" until the file was deleted by hand). Setting
        # the module global here — synchronously, immediately before the
        # synchronous Quotex(...) constructor call that consumes it, so
        # there's no `await` in between for another user's connect() to
        # interleave — redirects it to this user's own isolated folder.
        _pyquotex_config.base_dir = sessions_dir
        self._client = self._build_client(Quotex, sessions_dir)
        # BUG FIX (stale subscriptions across reconnect): connect() rebuilds
        # a brand-new pyquotex client above on *every* call, including
        # reconnects -- but _streamed/_stream_tick_ts/_last_resync are
        # instance state that used to only get cleared in disconnect(). If a
        # reconnect ever happened without an explicit disconnect() first
        # (e.g. _tick_watcher noticing a frozen stream and flipping
        # _connected=False directly, then _reconnect_with_backoff calling
        # connect() again), get_candles()'s `stream_key in self._streamed`
        # check would still say "already subscribed" for a stream that was
        # only ever registered on the *old*, now-discarded client -- so
        # start_candles_stream() would never get called again on the new
        # one, and that asset's ticks (and the new_data_event wakeups they
        # drive) would silently stop forever. A new client has no
        # subscriptions regardless of what the old one had, so this always
        # resets to match -- get_candles() will simply re-subscribe on
        # everyone's next call, cheaply. _candle_cache is left alone: it's
        # just historical OHLC data, doesn't reference the old client, and
        # keeping it means charts don't blank out across a reconnect.
        self._streamed.clear()
        self._stream_tick_ts.clear()
        self._subscribing.clear()
        self._last_resync.clear()
        logger.info("Connecting to Quotex for %s (demo=%s, sessions_dir=%s)",
                    self._email, self._is_demo, sessions_dir)
        connect_exc: Exception | None = None
        try:
            ok, reason = await self._client.connect()
        except Exception as exc:
            ok, reason, connect_exc = False, str(exc), exc

        if not ok and self._should_seed_after_failure(reason, connect_exc):
            logger.warning("Primary Quotex login path was blocked; attempting curl_cffi fallback.")
            try:
                await self._client.close()
            except Exception:
                pass
            if await self._seed_session_via_curlcffi(sessions_dir, lang="en"):
                self._client = self._build_client(Quotex, sessions_dir)
                try:
                    ok, reason = await self._client.connect()
                except Exception as exc:
                    ok, reason = False, str(exc)

        if not ok:
            # pyquotex's own post-login check only waits ~2s for the
            # websocket's "authorized" confirmation before giving up — on a
            # slower connection (or a slightly delayed server-side ack) that
            # can produce a false "connection rejected" even though the
            # login itself succeeded and authorization arrives moments
            # later. Re-poll a few more times before accepting the failure;
            # each call re-checks current state, it doesn't repeat login/OTP.
            for _ in range(5):
                await asyncio.sleep(1.5)
                try:
                    if await self._client.check_connect():
                        ok, reason = True, "Authorized after retry"
                        break
                except Exception:
                    break
        self._connected = bool(ok)
        sd = getattr(self._client, "session_data", None) or {}
        logger.info(
            "Quotex connect result for %s: ok=%s reason=%r auth_status=%s | "
            "post-login session snapshot: ssid_present=%s ssid_len=%s cookies_present=%s "
            "cookies_len=%s user_agent=%r",
            self._email, ok, reason,
            getattr(getattr(getattr(self._client, "api", None), "state", None), "auth_status", "unknown"),
            bool(sd.get("token")), len(sd.get("token") or ""),
            bool(sd.get("cookies")), len(sd.get("cookies") or ""),
            sd.get("user_agent"),
        )
        if not ok:
            logger.warning("Quotex connect failed for this user: %s", reason)
        if ok:
            try:
                await self._client.get_instruments()
            except Exception:
                pass
            if self._tick_watcher_task is None or self._tick_watcher_task.done():
                self._tick_watcher_task = asyncio.create_task(self._tick_watcher())
        return self._connected

    #: Maximum number of (asset, period) candle caches held at once. Well
    #: above the ~36 a normal scan touches (12 assets x 3 timeframes), so
    #: eviction only ever affects assets that genuinely stopped being
    #: scanned.
    CACHE_MAX_KEYS = 120

    def _touch_cache_key(self, key: str) -> None:
        """Mark a cache key as recently used and evict the coldest ones.

        Cheap: one list remove/append plus, only when over the cap, a
        bounded number of pops.
        """
        try:
            self._cache_lru.remove(key)
        except ValueError:
            pass
        self._cache_lru.append(key)
        while len(self._cache_lru) > self.CACHE_MAX_KEYS:
            cold = self._cache_lru.pop(0)
            self._candle_cache.pop(cold, None)
            self._last_resync.pop(cold, None)

    def _new_ticks(self, asset: str, period: int, ticks: List[dict]) -> List[dict]:
        """Return the ticks not yet folded in for `asset`.

        Cursor is a (last_ts, last_idx_at_ts) pair for THIS (asset, period),
        NOT a list index or length -- the broker's buffer is a bounded
        1000-entry list that evicts from the left, so any position-based
        cursor becomes wrong the moment it saturates, and a cursor shared
        across timeframes starves whichever timeframe reads second.

        Strict `>` on the timestamp plus a position counter WITHIN the
        boundary timestamp means:
          * same-second ticks that arrive AFTER the previous fold are
            still picked up (no silent drop -- same-timestamp ticks are
            real and move high/low/close);
          * but the exact ticks we already folded are NOT refolded on the
            next call, which would double-count the tick-count volume
            proxy and make every candle's volume grow without bound.
        `>=` alone was the bug: it was idempotent for OHLC (max/min/last-
        write-wins) but NOT for additive volume, which is exactly what
        test_refolding_the_boundary_tick_is_idempotent caught.
        """
        last = self._stream_tick_ts.get((asset, period))
        if last is None:
            return ticks
        last_ts, last_idx = last
        out = []
        for i, t in enumerate(ticks):
            try:
                tt = float(t["time"])
                if tt > last_ts or (tt == last_ts and i > last_idx):
                    out.append(t)
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _advance_tick_cursor(self, asset: str, period: int, ticks: List[dict],
                             consumed: List[dict]) -> None:
        """Move the cursor past the newest tick actually folded in."""
        if not consumed:
            return
        # Position within the FULL ticks snapshot (consumed is a suffix).
        # Because consumed is built by iterating ticks in order, the last
        # element of consumed sits at index len(ticks) - (len(consumed) -
        # offset_from_end). Easier: scan backwards from the end for the
        # same dict identity or the same timestamp+price combo.
        last_t = consumed[-1]
        try:
            last_ts = float(last_t["time"])
        except (KeyError, TypeError, ValueError):
            return
        last_idx = -1
        for i in range(len(ticks) - 1, -1, -1):
            try:
                if float(ticks[i]["time"]) == last_ts:
                    last_idx = i
                    break
            except (KeyError, TypeError, ValueError):
                continue
        if last_idx >= 0:
            self._stream_tick_ts[(asset, period)] = (last_ts, last_idx)

    async def _tick_watcher(self) -> None:
        """pyquotex already streams ticks over its own websocket into
        `get_realtime_price()`'s in-memory buffer; nothing here used to
        actually look at that buffer until the next scan called
        get_candles(). This tight loop watches the buffer directly so the
        orchestrator's scan loop wakes up the moment a new tick lands,
        instead of waiting out a fixed poll interval.

        It also catches a *frozen* stream: pyquotex's websocket object can
        stay "connected" at the TCP level while silently stopping frame
        delivery (no exception raised anywhere) — candles just go stale
        forever with nothing visibly wrong. If a streamed asset goes
        STALL_THRESHOLD seconds with zero tick growth, that's treated as a
        hang and forces a reconnect instead of trading on stale data."""
        STALL_THRESHOLD = 45.0
        # Was `Dict[str, int]` compared against len(ticks). Once the
        # broker's bounded buffer saturated at 1000, len() never changed
        # again, so `any_growth` was False forever and this loop declared
        # a perfectly healthy stream "stalled" every 45s -- forcing an
        # endless reconnect cycle that cleared every subscription and
        # re-seeded every asset. Comparing the newest tick TIMESTAMP is
        # immune to eviction.
        watch_ts: Dict[str, float] = {}
        last_growth_ts = time.time()
        while self._connected:
            try:
                any_growth = False
                for asset, _period in list(self._streamed):
                    try:
                        ticks = await self._client.get_realtime_price(asset)
                    except Exception as exc:
                        # BUG FIX: Log tick fetch failures for debugging
                        logger.debug("Failed to fetch realtime ticks for %s: %s", asset, type(exc).__name__)
                        continue
                    if not ticks:
                        continue
                    try:
                        newest = float(ticks[-1]["time"])
                    except (KeyError, TypeError, ValueError, IndexError):
                        continue
                    if newest > watch_ts.get(asset, 0.0):
                        watch_ts[asset] = newest
                        self.new_data_event.set()
                        any_growth = True
                if any_growth:
                    last_growth_ts = time.time()
                elif self._streamed and (time.time() - last_growth_ts) > STALL_THRESHOLD:
                    log_event(logger, logging.WARNING, "LIVE_TIMEOUT",
                              stalled_seconds=round(time.time() - last_growth_ts, 1),
                              threshold=STALL_THRESHOLD,
                              streamed_assets=len(self._streamed),
                              action="forcing reconnect")
                    self._connected = False
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Same principle as everywhere else in this audit: this
                # loop is what notices a frozen stream and forces the
                # reconnect that recovers from it, so it must not be able
                # to die silently from something unrelated to that.
                logger.error("[tick_watcher] iteration failed, continuing: %s", exc, exc_info=True)
            await asyncio.sleep(0.25)

    async def disconnect(self) -> None:
        if self._tick_watcher_task:
            self._tick_watcher_task.cancel()
            try:
                await self._tick_watcher_task
            except (asyncio.CancelledError, Exception):
                pass
            self._tick_watcher_task = None
        if self._client:
            for asset, period in list(self._streamed):
                try:
                    # Bounded: disconnect() is reached from the scan loop's
                    # error path, and unsubscribing sends frames on the very
                    # socket we already suspect is dead. Unbounded here meant
                    # the recovery path itself could hang.
                    await asyncio.wait_for(
                        self._client.stop_candles_stream(asset),
                        timeout=PROVIDER_DISCONNECT_TIMEOUT,
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(
                    self._client.close(), timeout=PROVIDER_DISCONNECT_TIMEOUT
                )
            except Exception:
                pass
        self._streamed.clear()
        self._stream_tick_ts.clear()
        self._subscribing.clear()
        self._last_resync.clear()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def supports_tournaments(self) -> bool:
        # Verified against vendor/pyquotex/pyquotex/_api/account.py:
        # change_account(balance_mode, tournament_id) genuinely accepts a
        # tournament_id and every subsequent order/balance call routes
        # into it (buy.py auto-attaches self.api.tournament_id). This is
        # a real capability, not an assumption.
        return True

    async def switch_account(self, account_mode: str, tournament_id: Optional[int] = None) -> bool:
        """Switches the live connection's active account. Tournament mode
        is layered on top of PRACTICE at the wire protocol (pyquotex has
        no separate tournament account type -- see supports_tournaments()
        above for the audit trail). Uses change_account() rather than
        set_account_mode() because the latter only sets a local flag used
        before connect(); change_account() actually sends the live
        websocket message to switch an already-connected session, which
        is what account-mode switching from a running orchestrator needs."""
        if not self._client:
            return False
        try:
            if account_mode == "live":
                await self._client.change_account("REAL", tournament_id=0)
                self._is_demo = False
                self._tournament_id = None
            elif account_mode == "tournament" and tournament_id:
                await self._client.change_account("PRACTICE", tournament_id=int(tournament_id))
                self._is_demo = True
                self._tournament_id = int(tournament_id)
            else:  # "demo", or "tournament" requested without a valid id -- falls back to plain demo
                await self._client.change_account("PRACTICE", tournament_id=0)
                self._is_demo = True
                self._tournament_id = None
            return True
        except Exception as exc:
            logger.warning("switch_account(%s, tournament_id=%s) failed: %s", account_mode, tournament_id, exc)
            return False

    async def get_balance(self) -> Tuple[float, str]:
        bal = await self._client.get_balance()
        return float(bal or 0.0), "USD"

    def get_asset_feed_status(self) -> dict:
        """Diagnostics for the asset universe. Read-only, no I/O."""
        return self._asset_feed.to_dict()

    async def get_realtime_sentiment(self, asset: str) -> Optional[dict]:
        """Raw trader-sentiment payload from the vendor's shared state.

        Reads `api.realtime_sentiment[asset]`, which the vendor populates from
        the broker's "sentiment" control frame (`api.py:363-369` handler,
        registered at `api.py:171`). Non-blocking: this is a dict lookup on
        state the websocket already wrote, so there is no network call and no
        subscription to start from here.

        Returns None -- not {} -- whenever sentiment is unavailable, so the
        caller can tell "no sentiment" from "sentiment of zero". A missing or
        disconnected client is not an error worth raising into the scan loop.
        """
        client = getattr(self, "_client", None)
        if client is None or not self.connected():
            return None
        try:
            payload = await asyncio.wait_for(
                client.get_realtime_sentiment(asset), timeout=PROVIDER_IO_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(logger, logging.DEBUG, "SENTIMENT_UNAVAILABLE",
                      asset=asset, error=type(exc).__name__)
            return None
        if not isinstance(payload, dict) or not payload:
            return None
        return payload

    async def get_realtime_ticks(self, asset: str) -> List[dict]:
        """Raw price updates from the vendor's bounded tick buffer.

        These are price updates, NOT traded volume: the tick frame is
        `[asset, ts, price, direction]` (`api.py:790`) and carries no volume
        field at all. Callers may count them as activity; none may present the
        count as financial volume.
        """
        client = getattr(self, "_client", None)
        if client is None or not self.connected():
            return []
        try:
            ticks = await asyncio.wait_for(
                client.get_realtime_price(asset), timeout=PROVIDER_IO_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return []
        return list(ticks or [])

    async def get_assets(self) -> List[AssetInfo]:
        """Current tradable universe.

        Return type and signature are unchanged, so every existing caller
        keeps working. What changed is the FAILURE CONTRACT: this used to
        be

            try:
                instruments = await self._client.get_instruments()
            except Exception:
                instruments = []

        which made five completely different situations indistinguishable
        -- broker timeout, disconnected socket, malformed payload, zero
        instruments, and a genuinely closed market all produced an empty
        list and the message "0 open assets". Because get_assets() could
        then never raise, the `except` branch on /api/status's assets
        check was unreachable dead code, so a real asset-feed outage was
        reported to the user as a quiet WARN.

        Now every outcome is recorded on self._asset_feed (see
        get_asset_feed_status()), and a fetch failure falls back to the
        last known-good snapshot for at most ASSET_SNAPSHOT_STALE_TTL
        seconds so a single blip does not empty the scanner. Past that
        window the snapshot is dropped and the feed reports ERROR --
        `is_open` is time-sensitive and acting on an old open-flag is not
        safe. No asset is ever invented.
        """
        async with self._asset_refresh_lock:
            return await self._refresh_assets()

    async def _refresh_assets(self) -> List[AssetInfo]:
        snap = self._asset_feed
        snap.attempted_at = time.time()
        snap.min_payout_used = None

        try:
            instruments = await asyncio.wait_for(
                self._client.get_instruments(), timeout=PROVIDER_IO_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            snap.consecutive_failures += 1
            snap.last_attempt_rows = 0
            snap.last_attempt_malformed = 0
            return self._serve_degraded(f"{type(exc).__name__}: {exc}", "fetch failed")

        # Shape guard. `for i in <dict>` iterates KEYS, and `len(<dict>)`
        # is the key count, so a dict arriving here was silently reported
        # as "N rows, all malformed" -- the production "4/4 instrument
        # rows failed validation" against a 4-key dict. The vendor-side
        # cause is fixed in api.py::_h_instruments_list, but this boundary
        # must not turn a wrong-shaped payload into a universe wipe either.
        # Treated as an unusable refresh: the last known-good snapshot is
        # preserved and we retry, exactly as for a failure.
        if instruments is None:
            instruments = []
        elif not isinstance(instruments, (list, tuple)):
            snap.consecutive_failures += 1
            snap.last_attempt_rows = 0
            snap.last_attempt_malformed = 0
            detail = (f"dict with keys {sorted(instruments)[:8]}"
                      if isinstance(instruments, dict) else repr(instruments)[:80])
            return self._serve_degraded(
                f"MalformedInstrumentPayload: expected a list of rows, got "
                f"{type(instruments).__name__} ({detail})",
                "instrument payload was not a list of rows",
            )

        out: List[AssetInfo] = []
        malformed = 0
        short_rows = 0
        numeric_rows = 0
        # Redacted shape samples from THIS attempt, so a payload that keeps
        # failing can be identified from /api/asset-universe without
        # needing to reproduce it or add ad-hoc logging.
        shape_samples: List[str] = []
        for i in instruments:
            try:
                # Index positions are NOT guessed -- they are confirmed
                # against three independent places in the vendor itself:
                #   i[1]  symbol   _api/assets.py:69,113,130; cli/market.py:42
                #   i[2]  name     _api/assets.py:69,116;     cli/market.py:42
                #   i[14] is_open  _api/assets.py:116,148; types.py:173;
                #                  cli/market.py:40
                #   i[5]  payout   _api/assets.py:143;        cli/market.py:41
                #   i[-9] 1M payout _api/assets.py:145
                # They are deliberately left unchanged. What is added is the
                # length guard types.py:173 already had and this parser did
                # not: a short row raised IndexError, was swallowed by the
                # per-instrument except below, and the instrument vanished
                # with only a DEBUG line. A schema change shortening rows
                # would therefore have emptied the whole universe silently.
                if not isinstance(i, (list, tuple)) or len(i) < 2:
                    malformed += 1
                    if len(shape_samples) < 3:
                        shape_samples.append(_describe_row(i))
                    continue
                # POSITIVE instrument-row check, not merely "long enough".
                #
                # Relaxing the length guard to match the vendor made this
                # necessary: a candle row is `[time, open, close, high, low]`
                # -- length 5, so it now passes a pure length test -- and
                # would be turned into an asset whose symbol is str(open
                # price). A misrouted history payload must never become a
                # tradeable instrument.
                #
                # An instrument row carries a STRING symbol at index 1
                # (_api/assets.py:69,113,130); a candle row carries a
                # number there. That is the discriminator, and it invents
                # no new index.
                if isinstance(i[1], (int, float)) and not isinstance(i[1], bool):
                    malformed += 1
                    numeric_rows += 1
                    if len(shape_samples) < 3:
                        shape_samples.append(
                            f"{_describe_row(i)} — numeric at index 1 "
                            f"(candle/quote row, not an instrument)")
                    continue
                # ROW-LENGTH TOLERANCE -- aligned with the vendor's own
                # contract at pyquotex/types.py:173:
                #     is_open=bool(row[14]) if len(row) > 14 else False
                #
                # This parser previously required len(i) >= 15 and REJECTED
                # anything shorter. That is stricter than the vendor, and it
                # is a plausible cause of the production report
                # "4/4 instrument rows failed validation" against a
                # non-empty broker response: four genuine but short rows
                # would be dropped outright, leaving zero assets.
                #
                # Rejecting is not the safe direction here. A row we cannot
                # read index 14 from is treated as CLOSED, exactly as the
                # vendor does -- and a closed asset is filtered out by
                # _eligible_assets(), so it can never become tradeable. That
                # keeps the asset visible in diagnostics instead of making
                # the whole universe vanish.
                if len(i) <= 14 and len(shape_samples) < 3:
                    shape_samples.append(_describe_row(i))
                symbol = str(i[1])
                name = str(i[2]).replace("\n", "") if len(i) > 2 else str(i[1])
                is_open = bool(i[14]) if len(i) > 14 else False
                short_rows += 1 if len(i) <= 14 else 0
                payout = 0.0
                try:
                    payout = float(i[-9] or 0) if len(i) >= 9 else 0.0
                except (IndexError, ValueError, TypeError):
                    payout = 0.0
                if payout <= 0:
                    try:
                        payout = float(i[5] or 0) if len(i) > 5 else 0.0
                    except (IndexError, ValueError, TypeError):
                        payout = 0.0
                out.append(AssetInfo(symbol=symbol, name=name, payout=round(payout, 1),
                                     is_open=is_open, is_otc="_otc" in symbol))
            except (IndexError, ValueError, TypeError, AttributeError) as exc:
                malformed += 1
                if len(shape_samples) < 3:
                    shape_samples.append(f"{_describe_row(i)} raised {type(exc).__name__}")
                logger.debug("Failed to parse instrument %s: %s", type(exc).__name__, i)
                continue

        # Record what THIS attempt saw, regardless of what we end up
        # serving. These two always describe the same refresh, so the
        # ratio between them can never be nonsensical.
        snap.last_attempt_rows = len(instruments)
        snap.last_attempt_malformed = malformed
        snap.last_attempt_short_rows = short_rows
        snap.last_attempt_numeric_rows = numeric_rows
        snap.last_attempt_shape = shape_samples

        if malformed:
            log_event(
                logger, logging.WARNING, "INSTRUMENTS_MALFORMED",
                malformed=malformed, total=len(instruments), parsed=len(out),
            )

        if not out:
            # ROOT CAUSE OF THE REPORTED BUG. This used to fall straight
            # through to `self._last_good_assets = list(out)` and wipe a
            # perfectly good 57-asset universe with an empty list.
            #
            # A zero-row response is NOT proof that the market is empty.
            # _api/assets.py::get_instruments() returns [] on at least
            # four non-exception paths -- not connected (line 27), the
            # `self.api.instruments or []` fallthrough (line 53), and a
            # TimeoutError it swallows itself (line 58) -- plus a
            # reconnect race where api.instruments has not been
            # repopulated yet. None of those raise, so the existing
            # stale-fallback (which only covered the exception path)
            # never engaged, and the empty snapshot became the new truth.
            snap.consecutive_empty += 1
            reason = ("every instrument row failed validation"
                      if instruments else "broker returned zero instrument rows")
            if instruments:
                log_event(
                    logger, logging.WARNING, "INSTRUMENTS_ALL_UNPARSEABLE",
                    total=len(instruments),
                    reason="every instrument row failed validation -- schema may have changed",
                )
            return self._serve_degraded(None, reason, empty_response=True)

        # Valid, non-empty snapshot: this is the only path that may
        # replace the known-good universe.
        snap.error = None
        snap.fetched_at = time.time()
        snap.total_instruments = len(instruments)
        snap.valid_rows = len(out)
        snap.malformed_count = malformed
        snap.open_count = sum(1 for a in out if a.is_open)
        snap.closed_count = len(out) - snap.open_count
        snap.status = "ok"
        snap.consecutive_empty = 0
        snap.consecutive_failures = 0
        self._last_good_assets = list(out)
        return out

    def _serve_degraded(
        self, error: Optional[str], reason: str, empty_response: bool = False
    ) -> List[AssetInfo]:
        """Single exit for every unusable refresh -- failed OR empty.

        Preserves the last known-good snapshot while it is within
        ASSET_SNAPSHOT_STALE_TTL, so one transient bad refresh cannot
        destroy a valid universe or kill the scanner's current
        candidates. Past the TTL the snapshot is dropped and the feed
        reports ERROR: `is_open` is time-sensitive and acting on an old
        open-flag is not safe.

        Never fabricates an asset. If there has never been a good
        snapshot, this returns [] and the state stays degraded.
        """
        snap = self._asset_feed
        if error is not None:
            snap.error = error
        age = snap.age_seconds

        if self._last_good_assets and age <= ASSET_SNAPSHOT_STALE_TTL:
            snap.status = "stale"
            log_event(
                logger, logging.WARNING, "ASSET_FEED_DEGRADED",
                reason=reason, error=snap.error, age_seconds=round(age, 1),
                serving="last known-good snapshot",
                assets=len(self._last_good_assets),
                consecutive_empty=snap.consecutive_empty,
                consecutive_failures=snap.consecutive_failures,
            )
            return list(self._last_good_assets)

        # No usable snapshot. Reset the SERVED counters together so they
        # stay mutually consistent -- leaving malformed_count behind
        # while zeroing total_instruments is what produced the impossible
        # "4/0 instrument rows failed validation".
        snap.status = "empty" if (empty_response and error is None) else "error"
        snap.total_instruments = 0
        snap.valid_rows = 0
        snap.malformed_count = 0
        snap.open_count = snap.closed_count = 0
        snap.payout_qualified_count = 0
        snap.open_and_payout_qualified_count = 0
        snap.whitelist_count = 0
        self._last_good_assets = []
        log_event(
            logger, logging.ERROR, "ASSET_FEED_UNAVAILABLE",
            reason=reason, error=snap.error,
            age_seconds=None if age == float("inf") else round(age, 1),
            stale_ttl=ASSET_SNAPSHOT_STALE_TTL,
        )
        return []

    def seed_plateau_status(self) -> dict:
        """How many (asset, timeframe) keys are currently accepted as
        broker-capped, so this shows up somewhere other than the debug log."""
        now = time.time()
        active = {k: round(now - t, 0) for k, t in self._seed_plateaued_at.items()}
        return {"plateaued_count": len(active), "sample": list(active.items())[:5]}

    async def get_candles(self, asset: str, timeframe: str, count: int = 120) -> List[Candle]:
        """Live-streamed path: subscribe once per (asset, period), then fold
        in ticks pyquotex's websocket has already pushed into shared memory
        (`realtime_price`) — zero extra network requests per call. A real
        history request only happens to seed a new subscription and, after
        that, as an infrequent resync safety net (RESYNC_INTERVAL) rather
        than on every scan like before."""
        period = TIMEFRAMES.get(timeframe, 60)
        key = f"{asset}:{period}"
        cache = self._candle_cache.setdefault(key, {})
        self._touch_cache_key(key)
        now = time.time()

        stream_key = (asset, period)
        streamed = stream_key in self._streamed
        if not streamed and stream_key in self._subscribing:
            # Another coroutine is already subscribing this exact stream.
            # Don't send a second frame; this call proceeds on the
            # history/resync path for now and picks up the live stream on
            # its next pass, which is a cycle away at most.
            log_event(logger, logging.DEBUG, "SUBSCRIPTION_IN_FLIGHT",
                      asset=asset, period=period)
        elif not streamed and self._client:
            self._subscribing.add(stream_key)
            try:
                # ROOT-CAUSE FIX (secondary layer): this was a bare await.
                # It sends a subscribe frame, so on a half-open socket it
                # blocked here forever -- inside _evaluate_asset_signal,
                # inside _scan_once's gather(), which meant the whole scan
                # cycle never completed. A subscribe that takes >20s is a
                # dead transport, not a busy broker.
                await asyncio.wait_for(
                    self._client.start_candles_stream(asset, period),
                    timeout=PROVIDER_IO_TIMEOUT,
                )
                self._streamed.add(stream_key)
                streamed = True
                log_event(logger, logging.INFO, "LIVE_SUBSCRIBED",
                          asset=asset, period=period)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                streamed = False
                log_event(logger, logging.WARNING, "SUBSCRIPTION_FAILED",
                          asset=asset, period=period, error=type(exc).__name__)
            finally:
                self._subscribing.discard(stream_key)

        RESYNC_INTERVAL = 45.0 if streamed else 3.0  # streamed: rare drift-correction; else: old pull cadence
        # BUG FIX (2026-08): this was `len(cache) < min(count, 60)`.
        #
        # The `min(..., 60)` capped seeding at 60 candles no matter what the
        # caller asked for. Once an asset's cache reached 60, seed_needed went
        # False permanently and history was never requested again -- the cache
        # could only grow from live ticks, one candle per period. So a request
        # for 300 candles returned ~60, and on a 15m timeframe reaching even
        # 200 would have taken 35 hours of uninterrupted uptime.
        #
        # This is why the old 55-bar warm floor always passed and nothing ever
        # looked broken: 60 was both the ceiling AND just above the floor.
        # Every signal this system has ever produced ran on ~60 bars.
        seed_needed = len(cache) < count
        # BUG FIX (2026-08): `seed_needed` alone does not know the broker has
        # a hard ceiling on how much history it will serve. When
        # trading.history_fetch_bars (the target here) is set above what the
        # broker actually delivers -- exactly the case after the 200-candle
        # gate, once the broker turned out to cap at ~120 -- `len(cache)`
        # plateaus below `count` PERMANENTLY, so `seed_needed` stayed True on
        # every single call forever. Each call to get_candles() for that asset
        # then re-ran the full ~150s bounded deep-seed walk, got roughly the
        # same ~120 candles back, gained nothing, and repeated next cycle.
        #
        # This is invisible at first: with a fresh cache everything grows
        # normally scan to scan. It only bites once assets approach the
        # broker's real ceiling -- which is exactly "starts fine, then more
        # and more cycles quietly stop advancing" as more assets cross that
        # line and each eats a ~150s dead-end every time it is scanned.
        #
        # Fix: once a full attempt gains almost nothing, accept the plateau
        # for a cooldown period. The cache keeps growing slowly from live
        # ticks / the light resync pull in the meantime; a full re-attempt
        # happens periodically in case the broker's limit changes.
        plateaued_at = self._seed_plateaued_at.get(key)
        if seed_needed and plateaued_at is not None:
            if now - plateaued_at < SEED_PLATEAU_COOLDOWN_SECONDS:
                seed_needed = False
            else:
                self._seed_plateaued_at.pop(key, None)  # cooldown elapsed, try again
        resync_due = now - self._last_resync.get(key, 0) > RESYNC_INTERVAL
        if seed_needed or resync_due:
            if seed_needed:
                pre_len = len(cache)
                log_event(logger, logging.INFO, "HISTORY_REQUEST",
                          asset=asset, period=period, want=max(count, 60),
                          have=len(cache))
                try:
                    # Bounded: this walks history in chunks and is the
                    # slowest thing on the scan path. Note it re-runs on
                    # EVERY scan for any asset whose cache never reaches
                    # 60 candles (seed_needed above), so an unbounded
                    # version here stalls the cycle repeatedly, not once.
                    # Ask for a bit more than the shortfall so boundary
                    # rounding and gaps don't leave us permanently one candle
                    # short and re-seeding every single cycle.
                    want = max(count, 60)
                    span = min(period * (want + 20), MAX_HISTORY_SPAN_SECONDS)
                    hist = await asyncio.wait_for(
                        self._client.get_historical_candles(
                            asset, amount_of_seconds=span,
                            period=period, max_workers=3,
                        ),
                        # Scales with how much is actually being walked: the
                        # old flat 45s was sized for a ~60-candle pull and
                        # simply could not complete a 300-candle one, so every
                        # deep seed died on timeout and the asset never
                        # reached READY.
                        timeout=min(PROVIDER_SEED_TIMEOUT * max(1.0, want / 60.0),
                                    PROVIDER_SEED_TIMEOUT_MAX),
                    )
                    stored = 0
                    for c in hist or []:
                        if isinstance(c, dict) and "time" in c:
                            # Binary (Quotex/pyquotex) has NO real exchange volume.
                            # The broker only returns a per-candle TICK count, which
                            # looks like real volume if we pass it through verbatim:
                            # it varies, has nunique()>1, and passes the nunique/sum
                            # heuristic in indicators._has_real_volume, which then
                            # feeds it into volume_ma / volume_strength as if it were
                            # order-flow data. Tick count correlates with activity
                            # but is NOT volume -- applying volume-based confidence
                            # penalties/bonuses to it is mathematically wrong.
                            #
                            # We tag the tick-count proxy explicitly so
                            # _has_real_volume can distinguish "real exchange volume"
                            # (Binance/Bybit) from "synthetic tick-count activity
                            # measure" (pyquotex). The numeric value stays as the
                            # tick count so the frontend still sees a meaningful
                            # activity number in the volume field.
                            c_real_vol = False
                            try:
                                raw_v = c.get("volume")
                                if raw_v is None or raw_v == "" or float(raw_v) == 0.0:
                                    c_real_vol = False
                                else:
                                    c_real_vol = True
                            except (TypeError, ValueError):
                                c_real_vol = False
                            if not c_real_vol:
                                # RCA C.2: a tick count is NOT a traded
                                # volume, so it no longer gets copied into
                                # this field. `volume` stays 0 when the
                                # broker published no real volume; the count
                                # remains available under "ticks" and is
                                # surfaced through the tick-activity
                                # features instead.
                                c["volume"] = 0.0
                            c["_volume_source"] = "real" if c_real_vol else "synthetic"
                            cache[float(c["time"])] = c
                            stored += 1
                    log_event(logger, logging.INFO, "HISTORY_STORED",
                              asset=asset, period=period, stored=stored,
                              cache_size=len(cache))
                    gained = len(cache) - pre_len
                    if gained < SEED_PLATEAU_MIN_GAIN and len(cache) < count:
                        self._seed_plateaued_at[key] = now
                        log_event(
                            logger, logging.INFO, "HISTORY_PLATEAU",
                            asset=asset, period=period, have=len(cache), want=count,
                            gained=gained,
                            note=(f"broker delivered {len(cache)}/{count} and gave only "
                                  f"{gained} new candles -- treating as the broker's real "
                                  f"ceiling for {SEED_PLATEAU_COOLDOWN_SECONDS/60:.0f} min "
                                  f"instead of re-walking history every cycle"),
                        )
                    else:
                        self._seed_plateaued_at.pop(key, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event(logger, logging.WARNING, "HISTORY_FAILED",
                              asset=asset, period=period, error=type(exc).__name__)
            try:
                raw = await asyncio.wait_for(
                    self._client.get_candles(asset, time.time(), period * 6, period),
                    timeout=PROVIDER_IO_TIMEOUT,
                )
                for c in raw or []:
                    if isinstance(c, dict) and "time" in c:
                        c_real_vol = False
                        try:
                            raw_v = c.get("volume")
                            if raw_v is not None and raw_v != "" and float(raw_v) != 0.0:
                                c_real_vol = True
                        except (TypeError, ValueError):
                            c_real_vol = False
                        if not c_real_vol:
                            # RCA C.2: tick count is activity, not volume.
                            c["volume"] = 0.0
                        c["_volume_source"] = "real" if c_real_vol else "synthetic"
                        cache[float(c["time"])] = c
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            self._last_resync[key] = now

        if streamed:
            # Fold in ticks pushed since we last looked — pure in-memory read,
            # no request sent to Quotex.
            try:
                # Reads shared memory today (realtime.py:500) so it is
                # fast, but it is an `async def` on the provider boundary
                # -- bounded so a future implementation change cannot
                # silently reintroduce an unbounded await on this path.
                ticks = await asyncio.wait_for(
                    self._client.get_realtime_price(asset), timeout=PROVIDER_IO_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                ticks = []
            fresh = self._new_ticks(asset, period, ticks)
            folded = []
            for t in fresh:
                try:
                    ts, price = float(t["time"]), float(t["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                bucket = float(int(ts // period * period))
                row = cache.get(bucket)
                if row is None:
                    # RCA C.2: `volume` is 0 here on purpose. Quotex tick
                    # frames carry no volume field at all (api.py:790), so
                    # there is nothing honest to put in it. The activity
                    # count lives in "ticks" and is tagged synthetic so
                    # indicators treat any volume maths as neutral.
                    cache[bucket] = {"time": bucket, "open": price, "high": price,
                                     "low": price, "close": price, "volume": 0.0,
                                     "ticks": 1, "_volume_source": "synthetic"}
                else:
                    row["close"] = price
                    row["high"] = max(row["high"], price)
                    row["low"] = min(row["low"], price)
                    # Count the tick as ACTIVITY only. This used to also do
                    # `row["volume"] += 1.0`, which put a tick count in a
                    # field named volume; every consumer that forgot the
                    # synthetic tag then did order-flow maths on it.
                    if row.get("_volume_source") != "real":
                        row["_volume_source"] = "synthetic"
                    try:
                        row["ticks"] = int(row.get("ticks", 0) or 0) + 1
                    except Exception:
                        row["ticks"] = 1
                folded.append(t)
            self._advance_tick_cursor(asset, period, ticks, folded)

        if len(cache) > 800:  # cap memory
            for k in sorted(cache)[:len(cache) - 800]:
                cache.pop(k, None)

        rows = [cache[k] for k in sorted(cache)][-count:]
        out: List[Candle] = []
        for c in rows:
            try:
                vol = c.get("volume", 0) or 0
                v_src = c.get("_volume_source") or "synthetic"
                # RCA C.2: the `ticks` -> `volume` fallback is gone. A tick
                # count is not a traded volume, so when the broker published
                # no real volume this stays 0 and the tag stays "synthetic".
                # Honour a real volume field only if the broker sent one.
                out.append(Candle(timestamp=float(c["time"]), open=float(c["open"]), high=float(c["high"]),
                                  low=float(c["low"]), close=float(c["close"]), volume=float(vol),
                                  volume_source=v_src))
            except Exception:
                continue
        return out

    async def get_payout(self, asset: str, timeframe: str) -> float:
        try:
            tf = "5" if timeframe in ("5m",) else "1"
            val = self._client.get_payout_by_asset(asset, tf)
            return float(val or 0)
        except Exception:
            return 0.0

    async def fetch_history(self, asset: str, timeframe: str, bars: int) -> List[Candle]:
        """Deep historical fetch for backtesting, independent of the small
        rolling cache/live-stream path get_candles() uses. Pulls directly
        from pyquotex's history endpoint in one shot — Quotex allows a much
        bigger span per request than a live scan ever needs."""
        period = TIMEFRAMES.get(timeframe, 60)
        try:
            hist = await self._client.get_historical_candles(
                asset, amount_of_seconds=period * bars, period=period, max_workers=5
            )
        except Exception as exc:
            logger.warning("fetch_history failed for %s %s: %s", asset, timeframe, exc)
            return []
        rows = sorted((c for c in (hist or []) if isinstance(c, dict) and "time" in c),
                      key=lambda c: c["time"])
        out: List[Candle] = []
        for c in rows[-bars:]:
            try:
                c_real_vol = False
                try:
                    raw_v = c.get("volume")
                    if raw_v is not None and raw_v != "" and float(raw_v) != 0.0:
                        c_real_vol = True
                except (TypeError, ValueError):
                    c_real_vol = False
                # RCA C.2: honour a real broker volume only. The previous
                # `ticks` fallback put a tick count into `volume`.
                vol = c.get("volume", 0) or 0
                out.append(Candle(timestamp=float(c["time"]), open=float(c["open"]), high=float(c["high"]),
                                  low=float(c["low"]), close=float(c["close"]), volume=float(vol),
                                  volume_source="real" if c_real_vol else "synthetic"))
            except Exception:
                continue
        return out

    async def place_order(self, asset: str, amount: float, direction: Direction, duration: int):
        d = "call" if direction == Direction.CALL else "put"
        # time_mode was hardcoded to the library default ("TIME" -> optionType 3).
        # Now configurable via trading.order_time_mode so the working protocol
        # shape can be found without editing vendor code.
        mode = str(getattr(self, "_order_time_mode", None) or "TIMER").upper()
        logger.info("[place_order] %s %s %ss $%s time_mode=%s", asset, d, duration, amount, mode)
        ok, info = await self._client.buy(amount, asset, d, duration, time_mode=mode)
        if not ok:
            # Distinguish the two failures that matter for money. pyquotex's
            # preparation phase fails BEFORE the order is transmitted, so
            # "no order was sent" is a clean, definite no-trade: the caller can
            # safely move on. Anything else (notably a confirmation timeout)
            # means the order may have reached the broker, and that has to
            # surface as an unknown outcome rather than a plain failure.
            detail = str(info)
            if "no order was sent" in detail.lower():
                raise OrderNotSent(f"buy failed before transmission: {detail}")
            raise RuntimeError(f"buy failed: {detail}")
        oid = str(info.get("id") if isinstance(info, dict) else info)
        self._orders[oid] = {"asset": asset}
        open_price = info.get("openPrice") if isinstance(info, dict) else None
        return oid, open_price

    async def recover_result(self, order_id: str):
        """Settle an order from the broker's own trade history. RCA F8.

        Uses pyquotex's `get_result()`, which reads `get_trader_history()` --
        the broker's server-side record, not anything cached in this process --
        so it still knows about trades opened before a restart.

        The vendor classifies that history as `"win" if profitAmount > 0 else
        "loss"`, which is exactly the tie/refund defect fixed in
        `check_result` (RCA F7): a returned stake would be booked as a loss
        and would trip `consecutive_losses` and escalate `_martingale_step`.
        So the vendor's verdict string is ignored and the classification is
        done here from `profitAmount` by sign, with zero meaning draw.
        """
        try:
            _vendor_status, item = await asyncio.wait_for(
                self._client.get_result(str(order_id)), CHECK_RESULT_TIMEOUT
            )
        except asyncio.TimeoutError:
            log_event(
                logger, logging.WARNING, "recover_result_timeout",
                order_id=str(order_id), timeout_seconds=CHECK_RESULT_TIMEOUT,
                reason="broker history did not answer in time -- trade left pending",
            )
            return None
        except Exception as exc:
            logger.debug("recover_result(%s) failed: %s", order_id, exc)
            return None

        if not isinstance(item, dict):
            # The vendor returns (None, "OperationID Not Found.") when the
            # order is not in the history page it fetched.
            return None

        try:
            amount = float(item.get("profitAmount", 0) or 0)
        except (TypeError, ValueError):
            return None

        if amount > 0:
            status = "win"
        elif amount < 0:
            status = "loss"
        else:
            status = "draw"

        log_event(
            logger, logging.INFO, "trade_recovered_from_history",
            order_id=str(order_id), status=status, profit=amount,
            reason="settled from broker trade history after a restart",
        )
        return {"status": status, "profit": amount, "close_price": None, "open_price": None}

    async def check_result(self, order_id: str):
        """Resolve one order. Returns a result dict, or None when the outcome
        is NOT YET KNOWN -- and "not yet known" must never be reported as a
        loss (RCA F7).

        Two defects fixed here:

        1. THE VENDOR'S TIMEOUT LOOKED LIKE A LOSS. pyquotex's
           `check_win()` returns the literal tuple `("loss", 0.0)` in three
           situations that have nothing to do with losing: `self.api is None`,
           an empty result payload, and -- the important one -- its own
           internal `slot.wait(timeout=300)` giving up after five minutes
           (_api/trading.py:216-217). The old code passed that straight
           through, so a trade whose confirmation was merely slow got booked
           as a $0 LOSS. We now impose our own, much shorter ceiling and
           return None on timeout, leaving the trade pending for the next
           pass. Retrying is safe and cheap: `check_win` has a cached
           fast-path via `listinfodata`, and `release_win_result()` only pops
           the slot dict so a later `_on_message` simply recreates it -- no
           result is lost by bailing out early.

        2. TIES AND REFUNDS WERE LOSSES. pyquotex derives its verdict as
           `win = "win" if profit > 0 else "loss"` in three separate places
           (api.py:643, 718, 762), so a trade that returns the stake --
           a tie, or a broker refund/void -- arrives as `("loss", 0.0)` and
           was booked as a loss. That is not cosmetic: `record_result()`
           increments `consecutive_losses` (three in a row auto-pauses the
           bot) AND `_martingale_step`, which escalates the stake on the next
           trade after one that lost nothing. The old `else "draw"` branch in
           this function was unreachable, because the vendor only ever emits
           the strings "win" and "loss" and both matched an earlier branch.

           We therefore classify on the PROFIT SIGN, which is the
           economically meaningful value, and treat exactly zero as a draw.
           Verified against 25 real recorded trades in the repo's own
           trade history: every `loss` has profit < 0 (e.g. -1.4, -5.8,
           -6.6) and every `win` has profit > 0 (e.g. 4.968, 441.6) -- so
           `profit` is signed net P&L, and zero can only mean "stake
           returned". The vendor's string is kept only to log the anomaly if
           it ever contradicts the money.
        """
        try:
            win, profit = await asyncio.wait_for(
                self._client.check_win(order_id), CHECK_RESULT_TIMEOUT
            )
        except asyncio.TimeoutError:
            log_event(
                logger, logging.WARNING, "check_result_timeout",
                order_id=str(order_id), timeout_seconds=CHECK_RESULT_TIMEOUT,
                reason="broker did not confirm the outcome in time -- left "
                       "pending rather than booked as a loss",
            )
            return None
        except Exception:
            return None
        if win is None:
            return None

        amount = float(profit or 0)
        if amount > 0:
            status = "win"
        elif amount < 0:
            status = "loss"
        else:
            status = "draw"

        w = str(win).lower()
        vendor_says_win = "win" in w and "loss" not in w
        if vendor_says_win != (status == "win"):
            log_event(
                logger, logging.WARNING, "check_result_verdict_mismatch",
                order_id=str(order_id), vendor_verdict=w, profit=amount,
                classified_as=status,
                reason="broker verdict string disagrees with the profit sign "
                       "-- classified on the money",
            )
        return {"status": status, "profit": amount, "close_price": None, "open_price": None}


def build_provider(name: str, settings, otp_callback=None, sessions_dir: Optional[str] = None) -> MarketProvider:
    """Construct a provider by name. Falls back to the public real-data provider
    when Quotex credentials/SSID are not available. `sessions_dir`, when given,
    isolates the pyquotex on-disk session cache per user."""
    if name == "pyquotex" and (
        (settings.quotex_email and settings.quotex_password) or settings.quotex_ssid
    ):
        return PyQuotexProvider(
            settings.quotex_email or "",
            settings.quotex_password or "",
            settings.quotex_is_demo,
            otp_callback=otp_callback,
            ssid=settings.quotex_ssid,
            user_agent=settings.quotex_user_agent,
            cookies=getattr(settings, "quotex_cookies", None),
            sessions_dir=sessions_dir,
        )
    return PublicDataProvider()
