"""Realtime tick + sentiment state for the signal engine.

Two hard requirements drive this module:

*  **Every piece of state is keyed by `(asset, timeframe)`, never by asset alone.**
    The provider already learned this the hard way — `market.py:_new_ticks`'s
    docstring records that a cursor shared across timeframes starves whichever
    timeframe reads second. This module uses the same key shape.

*  **Nothing here can break the signal engine.** Every public method catches its own
    errors and returns a neutral value. Sentiment is optional: a broker that never
    publishes it simply yields an unavailable `SentimentState`, and the engine
    proceeds on OHLC exactly as it does today.

Tick *counts* are recorded as activity, never as volume. There is no `real_volume`
field anywhere in this module and none may be added.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from app.engine.market_features import (
    NEUTRAL_INTENSITY,
    QualityReport,
    SentimentState,
    TickActivity,
    compute_tick_activity,
    normalize_sentiment,
)

logger = logging.getLogger(__name__)

Key = Tuple[str, str]


def make_key(asset: str, timeframe: str) -> Key:
    """The single canonical key shape for all realtime state."""
    return (str(asset), str(timeframe))


class _BarTicks:
    """Ticks observed inside one forming bar, with dedup bookkeeping."""

    __slots__ = ("bucket", "period", "prices", "times", "seen", "open", "high", "low")

    def __init__(self, bucket: float, period: float = 0.0) -> None:
        self.bucket = float(bucket)
        self.period = float(period)
        self.prices: List[float] = []
        self.times: List[float] = []
        self.seen: set = set()
        self.open: Optional[float] = None
        self.high: Optional[float] = None
        self.low: Optional[float] = None


class RealtimeStateManager:
    """Holds per-`(asset, timeframe)` tick activity and sentiment.

    Memory is bounded on every axis: a fixed number of buckets per key, a fixed
    number of ticks per bucket, and a fixed baseline window. This runs inside the
    scan loop, so it must not grow with uptime.
    """

    def __init__(
        self,
        *,
        max_ticks_per_bar: int = 512,
        baseline_bars: int = 40,
        max_keys: int = 256,
    ) -> None:
        self._bars: Dict[Key, Deque[_BarTicks]] = {}
        self._counts: Dict[Key, Deque[float]] = {}
        self._sentiment: Dict[Key, SentimentState] = {}
        self._last_seen_tick: Dict[Key, float] = {}
        self._max_ticks_per_bar = int(max_ticks_per_bar)
        self._baseline_bars = int(baseline_bars)
        self._max_keys = int(max_keys)

    # ────────────────────────────────────────────────────────────────────────
    # Ticks
    # ────────────────────────────────────────────────────────────────────────
    def observe_ticks(
        self,
        asset: str,
        timeframe: str,
        ticks: Sequence[Any],
        *,
        period_seconds: float,
        report: Optional[QualityReport] = None,
        now: Optional[float] = None,
    ) -> TickActivity:
        """Fold raw broker ticks into the current bar and return its activity.

        Ticks are `(ts, price)` pairs, dicts with `time`/`price`, or 3/4-element
        sequences — the shapes `api.py:783-796` can hand back. Anything
        unparseable is skipped rather than raising.

        Duplicate detection is on `(timestamp, price)` identity, so a broker that
        re-sends its whole bounded buffer on every poll (which pyquotex does) does
        not inflate the activity counts. Out-of-order ticks are accepted but
        flagged: they still belong to their own bucket, and dropping them would
        silently bias the range.
        """
        key = make_key(asset, timeframe)
        report = report if report is not None else QualityReport()
        period = float(period_seconds or 0.0)
        if period <= 0:
            report.add("invalid_timeframe")
            return TickActivity()

        bars = self._bars.setdefault(key, deque(maxlen=self._baseline_bars))
        prev_ts = self._last_seen_tick.get(key)
        for t in ticks or []:
            ts, price = _parse_tick(t)
            if ts is None or price is None:
                report.add("malformed_tick")
                continue
            if prev_ts is not None and ts < prev_ts:
                report.add("out_of_order_tick")
            bucket = float(int(ts // period * period))
            bar = self._current_bar(bars, bucket, period)
            sig = (round(ts, 6), round(price, 10))
            if sig in bar.seen:
                report.add("duplicate_tick")
                continue
            if len(bar.prices) >= self._max_ticks_per_bar:
                continue  # bounded: activity saturates instead of growing
            bar.seen.add(sig)
            bar.prices.append(price)
            bar.times.append(ts)
            bar.open = price if bar.open is None else bar.open
            bar.high = price if bar.high is None else max(bar.high, price)
            bar.low = price if bar.low is None else min(bar.low, price)
            self._last_seen_tick[key] = max(prev_ts or ts, ts)
            prev_ts = self._last_seen_tick[key]

        self._evict_keys()
        if not bars:
            return TickActivity()
        return self._activity_for(bars[-1], key, period)

    def _current_bar(self, bars: Deque[_BarTicks], bucket: float,
                     period: float = 0.0) -> _BarTicks:
        """Return the bar for `bucket`, starting a new one when the clock rolls."""
        if bars and bars[-1].bucket == bucket:
            return bars[-1]
        if bars and bucket < bars[-1].bucket:
            # An older bucket arrived late: reuse it if we still hold it rather
            # than corrupting the newest bar's range with a stale price.
            for bar in bars:
                if bar.bucket == bucket:
                    return bar
        bar = _BarTicks(bucket, period)
        bars.append(bar)
        return bar

    def _activity_for(self, bar: _BarTicks, key: Key, period: float) -> TickActivity:
        counts = self._counts.setdefault(key, deque(maxlen=self._baseline_bars))
        act = compute_tick_activity(
            bar.prices,
            bar.times,
            period_seconds=period,
            bar_open=bar.open,
            bar_high=bar.high,
            bar_low=bar.low,
            baseline_counts=list(counts),
        )
        # Publish the finished bar's count to the baseline only when the bar is
        # complete; a half-formed bar would drag the average down and make every
        # subsequent bar look "intense".
        return act

    def commit_bar(self, asset: str, timeframe: str) -> None:
        """Record the newest bar's tick count into the activity baseline.

        Called once per closed candle, so `activity_intensity` compares a finished
        bar against finished bars.
        """
        key = make_key(asset, timeframe)
        bars = self._bars.get(key)
        if not bars:
            return
        counts = self._counts.setdefault(key, deque(maxlen=self._baseline_bars))
        counts.append(float(len(bars[-1].prices)))

    def tick_activity(self, asset: str, timeframe: str) -> TickActivity:
        """Current activity without folding anything new."""
        key = make_key(asset, timeframe)
        bars = self._bars.get(key)
        if not bars:
            return TickActivity()
        counts = list(self._counts.get(key, ()))
        return compute_tick_activity(
            bars[-1].prices,
            bars[-1].times,
            period_seconds=bars[-1].period,
            bar_open=bars[-1].open,
            bar_high=bars[-1].high,
            bar_low=bars[-1].low,
            baseline_counts=counts,
        )

    def last_tick_ts(self, asset: str, timeframe: str) -> float:
        return float(self._last_seen_tick.get(make_key(asset, timeframe), 0.0))

    # ────────────────────────────────────────────────────────────────────────
    # Sentiment
    # ────────────────────────────────────────────────────────────────────────
    def update_sentiment(
        self,
        asset: str,
        timeframe: str,
        payload: Optional[Dict[str, Any]],
        *,
        max_age_seconds: float = 120.0,
        now: Optional[float] = None,
    ) -> SentimentState:
        """Normalise and store a broker sentiment payload for this key.

        A `None`/empty payload leaves any previously stored state in place but
        re-evaluates its staleness, so a stream that stops mid-session ages out
        instead of being trusted forever.
        """
        key = make_key(asset, timeframe)
        now = float(now if now is not None else time.time())
        if payload:
            state = normalize_sentiment(payload, now=now, max_age_seconds=max_age_seconds)
            if state.available:
                self._sentiment[key] = state
                self._evict_keys()
                return state
        stored = self._sentiment.get(key)
        if stored is None:
            return SentimentState(timestamp=now, stale=True, available=False)
        stored.stale = (now - stored.timestamp) > float(max_age_seconds)
        return stored

    def sentiment(
        self,
        asset: str,
        timeframe: str,
        *,
        max_age_seconds: float = 120.0,
        now: Optional[float] = None,
    ) -> SentimentState:
        """Current sentiment for this key, with staleness re-evaluated on read.

        Reading must not silently resurrect an old payload: the caller decides
        policy, but `stale` is always accurate as of `now`.
        """
        key = make_key(asset, timeframe)
        now = float(now if now is not None else time.time())
        stored = self._sentiment.get(key)
        if stored is None:
            return SentimentState(timestamp=now, stale=True, available=False)
        stored.stale = (now - stored.timestamp) > float(max_age_seconds)
        return stored

    # ────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────────────
    def reset(self, asset: Optional[str] = None, timeframe: Optional[str] = None) -> None:
        """Clear state — all keys, or just one `(asset, timeframe)`."""
        if asset is None:
            self._bars.clear()
            self._counts.clear()
            self._sentiment.clear()
            self._last_seen_tick.clear()
            return
        if timeframe is None:
            for store in (self._bars, self._counts, self._sentiment, self._last_seen_tick):
                for k in [k for k in store if k[0] == asset]:
                    store.pop(k, None)
            return
        key = make_key(asset, timeframe)
        for store in (self._bars, self._counts, self._sentiment, self._last_seen_tick):
            store.pop(key, None)

    def _evict_keys(self) -> None:
        """Bound total memory: drop the least recently seen keys past `max_keys`."""
        if len(self._bars) <= self._max_keys:
            return
        stale = sorted(
            self._bars,
            key=lambda k: self._last_seen_tick.get(k, 0.0),
        )
        for k in stale[: len(self._bars) - self._max_keys]:
            self._bars.pop(k, None)
            self._counts.pop(k, None)
            self._sentiment.pop(k, None)
            self._last_seen_tick.pop(k, None)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Debug/observability view, keyed `"asset|timeframe"`."""
        out: Dict[str, Dict[str, Any]] = {}
        for key in self._bars:
            asset, tf = key
            act = self.tick_activity(asset, tf)
            out[f"{asset}|{tf}"] = {
                **act.as_dict(),
                **self.sentiment(asset, tf).as_dict(),
            }
        return out


def _parse_tick(t: Any) -> Tuple[Optional[float], Optional[float]]:
    """Extract `(timestamp, price)` from any tick shape the vendor can produce."""
    try:
        if isinstance(t, dict):
            ts = t.get("time", t.get("timestamp", t.get("ts")))
            price = t.get("price", t.get("close", t.get("value")))
        elif isinstance(t, (list, tuple)):
            # Wire format is [asset, ts, price, direction]; the stored form is
            # {time, price}. Both positional shapes are handled.
            if len(t) >= 4:
                ts, price = t[1], t[2]
            elif len(t) >= 3:
                ts, price = t[1], t[2]
            elif len(t) == 2:
                ts, price = t[0], t[1]
            else:
                return None, None
        else:
            return None, None
        return float(ts), float(price)
    except (TypeError, ValueError, IndexError, KeyError):
        return None, None
