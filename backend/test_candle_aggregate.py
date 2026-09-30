"""Tests for chart candle aggregation, normalization and validation.

These cover the cases the chart brief calls out: candle boundaries, duplicate
prevention, live update of the forming candle, rollover at a boundary, invalid
candle rejection, and the explicit requirement that no candle is fabricated.

Nothing here touches the network, the trading engine or the decision engine.
"""

from __future__ import annotations

import math

import pytest

from app.schemas import Candle
from app.services.candle_aggregate import (
    AggregationResult,
    aggregate,
    bucket_start,
    fold_into,
    is_bucket_closed,
    normalize_candles,
    validate_candle,
)

#: A 15m-aligned epoch second, used as the reference bucket.
BASE = 1759201200
P15M = 900
P1M = 60


def C(ts, o=1.0, h=None, l=None, cl=1.0, vol=0.0, src="synthetic") -> Candle:
    """Build a candle, deriving high/low from open/close when not given.

    Deriving by default matters: a literal high/low that does not contain the
    open and close is *invalid* data, so the validator would reject it and the
    test would fail for a reason that has nothing to do with the behaviour
    under test. Pass h/l explicitly only when testing rejection.
    """
    lo = l if l is not None else min(o, cl)
    hi = h if h is not None else max(o, cl)
    return Candle(timestamp=ts, open=o, high=hi, low=lo, close=cl,
                  volume=vol, volume_source=src)


# ── 1. candle boundary calculation ─────────────────────────────────────────

def test_bucket_start_is_epoch_aligned():
    assert bucket_start(BASE, P15M) == BASE
    assert bucket_start(BASE + 1, P15M) == BASE
    assert bucket_start(BASE + 899, P15M) == BASE
    assert bucket_start(BASE + 900, P15M) == BASE + 900, "next boundary"


def test_bucket_start_returns_an_int():
    """Float keys would drift across a long session."""
    assert isinstance(bucket_start(BASE + 0.5, P15M), int)


def test_bucket_start_rejects_a_non_positive_period():
    with pytest.raises(ValueError):
        bucket_start(BASE, 0)


def test_is_bucket_closed():
    assert is_bucket_closed(BASE, P15M, BASE + 900) is True
    assert is_bucket_closed(BASE, P15M, BASE + 899) is False


# ── 2. live update of the forming candle ───────────────────────────────────

def test_ticks_in_the_same_bucket_update_one_candle():
    """The brief's required case: 15m selected, ticks inside the same candle."""
    src = [
        C(BASE, o=1.1430, h=1.1435, l=1.1428, cl=1.1432),
        C(BASE + 60, o=1.1432, h=1.1440, l=1.1430, cl=1.1438),
        C(BASE + 120, o=1.1438, h=1.1442, l=1.1436, cl=1.1440),
    ]

    result = aggregate(src, P15M, now=BASE + 130)

    assert len(result.closed) == 0, "nothing has closed yet"
    assert result.partial is not None
    p = result.partial
    assert p.timestamp == BASE
    assert p.open == 1.1430, "open is the earliest tick"
    assert p.close == 1.1440, "close is the latest tick"
    assert p.high == 1.1442
    assert p.low == 1.1428


def test_forming_candle_is_never_reported_as_closed():
    result = aggregate([C(BASE), C(BASE + 60)], P15M, now=BASE + 70)

    assert result.closed == []
    assert result.partial is not None


# ── 3. rollover at the boundary ────────────────────────────────────────────

def test_crossing_the_boundary_finalizes_and_starts_a_new_candle():
    src = [
        C(BASE, cl=1.1432),
        C(BASE + 60, cl=1.1438),
        C(BASE + 120, cl=1.1440),
        C(BASE + 900, cl=1.1443),
        C(BASE + 960, cl=1.1446),
    ]

    result = aggregate(src, P15M, now=BASE + 970)

    assert len(result.closed) == 1, "exactly one finalized candle"
    assert result.closed[0].timestamp == BASE
    assert result.closed[0].close == 1.1440
    assert result.partial is not None
    assert result.partial.timestamp == BASE + 900
    assert result.partial.close == 1.1446


def test_no_duplicate_timestamps_across_a_rollover():
    src = [C(BASE + i * 60) for i in range(20)]  # spans two 15m buckets

    result = aggregate(src, P15M, now=BASE + 1150)

    stamps = [int(c.timestamp) for c in result.all]
    assert len(stamps) == len(set(stamps)), f"duplicate timestamps: {stamps}"
    assert stamps == sorted(stamps), "candles must be ordered"


def test_no_missing_and_no_fabricated_candle_at_the_boundary():
    src = [C(BASE), C(BASE + 60), C(BASE + 900), C(BASE + 960)]

    result = aggregate(src, P15M, now=BASE + 970)

    assert [int(c.timestamp) for c in result.all] == [BASE, BASE + 900]


# ── 4. duplicate prevention ────────────────────────────────────────────────

def test_replaying_the_same_ticks_is_idempotent():
    """Reconnects re-send history; folding twice must not change the result."""
    src = [C(BASE, cl=1.1432), C(BASE + 60, cl=1.1438),
           C(BASE + 900, cl=1.1443)]

    once = aggregate(src, P15M, now=BASE + 910)
    twice = aggregate(src + src, P15M, now=BASE + 910)

    def shape(r: AggregationResult):
        return [(int(c.timestamp), c.open, c.high, c.low, c.close)
                for c in r.all]

    assert shape(once) == shape(twice)


def test_normalize_collapses_duplicate_timestamps_last_wins():
    dup = [C(BASE + 60, cl=1.05), C(BASE, cl=1.10), C(BASE + 60, cl=1.15)]

    result = normalize_candles(dup)

    assert [int(c.timestamp) for c in result.candles] == [BASE, BASE + 60]
    assert result.candles[-1].close == 1.15, "the newest revision must win"
    assert result.rejected_count == 0


def test_an_invalid_duplicate_does_not_clobber_a_valid_candle():
    # explicit range so the second candle really is invalid
    mixed = [C(BASE, cl=1.10), C(BASE, h=1.1, l=0.9, cl=9.99)]

    result = normalize_candles(mixed)

    assert len(result.candles) == 1
    assert result.candles[0].close == 1.10
    assert result.rejected_count == 1


def test_normalize_sorts_unordered_input():
    result = normalize_candles([C(BASE + 120), C(BASE), C(BASE + 60)])
    assert [int(c.timestamp) for c in result.candles] == [BASE, BASE + 60, BASE + 120]


# ── 5. invalid candle rejection ────────────────────────────────────────────

@pytest.mark.parametrize("kwargs,expected", [
    ({"o": float("nan")}, "non-finite open"),
    ({"h": float("inf"), "l": 0.9}, "non-finite high"),
    ({"h": 0.5, "l": 0.9}, "high 0.5 < low 0.9"),
    # explicit high/low, so the close really is out of range
    ({"h": 1.1, "l": 0.9, "cl": 2.0}, "close 2.0 outside"),
    ({"h": 1.1, "l": 0.9, "o": 2.0}, "open 2.0 outside"),
    ({"o": 0.0}, "non-positive open"),
    # a negative close also drags the derived low negative, and the validator
    # reports the low first; either way the candle is rejected
    ({"cl": -1.0}, "non-positive low"),
    ({"h": 1.1, "l": 0.9, "cl": -1.0}, "non-positive close"),
])
def test_validate_candle_rejects_bad_data(kwargs, expected):
    base = dict(o=1.0, cl=1.0)
    base.update(kwargs)
    reason = validate_candle(C(BASE, **base))
    assert reason is not None, f"{kwargs} was accepted but should be rejected"
    assert expected in reason


def test_validate_candle_accepts_good_data():
    assert validate_candle(C(BASE)) is None


def test_validate_candle_rejects_a_non_positive_timestamp():
    assert validate_candle(C(0)) is not None
    assert validate_candle(C(-100)) is not None


def test_corrupt_candles_are_excluded_not_drawn():
    src = [C(BASE), C(BASE + 60, h=0.1, l=0.9), C(BASE + 120)]

    result = aggregate(src, P15M, now=BASE + 130)

    # The bad candle is dropped, but its bucket still exists from the good ones.
    assert result.partial is not None
    assert result.partial.timestamp == BASE
    norm = normalize_candles(src)
    assert norm.rejected_count == 1


def test_an_all_invalid_input_produces_nothing():
    result = aggregate([C(BASE, h=0.1, l=0.9)], P15M, now=BASE + 10)
    assert result.closed == []
    assert result.partial is None


# ── 6. no fabricated candles ───────────────────────────────────────────────

def test_a_gap_in_the_source_is_not_filled():
    """A missing bucket stays missing; filling it would invent price data."""
    src = [C(BASE), C(BASE + 2700)]

    result = aggregate(src, P15M, now=BASE + 2710)

    assert [int(c.timestamp) for c in result.all] == [BASE, BASE + 2700]
    assert len(result.all) == 2, "must not interpolate the two missing buckets"


def test_empty_input_produces_no_candles():
    result = aggregate([], P15M, now=BASE)
    assert result.closed == []
    assert result.partial is None


# ── 7. volume semantics ────────────────────────────────────────────────────

def test_synthetic_volume_never_becomes_real():
    """Quotex volume is a tick-count proxy; presenting it as volume would lie."""
    result = aggregate([C(BASE, vol=5, src="synthetic")], P15M, now=BASE + 10)
    assert result.partial.volume_source == "synthetic"
    assert result.partial.volume == 0.0


def test_real_volume_is_summed():
    result = aggregate([C(BASE, vol=5, src="real"),
                        C(BASE + 60, vol=2, src="real")], P15M, now=BASE + 70)
    assert result.partial.volume_source == "real"
    assert result.partial.volume == 7.0


def test_one_synthetic_input_makes_the_whole_bucket_synthetic():
    result = aggregate([C(BASE, vol=5, src="real"),
                        C(BASE + 60, vol=2, src="synthetic")],
                       P15M, now=BASE + 70)
    assert result.partial.volume_source == "synthetic"
    assert result.partial.volume == 0.0


# ── 8. fold_into ───────────────────────────────────────────────────────────

def test_fold_into_is_a_noop_when_periods_match():
    src = [C(BASE), C(BASE + 900)]
    result = fold_into(src, P15M, P15M, now=BASE + 910)
    assert len(result.all) == 2


def test_fold_into_aggregates_a_finer_source():
    finer = [C(BASE + i * P1M) for i in range(15)]  # 15 x 1m == one 15m

    result = fold_into(finer, P15M, P1M, now=BASE + 850)

    assert len(result.all) == 1
    assert result.partial.timestamp == BASE


def test_fold_into_does_not_aggregate_a_coarser_source():
    """Folding 15m into 1m would be nonsense; return the input instead."""
    src = [C(BASE), C(BASE + 900)]
    result = fold_into(src, P1M, P15M, now=BASE + 910)
    assert len(result.all) == 2


def test_fold_into_rejects_a_non_positive_source_period():
    with pytest.raises(ValueError):
        fold_into([C(BASE)], P15M, 0)


# ── 9. determinism ─────────────────────────────────────────────────────────

def test_aggregation_is_deterministic_for_the_same_input():
    src = [C(BASE + i * 60, cl=1.0 + i * 0.001) for i in range(20)]

    a = aggregate(src, P15M, now=BASE + 1150)
    b = aggregate(src, P15M, now=BASE + 1150)

    assert [(int(c.timestamp), c.open, c.high, c.low, c.close)
            for c in a.all] == [(int(c.timestamp), c.open, c.high, c.low, c.close)
                                for c in b.all]


def test_reordered_input_gives_the_same_candles():
    """open/close come from real timestamps, not arrival order."""
    src = [C(BASE + i * 60, cl=1.0 + i * 0.001) for i in range(10)]
    shuffled = list(reversed(src))

    a = aggregate(src, P15M, now=BASE + 550)
    b = aggregate(shuffled, P15M, now=BASE + 550)

    assert a.partial.open == b.partial.open
    assert a.partial.close == b.partial.close


def test_result_has_no_non_finite_values():
    src = [C(BASE + i * 60, cl=1.0 + i * 0.001) for i in range(10)]
    for c in aggregate(src, P15M, now=BASE + 550).all:
        for name in ("timestamp", "open", "high", "low", "close", "volume"):
            assert math.isfinite(getattr(c, name)), f"{name} is not finite"
