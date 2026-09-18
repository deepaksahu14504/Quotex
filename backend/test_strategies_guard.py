"""Regression tests for backend/app/engine/strategies.py.

Focus: the minimum-history contract, and proof that the fix does NOT
break signal generation at the warm-up boundary (the dangerous direction).

Run: cd backend && python3 -m pytest test_strategies_guard.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from app.engine import indicators as ta  # noqa: E402
from app.engine import strategies as st  # noqa: E402
from app.engine.incremental import IndicatorCache  # noqa: E402
from app.engine.backtest import MIN_BARS  # noqa: E402

ALL = ["trend_pullback", "ema_ribbon", "supertrend", "psar", "mean_reversion",
       "bollinger_bounce", "momentum_breakout", "macd_cross", "stoch_rsi", "adx_di"]


def mk(ts, c):
    return {"timestamp": ts, "open": c, "high": c + 0.3, "low": c - 0.3,
            "close": c, "volume": 1.0}


def trend(n, slope=0.4):
    return [mk(1000 + i * 60, 100 + i * slope) for i in range(n)]


def wave(n):
    return [mk(1000 + i * 60, 100 + np.sin(i / 7) * 3) for i in range(n)]


# ================================================================= #
# 1. The contract mismatch
# ================================================================= #
@pytest.mark.parametrize("n", [21, 25, 40, 50, 53])
def test_short_history_produces_no_signal(n):
    """PRE-FIX: 21 bars returned Direction.CALL at confidence 75 with all
    10 strategies voting, while confluence was hardcoded 0 and
    support/resistance were entirely NaN."""
    r = st.evaluate(ta.candles_to_df(trend(n)), ALL)
    assert r.direction is None
    assert r.confidence == 0
    assert "Not enough data" in r.reasons[0]
    assert str(n) in r.reasons[0] and "54" in r.reasons[0]


def test_rejection_reason_names_both_numbers():
    r = st.evaluate(ta.candles_to_df(trend(30)), ALL)
    assert r.reasons == ["Not enough data (30 bars, need 54)"]


def test_none_input_is_handled():
    r = st.evaluate(None, ALL)
    assert r.direction is None and r.confidence == 0


# ================================================================= #
# 2. THE DANGEROUS DIRECTION — the guard must not break warm-up
# ================================================================= #
def test_exactly_54_bars_still_evaluates():
    """54 is load-bearing. `warm` needs >=55 RAW bars and the caller drops
    the forming candle, so the warm-up boundary hands evaluate() exactly
    54 rows. A guard of 55 would silently kill signal generation for every
    asset at the moment it finishes warming up."""
    r = st.evaluate(ta.candles_to_df(trend(54)), ALL)
    assert r.direction is not None
    assert r.confidence > 0


def test_the_orchestrator_boundary_frame_is_accepted():
    """Reproduces the exact production handoff: get_enriched -> warm ->
    iloc[:-1] -> evaluate(pre_enriched=True)."""
    cache = IndicatorCache()
    for n in (55, 56, 60, 120):
        candles = wave(n)
        edf, warm = cache.get_enriched(f"K{n}|1m", candles)
        assert warm is True, f"{n} bars should be warm"
        edf_eval = edf.iloc[:-1]
        assert len(edf_eval) >= st.MIN_BARS_FOR_EVALUATION, (
            f"{n} raw bars -> {len(edf_eval)} evaluable rows, below the guard "
            f"of {st.MIN_BARS_FOR_EVALUATION} -- this would break warm-up"
        )
        r = st.evaluate(edf_eval, ALL, pre_enriched=True)
        assert "Not enough data" not in (r.reasons[0] if r.reasons else "")


def test_guard_is_consistent_with_the_rest_of_the_codebase():
    """backtest.py already documented 55 as evaluate()'s requirement while
    evaluate() actually enforced 21."""
    assert MIN_BARS == 55
    assert st.MIN_BARS_FOR_EVALUATION == MIN_BARS - 1


# ================================================================= #
# 3. Behaviour above the guard is unchanged
# ================================================================= #
def test_results_above_the_guard_are_unchanged_by_the_fix():
    """The fix only rejects below the threshold; it must not alter any
    result at or above it."""
    for n in (54, 60, 120, 300):
        df = ta.candles_to_df(wave(n))
        r = st.evaluate(df, ALL)
        assert r.regime in ("trend", "range", "mixed")
        assert 0 <= r.confidence <= 100
        assert isinstance(r.votes, dict)


def test_evaluation_is_deterministic():
    df = ta.candles_to_df(wave(150))
    a, b = st.evaluate(df, ALL), st.evaluate(df, ALL)
    assert (a.direction, a.confidence, a.votes, a.regime) == \
           (b.direction, b.confidence, b.votes, b.regime)


def test_evaluate_does_not_mutate_a_shared_enriched_frame():
    """The frame comes from IndicatorCache and is reused across calls, so
    a write here would corrupt every later scan for that key."""
    cache = IndicatorCache()
    edf, warm = cache.get_enriched("SHARED|1m", wave(120))
    assert warm
    before = edf.copy(deep=True)
    st.evaluate(edf, ALL, pre_enriched=True)
    pd.testing.assert_frame_equal(edf, before)


def test_enrich_does_not_mutate_its_input():
    df = ta.candles_to_df(wave(120))
    before = df.copy(deep=True)
    st._enrich(df)
    pd.testing.assert_frame_equal(df, before)


def test_pre_enriched_missing_columns_fails_loudly():
    df = ta.candles_to_df(wave(120))[["timestamp", "open", "high", "low"]]
    with pytest.raises(ValueError) as exc:
        st.evaluate(df, ALL, pre_enriched=True)
    assert "close" in str(exc.value)


# ================================================================= #
# 4. Regime helpers
# ================================================================= #
def test_current_regime_delegates_to_regime():
    """Not a duplicate implementation -- a thin public wrapper."""
    for n in (60, 120, 300):
        edf = st._enrich(ta.candles_to_df(wave(n)))
        assert st.current_regime(edf) == st._regime(edf)


def test_regime_thresholds_are_honoured():
    edf = st._enrich(ta.candles_to_df(trend(120)))
    assert st._regime(edf, trend_adx=0.0, range_adx=0.0) == "trend"
    assert st._regime(edf, trend_adx=1e9, range_adx=1e9) == "range"


# ================================================================= #
# 5. NaN / degenerate input safety
# ================================================================= #
def test_flat_market_does_not_produce_nan_confidence():
    flat = [mk(1000 + i * 60, 100.0) for i in range(120)]
    r = st.evaluate(ta.candles_to_df(flat), ALL)
    assert isinstance(r.confidence, int)
    assert 0 <= r.confidence <= 100
    assert not any(isinstance(v, float) and np.isnan(v) for v in r.raw_features.values())


def test_nan_support_resistance_does_not_break_evaluation():
    """S/R is all-NaN on a straight line (no pivots); the proximity paths
    are pd.notna()-guarded and must stay that way."""
    df = ta.candles_to_df(trend(120))
    edf = st._enrich(df)
    assert edf["support"].isna().all()
    r = st.evaluate(df, ALL)
    assert 0 <= r.confidence <= 100


def test_empty_enabled_list_is_safe():
    r = st.evaluate(ta.candles_to_df(wave(120)), [])
    assert r.direction is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
