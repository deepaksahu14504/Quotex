"""Resolve the candle a trade actually entered on.

The chart, the trade marker and Trade History must all describe the same bar.
Getting that wrong is easy and silent: a trade placed at 15:32:18 on a 15m
chart belongs to the 15:30 candle, and attaching it to 15:15 or 15:45 produces
an entry price that matches nothing on the chart, with no error anywhere.

So the lookup here is strict. It aligns the execution timestamp to the same
epoch boundary the chart and the aggregator use
(``candle_aggregate.bucket_start``), and returns a candle only when a candle
with exactly that bucket exists, is itself valid, and genuinely spans the
execution instant. When no candle qualifies the answer is ``None`` with a
reason -- never a neighbouring bar, never a fabricated one.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence

from ..schemas import Candle
from .candle_aggregate import bucket_start, validate_candle
from .chart_service import normalize_timeframe, timeframe_period

logger = logging.getLogger("app.entry_candle")

__all__ = [
    "EntryCandleResult",
    "find_entry_candle",
    "entry_candle_fields",
]


@dataclass(frozen=True)
class EntryCandleResult:
    """The candle a trade entered on, plus why there isn't one when there isn't."""

    candle: Optional[Candle]
    #: Epoch-aligned start of the bucket the execution instant falls in.
    bucket: Optional[int]
    #: Seconds per bar for the trade's timeframe.
    period: int
    #: None on success, otherwise a short reason suitable for logging.
    reason: Optional[str]

    @property
    def found(self) -> bool:
        return self.candle is not None


def find_entry_candle(
    candles: Optional[Sequence[Candle]],
    execution_ts: Optional[float],
    timeframe: Optional[str],
) -> EntryCandleResult:
    """Find the candle whose bucket contains ``execution_ts``.

    ``candles`` must already be on ``timeframe``'s period; this function does
    not resample, because resampling here would create a second definition of
    what a bar is and the whole point is that the chart and the trade agree.
    """
    key = normalize_timeframe(timeframe or "")
    if key is None:
        return EntryCandleResult(None, None, 0, f"unknown timeframe {timeframe!r}")

    period = timeframe_period(key)
    if period <= 0:
        return EntryCandleResult(None, None, period, "non-positive period")

    if execution_ts is None or not math.isfinite(execution_ts) or execution_ts <= 0:
        return EntryCandleResult(None, None, period, "no execution timestamp")

    if not candles:
        return EntryCandleResult(None, None, period, "no candles available")

    target = bucket_start(execution_ts, period)

    # Exact-timestamp match first: that is the common case, where the provider
    # returns bars already aligned to the period.
    by_ts = {int(c.timestamp): c for c in candles if math.isfinite(c.timestamp)}
    candidate = by_ts.get(int(target))

    if candidate is None:
        # Fall back to a bucket comparison so a provider whose bars are offset
        # by a constant (a real thing on some feeds) still resolves correctly,
        # rather than silently attaching the trade to nothing.
        for candle in candles:
            if not math.isfinite(candle.timestamp):
                continue
            if bucket_start(candle.timestamp, period) == target:
                candidate = candle
                break

    if candidate is None:
        # The bar the trade entered on is simply not in this window. Reporting
        # that is better than quietly using the nearest bar, which would put a
        # marker on a candle whose OHLC has nothing to do with the entry.
        newest = max((c.timestamp for c in candles if math.isfinite(c.timestamp)),
                     default=None)
        return EntryCandleResult(
            None, int(target), period,
            f"no candle for bucket {int(target)}"
            + (f" (newest available {int(newest)})" if newest is not None else ""),
        )

    reason = validate_candle(candidate)
    if reason is not None:
        return EntryCandleResult(None, int(target), period, f"invalid candle: {reason}")

    # The bar must actually span the execution instant. With aligned bars this
    # follows from the bucket match; checking it explicitly is what catches a
    # mis-aligned feed instead of trusting the arithmetic.
    start = float(candidate.timestamp)
    if not (start <= execution_ts < start + period):
        return EntryCandleResult(
            None, int(target), period,
            f"candle at {int(start)} does not span {execution_ts}",
        )

    return EntryCandleResult(candidate, int(target), period, None)


def entry_candle_fields(result: EntryCandleResult) -> dict:
    """Flatten a result into the ``TradeRecord`` field names.

    Always returns every key, so a failed lookup writes explicit ``None``s
    rather than leaving whatever a previous trade happened to store. Callers
    can spread this straight into a model.
    """
    candle = result.candle
    if candle is None:
        return {
            "entry_candle_timestamp": None,
            "entry_candle_open": None,
            "entry_candle_high": None,
            "entry_candle_low": None,
            "entry_candle_close": None,
        }
    return {
        "entry_candle_timestamp": float(candle.timestamp),
        "entry_candle_open": float(candle.open),
        "entry_candle_high": float(candle.high),
        "entry_candle_low": float(candle.low),
        "entry_candle_close": float(candle.close),
    }
