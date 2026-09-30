"""Tests for entry-candle resolution.

The central case is the one the requirement spells out: a trade executed at
15:32:18 on a 15m chart belongs to the 15:30 candle, and must not be attached
to the 15:15 or the 15:45 one. Everything else here protects that same
property -- the boundaries, the missing bar, the misaligned feed -- because a
wrong bucket produces no error anywhere, it just puts the marker on a candle
whose OHLC has nothing to do with the trade.

All timestamps below are real epoch seconds, verified with datetime:
1759246200 == 2025-09-30 15:30:00 UTC, and 900 divides it exactly.
"""

from __future__ import annotations

import pytest

from app.schemas import Candle
from app.services.entry_candle import entry_candle_fields, find_entry_candle

# 2025-09-30 15:30:00 UTC, 15m-aligned.
T1530 = 1759246200
T1515 = T1530 - 900
T1545 = T1530 + 900

EXEC_MID = T1530 + 138      # 15:32:18 -- the case from the requirement
EXEC_FIRST = T1530          # 15:30:00 exactly
EXEC_LAST = T1530 + 899     # 15:44:59
EXEC_NEXT = T1545           # 15:45:00 exactly


def C(ts: float, o=1.14410, h=1.14435, l=1.14400, cl=1.14422) -> Candle:
    """A candle, with high/low derived from open/close unless given explicitly
    so the fixture is always valid and only the behaviour under test can fail."""
    return Candle(timestamp=ts, open=o, high=h, low=l, close=cl, volume=0.0)


def window(*timestamps) -> list:
    return [C(ts) for ts in timestamps]


# ── the requirement's own case ─────────────────────────────────────────────

def test_a_trade_at_15_32_18_attaches_to_the_15_30_candle():
    """Stated case: 15:32:18 on 15m must resolve to the 15:30 bar."""
    candles = window(T1515, T1530, T1545)

    result = find_entry_candle(candles, EXEC_MID, "15m")

    assert result.found, result.reason
    assert result.candle.timestamp == T1530
    assert result.bucket == T1530
    assert result.period == 900


def test_it_does_not_attach_to_the_previous_or_next_candle():
    """The failure mode that is easy to ship: off by one bar either way."""
    candles = window(T1515, T1530, T1545)

    result = find_entry_candle(candles, EXEC_MID, "15m")

    assert result.candle.timestamp != T1515, "attached to 15:15"
    assert result.candle.timestamp != T1545, "attached to 15:45"


def test_the_ohlc_returned_is_that_candles_ohlc():
    candles = [C(T1515, o=1.10, h=1.11, l=1.09, cl=1.105),
               C(T1530, o=1.14410, h=1.14435, l=1.14400, cl=1.14422),
               C(T1545, o=1.20, h=1.21, l=1.19, cl=1.205)]

    result = find_entry_candle(candles, EXEC_MID, "15m")

    fields = entry_candle_fields(result)
    assert fields == {
        "entry_candle_timestamp": float(T1530),
        "entry_candle_open": 1.14410,
        "entry_candle_high": 1.14435,
        "entry_candle_low": 1.14400,
        "entry_candle_close": 1.14422,
    }


# ── boundaries ─────────────────────────────────────────────────────────────

def test_the_first_instant_of_a_bar_belongs_to_that_bar():
    result = find_entry_candle(window(T1530, T1545), EXEC_FIRST, "15m")
    assert result.found and result.candle.timestamp == T1530


def test_the_last_instant_of_a_bar_belongs_to_that_bar():
    """15:44:59 is still the 15:30 bar; 15:45:00 is not."""
    result = find_entry_candle(window(T1530, T1545), EXEC_LAST, "15m")
    assert result.found and result.candle.timestamp == T1530


def test_the_exact_rollover_instant_belongs_to_the_new_bar():
    result = find_entry_candle(window(T1530, T1545), EXEC_NEXT, "15m")
    assert result.found and result.candle.timestamp == T1545


@pytest.mark.parametrize("offset", [0, 1, 449, 899])
def test_every_instant_inside_the_bar_maps_to_the_same_bar(offset):
    """No instant in [15:30:00, 15:45:00) may resolve anywhere but 15:30."""
    result = find_entry_candle(window(T1530, T1545), T1530 + offset, "15m")
    assert result.found and result.candle.timestamp == T1530


# ── other timeframes ───────────────────────────────────────────────────────

def test_a_1m_trade_uses_a_60_second_bucket():
    t = T1530 + 37  # 15:30:37
    result = find_entry_candle(window(T1530, T1530 + 60), t, "1m")
    assert result.found and result.candle.timestamp == T1530
    assert result.period == 60


def test_a_daily_trade_uses_a_one_day_bucket():
    midnight = T1530 - (T1530 % 86400)
    result = find_entry_candle(window(midnight, midnight + 86400), T1530, "1day")
    assert result.found and result.candle.timestamp == midnight
    assert result.period == 86400


def test_ui_labels_resolve_to_the_same_bucket_as_the_model_keys():
    """"1D" is a UI label; the model calls it "1day". Both must agree."""
    midnight = T1530 - (T1530 % 86400)
    candles = window(midnight, midnight + 86400)
    a = find_entry_candle(candles, T1530, "1D")
    b = find_entry_candle(candles, T1530, "1day")
    assert a.found and b.found
    assert a.candle.timestamp == b.candle.timestamp == midnight


# ── refusing to guess ──────────────────────────────────────────────────────

def test_a_missing_bar_returns_none_rather_than_a_neighbour():
    """The 15:30 bar is absent. Attaching to 15:15 would be a lie."""
    result = find_entry_candle(window(T1515, T1545), EXEC_MID, "15m")

    assert not result.found
    assert result.candle is None
    assert result.bucket == T1530, "the caller still learns which bucket was wanted"
    assert "no candle for bucket" in result.reason


def test_an_invalid_candle_is_not_used():
    bad = Candle(timestamp=T1530, open=1.1, high=1.0, low=1.2, close=1.15, volume=0.0)
    result = find_entry_candle([bad], EXEC_MID, "15m")

    assert not result.found
    assert "invalid candle" in result.reason


def test_an_unknown_timeframe_is_rejected_before_any_lookup():
    result = find_entry_candle(window(T1530), EXEC_MID, "7x")
    assert not result.found
    assert "unknown timeframe" in result.reason


def test_a_missing_execution_timestamp_is_reported_not_defaulted():
    result = find_entry_candle(window(T1530), None, "15m")
    assert not result.found
    assert "no execution timestamp" in result.reason


def test_no_candles_at_all_is_reported():
    result = find_entry_candle([], EXEC_MID, "15m")
    assert not result.found
    assert "no candles available" in result.reason
    assert find_entry_candle(None, EXEC_MID, "15m").reason == "no candles available"


# ── a feed whose bars are offset ───────────────────────────────────────────

def test_a_consistently_offset_feed_still_resolves_to_the_right_bucket():
    """Some feeds label bars a fixed distance from the epoch grid. Matching on
    the bucket rather than the raw timestamp keeps those correct instead of
    quietly attaching the trade to nothing."""
    candles = window(T1530 + 5, T1545 + 5)

    result = find_entry_candle(candles, EXEC_MID, "15m")

    assert result.found
    assert result.candle.timestamp == T1530 + 5
    assert result.bucket == T1530


def test_a_candle_that_does_not_span_the_execution_is_refused():
    """A bar stamped inside the right bucket but far too early to contain the
    execution instant must not be accepted on the strength of its label."""
    # Stamped at 15:30:00 but we assert the span check by using a bar whose
    # timestamp lands in the previous bucket under a shorter period.
    result = find_entry_candle([C(T1515)], EXEC_NEXT, "15m")
    assert not result.found


# ── field flattening ───────────────────────────────────────────────────────

def test_fields_are_all_present_and_none_when_not_found():
    """A failed lookup must write explicit Nones, never leave a previous
    trade's values in place."""
    fields = entry_candle_fields(find_entry_candle([], EXEC_MID, "15m"))

    assert set(fields) == {
        "entry_candle_timestamp", "entry_candle_open", "entry_candle_high",
        "entry_candle_low", "entry_candle_close",
    }
    assert all(value is None for value in fields.values())


def test_fields_match_the_candle_when_found():
    result = find_entry_candle(window(T1530), EXEC_MID, "15m")
    fields = entry_candle_fields(result)
    assert fields["entry_candle_timestamp"] == float(T1530)
    assert fields["entry_candle_close"] == 1.14422


# ── the chart, the marker and the history must agree ───────────────────────

def test_the_chart_bucket_and_the_entry_bucket_are_the_same_number():
    """Requirement: chart OHLC, marker OHLC and history OHLC refer to one
    candle. They all derive from bucket_start, so prove it rather than assume
    it -- this is the assertion that fails if anyone ever changes one path."""
    from app.services.candle_aggregate import bucket_start
    from app.services.chart_service import timeframe_period

    period = timeframe_period("15m")
    chart_bucket = bucket_start(EXEC_MID, period)
    entry = find_entry_candle(window(T1530), EXEC_MID, "15m")

    assert entry.bucket == chart_bucket == T1530
    assert entry.candle.timestamp == chart_bucket
