"""Timeframe aggregation and validation for chart candles.

Why this exists
---------------
``provider.get_candles()`` is the single source of truth for candles, and it
already serves every timeframe in ``TIMEFRAMES`` natively. This module is the
fallback for the one case it cannot cover: a provider that does not offer a
requested timeframe (the Binance path has no native 2m, for instance) but does
offer a lower one.

It is deliberately narrow. It does not fetch, cache or store anything, and it
never invents a candle. Every output bucket contains at least one real input
candle; a gap in the source stays a gap in the output, because filling it would
mean fabricating price data.

Rules enforced here
-------------------
* Bucket boundaries are aligned to the epoch: ``bucket = ts // period * period``.
  Folding the same tick twice is therefore idempotent, which is what makes
  reconnects safe.
* ``open`` comes from the earliest input in the bucket and ``close`` from the
  latest, so re-ordering or re-delivering inputs cannot change the result.
* The bucket still in progress is reported separately and is never mixed into
  the finalized history. A partial candle is not a historical candle.
* Invalid candles are rejected with a reason rather than silently dropped, so
  corrupt data surfaces instead of being drawn.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from ..schemas import Candle

#: Smallest period we will aggregate from, in seconds.
MIN_SOURCE_PERIOD = 1


def bucket_start(timestamp: float, period: int) -> int:
    """Epoch-aligned start of the bucket containing ``timestamp``.

    Returns an int so bucket keys compare and hash exactly, and so callers
    never accumulate float drift across a long session.
    """
    if period < MIN_SOURCE_PERIOD:
        raise ValueError(f"period must be >= {MIN_SOURCE_PERIOD}, got {period}")
    return int(timestamp // period) * period


def is_bucket_closed(bucket_ts: int, period: int, now: float) -> bool:
    """True when the bucket ending at ``bucket_ts + period`` is in the past."""
    return (bucket_ts + period) <= now


# ── validation ─────────────────────────────────────────────────────────────

def validate_candle(candle: Candle) -> Optional[str]:
    """Return a human-readable reason why this candle is unusable, or None.

    Checks the things that would make a chart lie: non-finite prices, a high
    below its low, an open or close outside the high/low range, and a missing
    or non-positive timestamp.
    """
    ts = candle.timestamp
    if ts is None or not isinstance(ts, (int, float)):
        return "missing timestamp"
    if not math.isfinite(ts) or ts <= 0:
        return f"invalid timestamp ({ts!r})"

    for name in ("open", "high", "low", "close"):
        value = getattr(candle, name)
        if value is None or not isinstance(value, (int, float)):
            return f"missing {name}"
        if not math.isfinite(value):
            return f"non-finite {name} ({value!r})"
        if value <= 0:
            return f"non-positive {name} ({value!r})"

    if candle.high < candle.low:
        return f"high {candle.high} < low {candle.low}"
    if not (candle.low <= candle.open <= candle.high):
        return f"open {candle.open} outside [{candle.low}, {candle.high}]"
    if not (candle.low <= candle.close <= candle.high):
        return f"close {candle.close} outside [{candle.low}, {candle.high}]"
    return None


@dataclass
class NormalizationResult:
    """Outcome of cleaning a raw candle list.

    ``rejected`` keeps the reason for every dropped candle so callers can log
    it once (not per tick) instead of silently drawing bad data.
    """

    candles: List[Candle] = field(default_factory=list)
    rejected: List[Tuple[Candle, str]] = field(default_factory=list)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


def normalize_candles(candles: Iterable[Candle]) -> NormalizationResult:
    """Validate, sort by timestamp, and collapse duplicate timestamps.

    Duplicate handling: for a repeated timestamp the LAST occurrence wins. Live
    updates re-send the forming candle with a revised close, so the newest
    version of a bucket is the correct one to keep.
    """
    result = NormalizationResult()
    by_ts: dict = {}

    for candle in candles:
        reason = validate_candle(candle)
        if reason is not None:
            result.rejected.append((candle, reason))
            continue
        key = int(candle.timestamp)
        by_ts[key] = candle

    ordered = [by_ts[k] for k in sorted(by_ts)]
    result.candles = ordered
    return result


# ── aggregation ────────────────────────────────────────────────────────────

@dataclass
class AggregationResult:
    """Finalized buckets plus the one still forming.

    ``closed`` is safe to treat as history. ``partial`` is the in-progress
    bucket and must be rendered as the live candle, never appended to history
    as though it had finished.
    """

    closed: List[Candle] = field(default_factory=list)
    partial: Optional[Candle] = None

    @property
    def all(self) -> List[Candle]:
        return self.closed + ([self.partial] if self.partial else [])


def aggregate(
    candles: Sequence[Candle],
    period: int,
    *,
    now: Optional[float] = None,
) -> AggregationResult:
    """Fold lower-timeframe candles into ``period``-second buckets.

    ``now`` decides which bucket is still forming. When it is None the newest
    input timestamp is used, which keeps the function deterministic and easy to
    test.

    Nothing is fabricated: a bucket appears in the output only if at least one
    input candle fell inside it.
    """
    if period < MIN_SOURCE_PERIOD:
        raise ValueError(f"period must be >= {MIN_SOURCE_PERIOD}, got {period}")

    normalized = normalize_candles(candles)
    if not normalized.candles:
        return AggregationResult()

    reference = now if now is not None else normalized.candles[-1].timestamp

    # Group first so that open/close depend on real ordering, not on the order
    # the inputs happened to arrive in.
    buckets: dict = {}
    for candle in normalized.candles:
        key = bucket_start(candle.timestamp, period)
        buckets.setdefault(key, []).append(candle)

    closed: List[Candle] = []
    partial: Optional[Candle] = None

    for key in sorted(buckets):
        group = buckets[key]
        merged = _merge_bucket(key, group)
        if is_bucket_closed(key, period, reference):
            closed.append(merged)
        else:
            # Only the newest bucket can be in progress; anything older that is
            # not closed means the source has a gap, and we do not paper over
            # it by inventing a candle.
            if partial is None:
                partial = merged
            else:
                closed.append(merged)

    return AggregationResult(closed=closed, partial=partial)


def _merge_bucket(bucket_ts: int, group: Sequence[Candle]) -> Candle:
    """Combine the candles of one bucket into a single OHLC candle.

    ``group`` must already be sorted by timestamp, which ``normalize_candles``
    guarantees.
    """
    first, last = group[0], group[-1]
    high = max(c.high for c in group)
    low = min(c.low for c in group)

    # Volume is only meaningful if EVERY input carried real volume. A single
    # synthetic (tick-count) input makes the aggregate synthetic, because a sum
    # of tick counts is not order-book volume.
    all_real = all(getattr(c, "volume_source", "real") == "real" for c in group)
    volume = float(sum(c.volume for c in group)) if all_real else 0.0

    return Candle(
        timestamp=float(bucket_ts),
        open=first.open,
        high=high,
        low=low,
        close=last.close,
        volume=volume,
        volume_source="real" if all_real else "synthetic",
    )


def fold_into(
    candles: Sequence[Candle],
    target_period: int,
    source_period: int,
    *,
    now: Optional[float] = None,
) -> AggregationResult:
    """Aggregate only when the source is finer than the target.

    Returns the input untouched (already normalized) when the two periods match
    or the source is coarser, rather than silently producing nonsense.
    """
    if source_period <= 0:
        raise ValueError(f"source_period must be positive, got {source_period}")
    if target_period <= source_period:
        normalized = normalize_candles(candles)
        partial = None
        closed = list(normalized.candles)
        if closed:
            reference = now if now is not None else closed[-1].timestamp
            last_ts = int(closed[-1].timestamp)
            if not is_bucket_closed(last_ts, target_period, reference):
                partial = closed.pop()
        return AggregationResult(closed=closed, partial=partial)
    return aggregate(candles, target_period, now=now)
