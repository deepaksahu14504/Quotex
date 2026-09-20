"""Incremental (streaming) indicator engine.

`indicators.py` + `strategies._enrich()` recompute ~15 indicator columns over
the *entire* candle window (120 bars) every time they're called — fine for a
one-off backtest, wasteful for a live loop re-evaluating the same window
every few seconds when usually only the still-forming candle changed.

This module keeps a small per-(asset, timeframe) state object and advances it
one bar at a time:
  - On a brand new candle closing: O(1) state update (EMA/Wilder smoothing +
    small fixed-size windows for Bollinger/Stochastic/StochRSI), appended to
    a bounded tail of already-enriched rows.
  - On the same still-forming candle being re-scanned (the common case,
    since scans run every few seconds but a 1m candle only closes once a
    minute): a non-mutating "peek" recomputes just that last row.
  - On anything unexpected (asset switch, timeframe switch, gap, backend
    restart): falls back to a full rebuild from the fetched window — safe
    correctness net, still only O(window) once.

Output shape matches what `strategies.py` expects from `_enrich()`, so the
orchestrator can hand this straight to `strategies.evaluate(..., pre_enriched=True)`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from .indicators import (
    candles_to_df, find_support_resistance, volume_ma, volume_strength,
    volatility_ratio, price_action_strength, confluence_points,
    time_session_volatility_multiplier, _VOLUME_SOURCE_COLUMN,
)

def _carry_volume_source(row: dict, raw, i: int) -> dict:
    """Copy the `_volume_source` tag from the raw frame onto a hand-built row.

    RCA F5. `candles_to_df()` tags every row with the provider's volume
    source ("real" for Binance/Bybit order flow, "synthetic" for the
    tick-count proxy pyquotex fills in), and `indicators._has_real_volume()`
    honours that tag FIRST -- its own docstring explains that the
    variance+sum fallback heuristic MISCLASSIFIES pyquotex tick counts as real
    volume, because tick count varies, has nunique()>1 and sum()>0.

    But `get_enriched()` below does not build its frame with
    `candles_to_df`; it assembles each row by hand from the incremental
    `SymbolState` and copied over only timestamp/open/high/low/close/volume.
    The tag was silently dropped, so the LIVE trading frame had no
    `_volume_source` column and fell through to exactly the heuristic the tag
    exists to avoid -- feeding synthetic tick counts into volume_ma and
    volume_strength as if they were order flow. Measured on 240 synthetic
    pyquotex-style candles, last closed bar: vol_ma 23.85 vs 1.0 and
    vol_strength 0.922 vs 1.0 against the vectorized path, and
    `_has_real_volume` True instead of False. That undid PR #3's fix on the
    live path only, so backtest and live disagreed.
    """
    if _VOLUME_SOURCE_COLUMN in getattr(raw, "columns", ()):
        row[_VOLUME_SOURCE_COLUMN] = raw[_VOLUME_SOURCE_COLUMN].iloc[i]
    return row


# RCA F6: how many rows of raw history the bounded-tail derived columns are
# computed over. TAIL_ROWS alone is NOT enough: volatility_ratio() takes
# atr_pct.rolling(50).mean(), and with ATR(20) still warming up a 61-row frame
# yields only ~41 usable values, so the whole column came back NaN and the
# `.fillna(1.0)` inside it flattened vol_ratio to a constant 1.0 on the live
# path while the vectorized path (which sees the full window) produced a real
# multiplier.
#
# Measured convergence of vol_ratio against the vectorized reference on the
# last closed bar of a 240-candle series (reference 1.022449):
#     window  61 -> 1.000000  (constant, every bar -- the bug)
#     window 120 -> 1.019878  (diff 2.6e-3, varies correctly)
#     window 200 -> 1.022389  (diff 6.0e-5)
#     window 240 -> 1.022449  (exact)
# The residual is Wilder smoothing in atr(): it recurses over the whole series,
# so a shorter warm-up leaves a small seed offset. 200 buys near-exact parity,
# and because the broker's own history ceiling is around 120 candles this
# costs nothing in production -- `base.tail(200)` simply returns whatever is
# available. Measured warm-path cost at raw=120: 14.6ms -> 20.7ms per
# get_enriched() call, which is the inherent price of computing over the real
# window instead of a truncated one.
DERIVED_WINDOW = 200

TAIL_ROWS = 60  # sized for confluence_points()'s internal n>=50 gate (needs a
# real ~10-bar runway past that minimum to produce a meaningful value for the
# most recent row) -- comfortably covers every other lookback in this module
# too (rolling(20), lookback=30, lookback=12).


@dataclass
class _Ema:
    period: int
    value: Optional[float] = None

    def calc(self, price: float, commit: bool) -> float:
        alpha = 2.0 / (self.period + 1)
        val = price if self.value is None else price * alpha + self.value * (1 - alpha)
        if commit:
            self.value = val
        return val


@dataclass
class _Wilder:
    """EWM(alpha=1/period, adjust=False) — matches the Wilder smoothing used
    by the vectorized RSI/ATR/ADX."""
    period: int
    value: Optional[float] = None

    def calc(self, x: float, commit: bool) -> float:
        alpha = 1.0 / self.period
        val = x if self.value is None else x * alpha + self.value * (1 - alpha)
        if commit:
            self.value = val
        return val


@dataclass
class SymbolState:
    bars_seen: int = 0
    prev_close: Optional[float] = None
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None

    ema: Dict[int, _Ema] = field(default_factory=dict)
    ema12: _Ema = field(default_factory=lambda: _Ema(12))
    ema26: _Ema = field(default_factory=lambda: _Ema(26))
    macd_signal: _Ema = field(default_factory=lambda: _Ema(9))

    rsi_gain: _Wilder = field(default_factory=lambda: _Wilder(14))
    rsi_loss: _Wilder = field(default_factory=lambda: _Wilder(14))

    atr14: _Wilder = field(default_factory=lambda: _Wilder(14))
    atr10: _Wilder = field(default_factory=lambda: _Wilder(10))  # Supertrend uses period=10

    plus_dm_s: _Wilder = field(default_factory=lambda: _Wilder(14))
    minus_dm_s: _Wilder = field(default_factory=lambda: _Wilder(14))
    dx_s: _Wilder = field(default_factory=lambda: _Wilder(14))

    closes_20: Deque[float] = field(default_factory=lambda: deque(maxlen=20))
    hl_14: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=14))
    k_3: Deque[float] = field(default_factory=lambda: deque(maxlen=3))
    rsi_14: Deque[float] = field(default_factory=lambda: deque(maxlen=14))
    srsi_k_3: Deque[float] = field(default_factory=lambda: deque(maxlen=3))
    srsi_d_3: Deque[float] = field(default_factory=lambda: deque(maxlen=3))

    st_final_upper: Optional[float] = None
    st_final_lower: Optional[float] = None
    st_dir: float = 1.0

    psar_val: Optional[float] = None
    psar_ep: Optional[float] = None
    psar_af: float = 0.02
    psar_up: bool = True

    def _ema(self, period: int) -> _Ema:
        if period not in self.ema:
            self.ema[period] = _Ema(period)
        return self.ema[period]

    def step(self, o: float, h: float, l: float, c: float, commit: bool) -> dict:
        """Compute the indicator row for one bar. When `commit=False` (the
        still-forming candle), all state reads current values but writes
        nothing back — so re-peeking the same live candle every scan never
        corrupts the smoothed state."""
        row: dict = {}
        prev_close = self.prev_close
        prev_high = self.prev_high
        prev_low = self.prev_low

        for p in (5, 8, 9, 13, 21, 50):
            row[f"ema{p}"] = self._ema(p).calc(c, commit)

        fast = self.ema12.calc(c, commit)
        slow = self.ema26.calc(c, commit)
        macd_line = fast - slow
        signal = self.macd_signal.calc(macd_line, commit)
        row["macd"], row["macd_sig"], row["macd_hist"] = macd_line, signal, macd_line - signal

        if prev_close is None:
            gain, loss = 0.0, 0.0
        else:
            delta = c - prev_close
            gain, loss = max(delta, 0.0), max(-delta, 0.0)
        if self.bars_seen >= 1:
            avg_gain = self.rsi_gain.calc(gain, commit)
            avg_loss = self.rsi_loss.calc(loss, commit)
            if avg_loss <= 1e-12:
                rsi_val = 100.0 if avg_gain > 0 else 50.0
            else:
                rsi_val = 100 - 100 / (1 + avg_gain / avg_loss)
        else:
            rsi_val = 50.0
        row["rsi"] = rsi_val

        closes_20 = self.closes_20 if commit else deque(self.closes_20, maxlen=20)
        closes_20.append(c)
        if len(closes_20) == 20:
            arr = np.fromiter(closes_20, dtype=float)
            mid, std = arr.mean(), arr.std(ddof=0)
            row["bb_mid"], row["bb_up"], row["bb_low"] = mid, mid + 2 * std, mid - 2 * std
            row["bb_width"] = (row["bb_up"] - row["bb_low"]) / mid if mid else np.nan
        else:
            row["bb_mid"] = row["bb_up"] = row["bb_low"] = row["bb_width"] = np.nan
        if commit:
            self.closes_20 = closes_20

        hl_14 = self.hl_14 if commit else deque(self.hl_14, maxlen=14)
        hl_14.append((h, l))
        highs = [x[0] for x in hl_14]
        lows = [x[1] for x in hl_14]
        hh, ll = max(highs), min(lows)
        k = 50.0 if hh == ll else 100 * (c - ll) / (hh - ll)
        k_3 = self.k_3 if commit else deque(self.k_3, maxlen=3)
        k_3.append(k)
        row["stoch_k"], row["stoch_d"] = k, float(np.mean(k_3))
        if commit:
            self.hl_14, self.k_3 = hl_14, k_3

        tr = (h - l) if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        atr14 = self.atr14.calc(tr, commit)
        row["atr"] = atr14

        if prev_high is None:
            plus_dm = minus_dm = 0.0
        else:
            up_move = h - prev_high
            down_move = prev_low - l
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dm_s = self.plus_dm_s.calc(plus_dm, commit)
        minus_dm_s = self.minus_dm_s.calc(minus_dm, commit)
        atr_safe = atr14 if atr14 else None
        plus_di = 100 * plus_dm_s / atr_safe if atr_safe else 0.0
        minus_di = 100 * minus_dm_s / atr_safe if atr_safe else 0.0
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0
        row["adx"] = self.dx_s.calc(dx, commit)

        rsi_14 = self.rsi_14 if commit else deque(self.rsi_14, maxlen=14)
        rsi_14.append(rsi_val)
        if len(rsi_14) == 14:
            arr = np.fromiter(rsi_14, dtype=float)
            rmin, rmax = arr.min(), arr.max()
            srsi = 50.0 if rmax == rmin else (rsi_val - rmin) / (rmax - rmin) * 100
        else:
            srsi = 50.0
        srsi_k_3 = self.srsi_k_3 if commit else deque(self.srsi_k_3, maxlen=3)
        srsi_k_3.append(srsi)
        k_line = float(np.mean(srsi_k_3))
        srsi_d_3 = self.srsi_d_3 if commit else deque(self.srsi_d_3, maxlen=3)
        srsi_d_3.append(k_line)
        row["srsi_k"], row["srsi_d"] = k_line, float(np.mean(srsi_d_3))
        if commit:
            self.rsi_14, self.srsi_k_3, self.srsi_d_3 = rsi_14, srsi_k_3, srsi_d_3

        atr10 = self.atr10.calc(tr, commit)
        hl2 = (h + l) / 2
        upper, lower = hl2 + 3.0 * atr10, hl2 - 3.0 * atr10
        prev_fu, prev_fl, st_dir = self.st_final_upper, self.st_final_lower, self.st_dir
        if prev_fu is None:
            fu, fl = upper, lower
        else:
            ref_close = prev_close if prev_close is not None else c
            fu = min(upper, prev_fu) if ref_close <= prev_fu else upper
            fl = max(lower, prev_fl) if ref_close >= prev_fl else lower
            # Direction flip must compare against the PRIOR bar's finalized bands
            # (matching indicators.py's vectorized supertrend()), not the bands
            # just recomputed above for this bar -- comparing against the new
            # bands flips the trend one bar early whenever the band narrowed.
            if c > prev_fu:
                st_dir = 1.0
            elif c < prev_fl:
                st_dir = -1.0
        row["st_dir"] = st_dir
        if commit:
            self.st_final_upper, self.st_final_lower, self.st_dir = fu, fl, st_dir

        p_val, p_ep, p_af, p_up = self.psar_val, self.psar_ep, self.psar_af, self.psar_up
        if p_val is None:
            p_val, p_ep, p_af, p_up = l, h, 0.02, True
        else:
            p_val = p_val + p_af * (p_ep - p_val)
            if p_up:
                if l < p_val:
                    p_up, p_val, p_ep, p_af = False, p_ep, l, 0.02
                elif h > p_ep:
                    p_ep, p_af = h, min(p_af + 0.02, 0.2)
            else:
                if h > p_val:
                    p_up, p_val, p_ep, p_af = True, p_ep, h, 0.02
                elif l < p_ep:
                    p_ep, p_af = l, min(p_af + 0.02, 0.2)
        row["psar_dir"] = 1.0 if p_up else -1.0
        if commit:
            self.psar_val, self.psar_ep, self.psar_af, self.psar_up = p_val, p_ep, p_af, p_up

        if commit:
            self.prev_close, self.prev_high, self.prev_low = c, h, l
            self.bars_seen += 1

        return row


def _add_derived_columns(enriched: pd.DataFrame,
                         base: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Adds support/resistance (+ touch-count strength) and volume-strength
    columns that this module previously never computed at all.

    Bug this fixes: `strategies._enrich()` (used by backtest.py and any
    direct evaluate() call) computes these columns, but this incremental
    engine -- the one actually used for LIVE trading via
    orchestrator._evaluate_asset_signal() -- did not. Any strategy or
    confluence-scoring logic reading row["support"]/["resistance"]/
    ["vol_strength"] would silently get nothing live (None/NaN via `.get()`,
    or a caught exception turning into a neutral vote for code using plain
    bracket access), even though the exact same logic worked in backtest.
    That's a live/backtest parity gap, not just a missing feature -- a
    strategy validated in backtest could behave differently once actually
    trading. Computed here as a small, cheap vectorized pass over just the
    bounded tail window (TAIL_ROWS = 60 rows), not maintained as incremental
    state -- support/resistance and volume-strength are naturally suited to
    a rolling-window recompute, unlike EMA/RSI/MACD which benefit from true
    O(1) incremental updates."""
    support, resistance, support_strength, resistance_strength = find_support_resistance(
        enriched, lookback=30, prominence=0.0015,
    )
    enriched["support"] = support
    enriched["resistance"] = resistance
    enriched["support_strength"] = support_strength
    enriched["resistance_strength"] = resistance_strength
    enriched["vol_ma"] = volume_ma(enriched, period=20)
    enriched["vol_strength"] = volume_strength(enriched, period=20)
    enriched["vol_ratio"] = volatility_ratio(enriched, period=20)
    enriched["price_strength"] = price_action_strength(enriched)
    enriched["time_vol_mult"] = time_session_volatility_multiplier(enriched)
    # confluence_points() requires n>=50 rows internally or it returns all
    # zeros -- TAIL_ROWS=60 (tail + 1 live row = 61) clears that with a real
    # ~10-bar runway, so this now produces meaningful values for the most
    # recent rows instead of always reading 0.0.
    enriched["confluence"] = confluence_points(enriched)
    return enriched


def _add_derived_columns_windowed(enriched: pd.DataFrame,
                                  base: Optional[pd.DataFrame]) -> pd.DataFrame:
    """`_add_derived_columns`, computed over a longer raw window and aligned
    back onto `enriched`. RCA F6.

    The warm path only holds TAIL_ROWS(+1) rows of state, which is too short
    for volatility_ratio()'s rolling(50) over a still-warming ATR(20) -- the
    result was all-NaN and the `.fillna(1.0)` inside it silently turned
    vol_ratio into a constant 1.0 for every live bar.

    Every column computed here reads only OHLCV/timestamp/volume (verified
    across volume_ma, volume_strength, volatility_ratio,
    price_action_strength, time_session_volatility_multiplier and
    confluence_points -- none of them touches an incrementally-maintained
    column), and `enriched` carries exactly the raw OHLCV for the rows it
    holds. So the values can be computed on the longer raw window and copied
    back positionally: the trailing `len(enriched)` rows of the two frames are
    the same bars in the same order. The row-count contract of
    `get_enriched()` is unchanged, and no indicator maths is touched.
    """
    if base is None or len(base) <= len(enriched):
        return _add_derived_columns(enriched)

    # The window must be at least as long as the frame we are going to copy
    # back onto, or the positional alignment below has too few rows.
    window = base.tail(max(DERIVED_WINDOW, len(enriched))).reset_index(drop=True)
    computed = _add_derived_columns(window.copy())
    # Align the trailing rows back onto `enriched`.
    aligned = computed.tail(len(enriched)).reset_index(drop=True)
    for col in ("support", "resistance", "support_strength", "resistance_strength",
                "vol_ma", "vol_strength", "vol_ratio", "price_strength",
                "time_vol_mult", "confluence"):
        enriched[col] = aligned[col].to_numpy()
    return enriched


class IndicatorCache:
    """One SymbolState + bounded tail DataFrame per (asset, timeframe) key.
    Safe to share across an orchestrator's whole scan loop; not thread-safe
    for concurrent writers to the *same* key (the scan loop is single-flight
    per user so this is never an issue in practice)."""

    def __init__(self) -> None:
        self._state: Dict[str, SymbolState] = {}
        self._tail: Dict[str, pd.DataFrame] = {}
        self._last_committed_ts: Dict[str, float] = {}

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            self._state.clear()
            self._tail.clear()
            self._last_committed_ts.clear()
        else:
            self._state.pop(key, None)
            self._tail.pop(key, None)
            self._last_committed_ts.pop(key, None)

    # Absolute floor: below this, the indicator functions cannot produce a
    # value at all. It is NOT a quality threshold -- see
    # TradingSettings.min_candles_for_signal, which callers pass in as
    # `min_bars` for live evaluation.
    ABSOLUTE_MIN_BARS = 55

    def get_enriched(self, key: str, candles,
                     min_bars: Optional[int] = None) -> Tuple[Optional[pd.DataFrame], bool]:
        """Returns (enriched_df, warm).

        `min_bars` is the number of real bars required before `warm` is True.
        It used to be hardcoded to 55 -- the point at which the indicator
        functions stop returning NaN. That is a "can compute" floor, not a
        "worth trusting" one: at 55 bars an EMA(50) is still mostly its seed
        value and ADX(14) has barely finished its second smoothing pass, so
        every consumer of this method was grading setups on half-converged
        numbers. Callers now pass the configured threshold (default 200);
        omitting it keeps the old floor for backtests and other callers that
        deliberately work with short windows."""
        floor = int(min_bars) if min_bars else self.ABSOLUTE_MIN_BARS
        floor = max(self.ABSOLUTE_MIN_BARS, floor)
        raw = candles_to_df(candles)
        if len(raw) < floor:
            return None, False

        state = self._state.get(key)
        tail = self._tail.get(key)
        last_ts = self._last_committed_ts.get(key)
        closed_ts = float(raw["timestamp"].iloc[-2])
        # Bar spacing, inferred from the window itself so this works for any
        # timeframe without needing to know it explicitly.
        interval = float(raw["timestamp"].iloc[-1] - raw["timestamp"].iloc[-2]) or 60.0

        cold = state is None or tail is None or last_ts is None
        if not cold and closed_ts < last_ts - 1e-6:
            cold = True  # behind — gap, restart, or a mismatched series
        elif not cold and closed_ts > last_ts + interval * 1.5:
            # Jumped by MORE than one bar (missed a scan entirely) — rebuild
            # to be safe. Advancing by exactly one bar (the normal case, a
            # single candle just closed) is NOT cold — that's the whole
            # point of the incremental path below, and treating every
            # single candle close as "cold" (as this used to do) meant a
            # full rebuild on every bar close instead of one O(1) update.
            cold = True

        if cold:
            state = SymbolState()
            rows = []
            n = len(raw)
            for i in range(n):
                r = raw.iloc[i]
                commit = i < n - 1
                row = state.step(float(r.open), float(r.high), float(r.low), float(r.close), commit)
                row["timestamp"] = float(r.timestamp)
                row["open"], row["high"], row["low"], row["close"] = (
                    float(r.open), float(r.high), float(r.low), float(r.close),
                )
                row["volume"] = float(getattr(r, "volume", 0.0) or 0.0)
                _carry_volume_source(row, raw, i)
                rows.append(row)
            enriched = pd.DataFrame(rows)
            enriched = _add_derived_columns_windowed(enriched, raw)
            self._state[key] = state
            self._tail[key] = enriched.iloc[:-1].tail(TAIL_ROWS).reset_index(drop=True)
            self._last_committed_ts[key] = float(raw["timestamp"].iloc[-2])
            return enriched, True

        if closed_ts > last_ts + 1e-6:
            r = raw.iloc[-2]
            row = state.step(float(r.open), float(r.high), float(r.low), float(r.close), True)
            row["timestamp"] = float(r.timestamp)
            row["open"], row["high"], row["low"], row["close"] = (
                float(r.open), float(r.high), float(r.low), float(r.close),
            )
            row["volume"] = float(getattr(r, "volume", 0.0) or 0.0)
            _carry_volume_source(row, raw, len(raw) - 2)
            tail = pd.concat([tail, pd.DataFrame([row])], ignore_index=True).tail(TAIL_ROWS).reset_index(drop=True)
            self._tail[key] = tail
            self._last_committed_ts[key] = closed_ts

        r = raw.iloc[-1]
        live_row = state.step(float(r.open), float(r.high), float(r.low), float(r.close), False)
        live_row["timestamp"] = float(r.timestamp)
        live_row["open"], live_row["high"], live_row["low"], live_row["close"] = (
            float(r.open), float(r.high), float(r.low), float(r.close),
        )
        live_row["volume"] = float(getattr(r, "volume", 0.0) or 0.0)
        _carry_volume_source(live_row, raw, len(raw) - 1)
        enriched = pd.concat([tail, pd.DataFrame([live_row])], ignore_index=True)
        enriched = _add_derived_columns_windowed(enriched, raw)
        return enriched, True
