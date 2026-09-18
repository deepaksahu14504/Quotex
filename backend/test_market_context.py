"""Regression tests for backend/app/engine/market_context.py.

Section 1 fails against the pre-fix code.
Run: cd backend && python3 -m pytest test_market_context.py -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.engine.market_context import (  # noqa: E402
    AdaptiveThresholdConfig,
    MarketContextEngine,
)


def eng(**kw):
    kw.setdefault("enabled", True)
    return MarketContextEngine(AdaptiveThresholdConfig(**kw))


# ================================================================= #
# 1. Duplicate observations corrupting the rolling window
# ================================================================= #
def test_rescanning_one_candle_yields_one_sample():
    """The scan loop is event-driven with a 3s backstop, so on a 1m
    timeframe strategies.evaluate() observes the SAME closed candle ~20
    times. PRE-FIX every one of those was appended."""
    e = eng(min_samples=30, rolling_window=200)
    for adx in (10.0, 12.0, 30.0):
        for _ in range(20):
            e.observe("EURUSD", "1m", "adx", adx)
    st = e._metrics[("EURUSD", "1m", "adx")]
    assert len(st.history) == 3
    assert st.sample_count == 3
    assert st.duplicates_skipped == 57


def test_min_samples_now_counts_candles_not_scans():
    """PRE-FIX min_samples=30 was cleared after ~30 scans (~1.5 min on a
    1m chart) rather than 30 candles, so adaptive thresholds engaged on
    almost no real data."""
    e = eng(min_samples=30)
    for _ in range(100):
        e.observe("EURUSD", "1m", "adx", 20.0)     # one candle, rescanned
    assert e.adaptive_threshold("EURUSD", "1m", "adx", 14.0) == 14.0


def test_percentile_rank_is_not_weighted_by_scan_frequency():
    """This rank scales trade stake via _adaptive_risk_multipliers."""
    e = eng(min_samples=2, rolling_window=200)
    for _ in range(90):
        e.observe("X", "1m", "adx", 10.0)
    time.sleep(0.01)
    for _ in range(5):
        e.observe("X", "1m", "adx", 40.0)
    assert e.percentile_rank("X", "1m", "adx", 25.0) == 50.0


def test_distinct_values_are_never_suppressed():
    e = eng(min_samples=1)
    for v in (10.0, 10.5, 11.0, 10.5, 12.0):
        e.observe("X", "1m", "adx", v)
    st = e._metrics[("X", "1m", "adx")]
    assert len(st.history) == 5, "only CONSECUTIVE identical values are duplicates"


def test_repeat_after_the_window_is_accepted():
    e = eng(min_samples=1, dedupe_window_s=0.05)
    e.observe("X", "1m", "adx", 20.0)
    time.sleep(0.08)
    e.observe("X", "1m", "adx", 20.0)
    assert len(e._metrics[("X", "1m", "adx")].history) == 2


def test_bar_ts_gives_exact_deduplication():
    e = eng(min_samples=1)
    e.observe("X", "1m", "adx", 20.0, bar_ts=1_700_000_000)
    e.observe("X", "1m", "adx", 20.0, bar_ts=1_700_000_000)
    e.observe("X", "1m", "adx", 20.0, bar_ts=1_700_000_060)   # new bar, same value
    assert len(e._metrics[("X", "1m", "adx")].history) == 2


def test_dedupe_can_be_disabled_for_exact_legacy_behaviour():
    e = eng(min_samples=1, dedupe_repeat_observations=False)
    for _ in range(50):
        e.observe("X", "1m", "adx", 20.0)
    assert len(e._metrics[("X", "1m", "adx")].history) == 50


# ================================================================= #
# 2. percentile_override cache collision
# ================================================================= #
def test_different_percentiles_are_not_served_the_cached_value():
    """PRE-FIX the second query got the first query's cached value."""
    e = eng(min_samples=3, update_interval_s=60.0)
    for v in (10.0, 20.0, 30.0, 40.0, 50.0):
        e.observe("Y", "1m", "rsi", v)
    hi = e.adaptive_threshold("Y", "1m", "rsi", 70.0, percentile_override=0.90)
    lo = e.adaptive_threshold("Y", "1m", "rsi", 30.0, percentile_override=0.10)
    assert hi != lo


def test_same_percentile_still_uses_the_cache():
    e = eng(min_samples=3, update_interval_s=60.0)
    for v in (10.0, 20.0, 30.0, 40.0, 50.0):
        e.observe("Y", "1m", "rsi", v)
    a = e.adaptive_threshold("Y", "1m", "rsi", 70.0, percentile_override=0.90)
    b = e.adaptive_threshold("Y", "1m", "rsi", 70.0, percentile_override=0.90)
    assert a == b


# ================================================================= #
# 3. Bounded memory
# ================================================================= #
def test_tracked_keys_are_bounded():
    e = eng(min_samples=1, max_tracked_keys=50)
    for i in range(300):
        e.observe(f"A{i}", "1m", "adx", float(i))
    assert len(e._metrics) <= 50


def test_hot_keys_survive_eviction():
    e = eng(min_samples=1, max_tracked_keys=50)
    for i in range(200):
        e.observe(f"COLD{i}", "1m", "adx", float(i))
        if i % 4 == 0:
            e.observe("HOT", "1m", "adx", float(i) + 0.5)
    assert ("HOT", "1m", "adx") in e._metrics


def test_rolling_window_stays_capped():
    e = eng(min_samples=1, rolling_window=20)
    for i in range(500):
        e.observe("X", "1m", "adx", float(i))
    assert len(e._metrics[("X", "1m", "adx")].history) == 20


def test_get_stats_payload_is_bounded():
    e = eng(min_samples=1, max_tracked_keys=400, stats_sample_limit=10)
    for i in range(200):
        e.observe(f"A{i}", "1m", "adx", float(i))
    stats = e.get_stats()
    assert len(stats["sample_counts"]) <= 10
    assert stats["sample_counts_truncated"] == 190


# ================================================================= #
# 4. Correctness preserved
# ================================================================= #
def test_disabled_engine_always_returns_the_static_default():
    e = eng(enabled=False, min_samples=1)
    for v in range(100):
        e.observe("X", "1m", "adx", float(v))
    assert e.adaptive_threshold("X", "1m", "adx", 14.0) == 14.0


def test_cold_start_returns_the_static_default():
    e = eng(min_samples=30)
    e.observe("X", "1m", "adx", 20.0)
    assert e.adaptive_threshold("X", "1m", "adx", 14.0) == 14.0


def test_nan_and_inf_are_ignored():
    e = eng(min_samples=1)
    for bad in (float("nan"), float("inf"), float("-inf"), None, "x"):
        e.observe("X", "1m", "adx", bad)
    assert ("X", "1m", "adx") not in e._metrics


def test_sorted_cache_matches_a_fresh_sort():
    e = eng(min_samples=1)
    vals = [3.0, 1.0, 4.0, 1.5, 5.0, 9.0, 2.0, 6.0]
    for v in vals:
        e.observe("X", "1m", "adx", v)
    st = e._metrics[("X", "1m", "adx")]
    assert st.sorted_history() == sorted(st.history)
    e.observe("X", "1m", "adx", 0.5)
    assert st.sorted_history() == sorted(st.history), "cache not invalidated"


def test_percentile_rank_matches_the_naive_computation():
    e = eng(min_samples=1)
    vals = [float(v) for v in (5, 1, 9, 3, 7, 2, 8)]
    for v in vals:
        e.observe("X", "1m", "adx", v)
    for probe in (0.0, 1.0, 4.0, 5.0, 9.0, 100.0):
        naive = round(100.0 * sum(1 for v in vals if v <= probe) / len(vals), 1)
        assert e.percentile_rank("X", "1m", "adx", probe) == naive


def test_drift_clamp_bounds_each_update():
    e = eng(min_samples=2, ema_alpha=1.0, max_drift_per_update=0.05)
    e.observe("X", "1m", "adx", 100.0)
    e.observe("X", "1m", "adx", 100.5)
    first = e.adaptive_threshold("X", "1m", "adx", 50.0)
    e.observe("X", "1m", "adx", 10000.0)
    second = e.adaptive_threshold("X", "1m", "adx", 50.0)
    assert abs(second - first) <= abs(first) * 0.05 + 1e-9


def test_reset_scoping_still_works():
    e = eng(min_samples=1)
    e.observe("A", "1m", "adx", 1.0)
    e.observe("A", "5m", "adx", 1.0)
    e.observe("B", "1m", "adx", 1.0)
    e.reset(asset="A")
    assert ("A", "1m", "adx") not in e._metrics
    assert ("A", "5m", "adx") not in e._metrics
    assert ("B", "1m", "adx") in e._metrics
    e.reset()
    assert not e._metrics


def test_adaptive_threshold_never_raises():
    e = eng(min_samples=1)
    e.observe("X", "1m", "adx", 5.0)
    e._metrics[("X", "1m", "adx")].history = None    # force an internal error
    assert e.adaptive_threshold("X", "1m", "adx", 14.0) == 14.0
    assert e._errors_total >= 1


def test_last_value_returns_the_newest_sample():
    e = eng(min_samples=1)
    for v in (1.0, 2.0, 3.0):
        e.observe("X", "1m", "adx", v)
    assert e.last_value("X", "1m", "adx") == 3.0
    assert e.last_value("NOPE", "1m", "adx") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
