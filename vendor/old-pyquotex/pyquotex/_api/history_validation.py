"""Validation, metrics and circuit-breaker primitives for the history pipeline.

Everything here is pure/stateless except HistoryMetrics and
HistoryCircuitBreaker, which are explicitly instance-scoped.

DESIGN RULE THAT GOVERNS THIS WHOLE MODULE
-------------------------------------------
Never invent market data. Nothing in here interpolates, back-fills,
repeats a previous close, or synthesises a candle for a detected gap.
A gap is *reported*; the candles either side of it are returned exactly
as the broker sent them. Validation rejects only what is demonstrably
impossible (NaN, inf, high < low, timestamps far in the future), never
what is merely surprising (a weekend gap, a halted market, a broker
that timestamps slightly off).
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable


# --------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------- #
@dataclass
class CandleQualityReport:
    """What validation observed. Advisory -- callers log/meter it; it
    never causes valid candles to be dropped."""

    received: int = 0
    accepted: int = 0
    rejected_invalid_ohlc: int = 0
    rejected_bad_timestamp: int = 0
    rejected_future: int = 0
    rejected_malformed: int = 0
    duplicates_identical: int = 0
    duplicates_conflicting: int = 0
    was_unordered: bool = False
    spacing_anomalies: int = 0
    gaps: list[tuple[int, int]] = field(default_factory=list)  # (after_ts, before_ts)
    missing_intervals: int = 0

    @property
    def rejected(self) -> int:
        return (
            self.rejected_invalid_ohlc
            + self.rejected_bad_timestamp
            + self.rejected_future
            + self.rejected_malformed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejected_invalid_ohlc": self.rejected_invalid_ohlc,
            "rejected_bad_timestamp": self.rejected_bad_timestamp,
            "rejected_future": self.rejected_future,
            "rejected_malformed": self.rejected_malformed,
            "duplicates_identical": self.duplicates_identical,
            "duplicates_conflicting": self.duplicates_conflicting,
            "was_unordered": self.was_unordered,
            "spacing_anomalies": self.spacing_anomalies,
            "gap_count": len(self.gaps),
            "missing_intervals": self.missing_intervals,
        }


class TransientHistoryError(Exception):
    """Worth retrying: timeout, socket blip, transient empty response."""


class PermanentHistoryError(Exception):
    """Not worth retrying: malformed/corrupt payload. Skip the chunk."""


class CircuitOpenError(TransientHistoryError):
    """Breaker is open; the request was not sent. Transient by nature --
    the breaker closes again after its cooldown."""


# --------------------------------------------------------------------- #
# Envelope validation
# --------------------------------------------------------------------- #
def validate_envelope(
    raw: Any,
    *,
    expected_asset: str | None = None,
    expected_index: Any = None,
) -> tuple[bool, str]:
    """Check the response wrapper before parsing candles out of it.

    LENIENCY IS DELIBERATE (spec §13): Quotex does not always echo
    `asset` or `index` back. A response is NOT rejected for *missing*
    optional metadata -- only for metadata that is present and
    *contradicts* the request, which is the case that indicates a
    misrouted response.
    """
    if raw is None:
        return False, "no response"
    if not isinstance(raw, dict):
        return False, f"unexpected response type {type(raw).__name__}"

    if "data" not in raw and "candles" not in raw:
        return False, "response has neither 'data' nor 'candles'"

    if expected_asset is not None:
        got_asset = raw.get("asset")
        if got_asset is not None and got_asset != expected_asset:
            return False, f"asset mismatch: wanted {expected_asset!r}, got {got_asset!r}"

    if expected_index is not None:
        got_index = raw.get("index")
        if got_index is not None and got_index != expected_index:
            return False, f"index mismatch: wanted {expected_index!r}, got {got_index!r}"

    return True, ""


# --------------------------------------------------------------------- #
# Candle normalisation + integrity
# --------------------------------------------------------------------- #
def _finite(value: Any) -> float | None:
    """float() that refuses NaN/inf and anything non-numeric."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def normalize_candle(raw: Any) -> dict[str, Any] | None:
    """Normalise one broker candle into the canonical dict.

    Returns None for anything that cannot be represented faithfully.
    Never fabricates a missing OHLC component -- a candle missing a
    field is dropped, not guessed at.

    Accepts the two shapes Quotex actually sends:
      * list  [time, open, close, high, low, ...]
      * dict  {"time":..., "open":..., "close":..., "high":..., "low":...}
    """
    if isinstance(raw, (list, tuple)):
        if len(raw) < 5:
            return None
        ts_raw, o_raw, c_raw, h_raw, l_raw = raw[0], raw[1], raw[2], raw[3], raw[4]
    elif isinstance(raw, dict):
        if "time" not in raw:
            return None
        ts_raw = raw.get("time")
        o_raw, c_raw = raw.get("open"), raw.get("close")
        h_raw, l_raw = raw.get("high"), raw.get("low")
        if o_raw is None or c_raw is None or h_raw is None or l_raw is None:
            return None
    else:
        return None

    ts_f = _finite(ts_raw)
    if ts_f is None:
        return None
    o = _finite(o_raw)
    c = _finite(c_raw)
    h = _finite(h_raw)
    lo = _finite(l_raw)
    if o is None or c is None or h is None or lo is None:
        return None

    out = {"time": int(ts_f), "open": o, "close": c, "high": h, "low": lo}
    # Carry through any extra broker fields on dict-shaped candles
    # (volume, ticks, ...) without interpreting them.
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k not in out:
                out[k] = v
    return out


def check_ohlc_integrity(candle: dict[str, Any]) -> tuple[bool, str]:
    """Reject only genuinely impossible bars."""
    o, c = candle["open"], candle["close"]
    h, lo = candle["high"], candle["low"]
    if h < lo:
        return False, "high < low"
    if h < o or h < c:
        return False, "high below open/close"
    if lo > o or lo > c:
        return False, "low above open/close"
    return True, ""


def check_timestamp(
    candle: dict[str, Any],
    *,
    min_plausible: int,
    future_tolerance: float,
    now: float | None = None,
) -> tuple[bool, str]:
    """Reject impossible timestamps only.

    A currently-forming candle sits within `future_tolerance` of now and
    is explicitly allowed -- rejecting it would silently drop the most
    recent bar, which is the one the trading engine cares about most.
    """
    ts = candle["time"]
    if ts < min_plausible:
        return False, "timestamp implausibly old"
    reference = time.time() if now is None else now
    if ts > reference + future_tolerance:
        return False, "timestamp in the future"
    return True, ""


# --------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------- #
_OHLC_KEYS = ("open", "close", "high", "low")


def _same_ohlc(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in _OHLC_KEYS)


def deduplicate(
    candles: Iterable[dict[str, Any]], report: CandleQualityReport
) -> dict[int, dict[str, Any]]:
    """Deduplicate by timestamp with a DETERMINISTIC conflict policy.

    Identical OHLC for the same timestamp: keep one, count it, stay quiet.

    Conflicting OHLC for the same timestamp: this is a real data-quality
    event and gets counted separately so it shows up in metrics. The
    policy is last-writer-wins in iteration order, which for this
    pipeline means the most recently fetched response wins -- chosen
    because it is deterministic and reproducible, not because later data
    is known to be better. It is never random.
    """
    out: dict[int, dict[str, Any]] = {}
    for candle in candles:
        ts = candle["time"]
        existing = out.get(ts)
        if existing is None:
            out[ts] = candle
            continue
        if _same_ohlc(existing, candle):
            report.duplicates_identical += 1
        else:
            report.duplicates_conflicting += 1
        out[ts] = candle
    return out


# --------------------------------------------------------------------- #
# Spacing / gap analysis
# --------------------------------------------------------------------- #
def analyse_spacing(
    ordered: list[dict[str, Any]],
    period: int,
    report: CandleQualityReport,
    *,
    spacing_tolerance: float,
    gap_multiplier: float,
) -> None:
    """Record spacing anomalies and gaps. Mutates `report`; never
    mutates or drops candles, and never fabricates a missing bar."""
    if period <= 0 or len(ordered) < 2:
        return
    low = period * (1.0 - spacing_tolerance)
    high = period * (1.0 + spacing_tolerance)
    gap_threshold = period * gap_multiplier
    for prev, cur in zip(ordered, ordered[1:]):
        delta = cur["time"] - prev["time"]
        if delta <= 0:
            # Should be impossible after dedup+sort; counted, not fatal.
            report.spacing_anomalies += 1
            continue
        if delta > gap_threshold:
            report.gaps.append((prev["time"], cur["time"]))
            report.missing_intervals += max(0, int(delta // period) - 1)
        elif not (low <= delta <= high):
            report.spacing_anomalies += 1


def validate_and_clean(
    raw_candles: Iterable[Any],
    period: int,
    *,
    min_plausible_timestamp: int,
    future_tolerance_seconds: float,
    spacing_tolerance: float,
    gap_multiplier: float,
    now: float | None = None,
) -> tuple[list[dict[str, Any]], CandleQualityReport]:
    """Full per-response pipeline: normalise -> integrity -> timestamp ->
    record ordering -> dedup -> sort -> spacing/gap analysis.

    Returns only real, broker-provided candles.
    """
    report = CandleQualityReport()
    accepted: list[dict[str, Any]] = []
    last_ts: int | None = None

    for raw in raw_candles:
        report.received += 1
        candle = normalize_candle(raw)
        if candle is None:
            report.rejected_malformed += 1
            continue
        ok, _reason = check_ohlc_integrity(candle)
        if not ok:
            report.rejected_invalid_ohlc += 1
            continue
        ok, reason = check_timestamp(
            candle,
            min_plausible=min_plausible_timestamp,
            future_tolerance=future_tolerance_seconds,
            now=now,
        )
        if not ok:
            if reason == "timestamp in the future":
                report.rejected_future += 1
            else:
                report.rejected_bad_timestamp += 1
            continue

        # Record ordering BEFORE sorting, so a broker sending out-of-order
        # data stays visible instead of being hidden by the sort.
        if last_ts is not None and candle["time"] < last_ts:
            report.was_unordered = True
        last_ts = candle["time"]
        accepted.append(candle)

    deduped = deduplicate(accepted, report)
    ordered = sorted(deduped.values(), key=lambda c: c["time"])
    report.accepted = len(ordered)
    analyse_spacing(
        ordered, period, report,
        spacing_tolerance=spacing_tolerance,
        gap_multiplier=gap_multiplier,
    )
    return ordered, report


# --------------------------------------------------------------------- #
# Metrics (bounded memory)
# --------------------------------------------------------------------- #
class HistoryMetrics:
    """Instance-scoped counters + a bounded latency ring buffer.

    Memory is O(metrics_latency_samples) forever -- there are no
    unbounded lists here, which matters because this object lives for
    the entire life of a Quotex instance (weeks).
    """

    __slots__ = ("_c", "_latencies", "_maxlen")

    _COUNTERS = (
        "requests_total", "requests_ok", "requests_failed",
        "timeouts", "retries", "circuit_rejections",
        "cache_hits", "cache_misses",
        "candles_received", "candles_accepted",
        "duplicates_identical", "duplicates_conflicting",
        "validation_failures", "spacing_anomalies", "gaps_detected",
        "missing_intervals", "unordered_responses",
        "correlation_mismatches", "chunks_abandoned",
    )

    def __init__(self, maxlen: int = 512) -> None:
        self._maxlen = maxlen
        self._c: dict[str, int] = {k: 0 for k in self._COUNTERS}
        self._latencies: deque[float] = deque(maxlen=maxlen)

    def incr(self, name: str, amount: int = 1) -> None:
        if amount and name in self._c:
            self._c[name] += amount

    def observe_latency(self, seconds: float) -> None:
        self._latencies.append(seconds)

    def absorb(self, report: CandleQualityReport) -> None:
        self.incr("candles_received", report.received)
        self.incr("candles_accepted", report.accepted)
        self.incr("validation_failures", report.rejected)
        self.incr("duplicates_identical", report.duplicates_identical)
        self.incr("duplicates_conflicting", report.duplicates_conflicting)
        self.incr("spacing_anomalies", report.spacing_anomalies)
        self.incr("gaps_detected", len(report.gaps))
        self.incr("missing_intervals", report.missing_intervals)
        if report.was_unordered:
            self.incr("unordered_responses")

    @staticmethod
    def _percentile(sorted_vals: list[float], pct: float) -> float:
        if not sorted_vals:
            return 0.0
        idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
        return sorted_vals[idx]

    def snapshot(self) -> dict[str, Any]:
        vals = sorted(self._latencies)
        return {
            **self._c,
            "latency_samples": len(vals),
            "latency_p50": round(self._percentile(vals, 50), 4),
            "latency_p95": round(self._percentile(vals, 95), 4),
            "latency_p99": round(self._percentile(vals, 99), 4),
            "latency_max": round(vals[-1], 4) if vals else 0.0,
        }

    def reset(self) -> None:
        """Clears counters only. Deliberately touches nothing outside
        this object -- resetting metrics must never disturb trading."""
        for k in self._c:
            self._c[k] = 0
        self._latencies.clear()


# --------------------------------------------------------------------- #
# Circuit breaker (instance-scoped)
# --------------------------------------------------------------------- #
class HistoryCircuitBreaker:
    """Stops retry storms against a broker that is clearly unhealthy.

    Never latches permanently: after `cooldown` it half-opens and lets a
    single probe through, and any success closes it fully. It guards the
    history pipeline only -- an open breaker here does not block
    streaming, orders, or anything else on the socket.
    """

    __slots__ = ("_threshold", "_cooldown", "_failures", "_opened_at", "_enabled")

    def __init__(self, threshold: int, cooldown: float, enabled: bool = True) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._enabled = enabled
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allows(self, now: float | None = None) -> bool:
        if not self._enabled or self._opened_at is None:
            return True
        reference = time.monotonic() if now is None else now
        if reference - self._opened_at >= self._cooldown:
            # Half-open: let one probe through. Not closed yet -- only a
            # real success closes it.
            self._opened_at = None
            self._failures = self._threshold - 1
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, now: float | None = None) -> None:
        if not self._enabled:
            return
        self._failures += 1
        if self._failures >= self._threshold and self._opened_at is None:
            self._opened_at = time.monotonic() if now is None else now

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "open": self.is_open,
            "consecutive_failures": self._failures,
            "threshold": self._threshold,
        }


__all__ = [
    "CandleQualityReport",
    "TransientHistoryError",
    "PermanentHistoryError",
    "CircuitOpenError",
    "validate_envelope",
    "normalize_candle",
    "check_ohlc_integrity",
    "check_timestamp",
    "deduplicate",
    "analyse_spacing",
    "validate_and_clean",
    "HistoryMetrics",
    "HistoryCircuitBreaker",
]
