"""Chart-specific adaptation of the existing candle pipeline.

This module does not fetch, store or generate market data. It sits between the
existing ``provider.get_candles()`` -- already the single source of truth, used
by ``/api/candles`` and by the trading pipeline -- and the frontend chart, and
does three things only:

1. Translate the timeframe labels the UI shows (``1D``) into the ones the model
   uses (``1day``).
2. Normalize candles into the wire format the chart consumes, and reject any
   that would make the chart lie.
3. Report what it dropped, so corrupt data surfaces in one log line instead of
   being drawn silently.

It deliberately holds no credentials, no session material and no auth state. A
chart response carries OHLC and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from ..schemas import Candle
from .candle_aggregate import aggregate, normalize_candles
from .market import TIMEFRAMES

logger = logging.getLogger("app.chart")

#: UI label -> the timeframe key the provider and the rest of the model use.
#: Only labels that differ from the model need an entry; everything else passes
#: through unchanged. Kept minimal on purpose so this cannot drift into a
#: second timeframe model.
TIMEFRAME_ALIASES: Dict[str, str] = {
    "1d": "1day",
    "1D": "1day",
    "day": "1day",
    "daily": "1day",
}

#: Timeframes the chart offers. Every one is already served natively by
#: TIMEFRAMES; this list only decides what the selector shows and in what order.
CHART_TIMEFRAMES: List[str] = [
    "1m", "2m", "3m", "5m", "15m", "30m", "1h", "4h", "1day",
]

#: Cap on how many candles the endpoint will return. Unbounded limits are an
#: easy way to turn one request into a multi-megabyte response.
MAX_CANDLE_LIMIT = 1000
DEFAULT_CANDLE_LIMIT = 300


def normalize_timeframe(label: str) -> Optional[str]:
    """Map a UI timeframe label to a key the provider understands.

    Returns None when the label is not a timeframe this system knows, so the
    caller can return a clear 400 instead of asking the broker for something it
    cannot serve.
    """
    if not label or not isinstance(label, str):
        return None
    key = TIMEFRAME_ALIASES.get(label) or TIMEFRAME_ALIASES.get(label.lower())
    if key is None:
        candidate = label.lower()
        key = candidate if candidate in TIMEFRAMES else None
    return key


def timeframe_period(timeframe: str) -> int:
    """Seconds per candle for a normalized timeframe key."""
    return int(TIMEFRAMES.get(timeframe, 60))


def clamp_limit(limit: Optional[int]) -> int:
    """Keep the requested candle count inside sane bounds."""
    try:
        value = int(limit) if limit is not None else DEFAULT_CANDLE_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_CANDLE_LIMIT
    if value <= 0:
        return DEFAULT_CANDLE_LIMIT
    return min(value, MAX_CANDLE_LIMIT)


def to_chart_candle(candle: Candle) -> Dict[str, Any]:
    """Convert one Candle into the chart's wire format.

    Timestamps stay in seconds, matching every other timestamp in the system.

    Volume is sent as None when it is synthetic. On the Quotex path volume is a
    tick-count proxy, and the schema itself says indicators must treat it as
    neutral -- so publishing it as a volume number would present a tick count as
    order-book activity, which it is not.
    """
    synthetic = getattr(candle, "volume_source", "real") != "real"
    return {
        "timestamp": int(candle.timestamp),
        "open": float(candle.open),
        "high": float(candle.high),
        "low": float(candle.low),
        "close": float(candle.close),
        "volume": None if synthetic else float(candle.volume),
    }


def build_chart_payload(
    asset: str,
    timeframe: str,
    candles: Sequence[Candle],
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate, order, trim and serialize candles into the chart envelope.

    Invalid candles are dropped rather than passed through, and the count is
    reported so the caller can log it once. The response never contains
    credentials or session material -- only OHLC.
    """
    normalized = normalize_candles(candles)
    wanted = clamp_limit(limit)
    kept = normalized.candles[-wanted:]

    if normalized.rejected_count:
        # Log a summary, not one line per bad candle: a corrupt burst would
        # otherwise flood the log.
        first_reasons = ", ".join(
            sorted({reason for _c, reason in normalized.rejected})[:3]
        )
        logger.warning(
            "chart: dropped %d/%d invalid candles for %s %s (%s)",
            normalized.rejected_count, len(normalized.candles) + normalized.rejected_count,
            asset, timeframe, first_reasons,
        )

    return {
        "asset": asset,
        "timeframe": timeframe,
        "candles": [to_chart_candle(c) for c in kept],
        "rejected": normalized.rejected_count,
    }


def aggregate_to_timeframe(
    candles: Sequence[Candle],
    target_timeframe: str,
    source_timeframe: str,
) -> List[Candle]:
    """Fold a finer source timeframe into the requested one.

    Used only when the provider cannot serve the target natively. The bucket
    still forming is included -- the chart needs it as the live candle -- but it
    is produced from real inputs, never interpolated.
    """
    target = timeframe_period(target_timeframe)
    source = timeframe_period(source_timeframe)
    if source <= 0 or target <= source:
        return list(normalize_candles(candles).candles)
    return aggregate(candles, target).all
