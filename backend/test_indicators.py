"""Regression tests for backend/app/engine/indicators.py.

Section 1 fails against the pre-fix code.
Run: cd backend && python3 -m pytest test_indicators.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from app.engine import indicators as ta  # noqa: E402
from app.engine.incremental import IndicatorCache  # noqa: E402


def mk(ts, c, spread=0.2):
    return {"timestamp": ts, "open": c, "high": c + spread,
            "low": c - spread, "close": c, "volume": 1.0}


def series(vals):
    return pd.Series([float(v) for v in vals])


# ================================================================ #
# 1. RSI divergence between the two implementations
# ================================================================ #
def test_rsi_pure_uptrend_is_100_not_neutral():
    """PRE-FIX: avg_loss==0 -> NaN -> fillna(50). A clean uptrend reported
    NEUTRAL instead of overbought, so this form of overbought could never
    be detected while oversold worked fine."""
    assert ta.rsi(series([100 + i * 0.5 for i in range(60)])).iloc[-1] == pytest.approx(100.0)


def test_rsi_pure_downtrend_still_zero():
    assert ta.rsi(series([100 - i * 0.5 for i in range(60)])).iloc[-1] == pytest.approx(0.0)


def test_rsi_flat_series_is_neutral():
    assert ta.rsi(series([100.0] * 60)).iloc[-1] == pytest.approx(50.0)


def test_rsi_matches_the_incremental_implementation():
    """The real defect: two live implementations of one indicator that
    disagreed by 50 RSI points, so a bar's value depended on whether the
    warm-up path or the incremental path produced it."""
    for slope in (0.5, -0.5, 0.0):
        candles = [mk(1000 + i * 60, 100 + i * slope) for i in range(80)]
        vec = float(ta.rsi(ta.candles_to_df(candles)["close"], 14).iloc[-1])
        edf, _warm = IndicatorCache().get_enriched("X|1m", candles)
        inc = float(edf["rsi"].iloc[-1])
        assert vec == pytest.approx(inc, abs=0.01), f"slope={slope}: {vec} vs {inc}"


def test_rsi_mixed_series_is_unchanged_by_the_fix():
    """The fix must only touch the zero-loss branch."""
    vals = [100, 101, 100.5, 102, 101.5, 103, 102, 104, 103.5, 105,
            104, 106, 105.5, 107, 106, 108, 107.5, 109, 108, 110]
    out = ta.rsi(series(vals)).iloc[-1]
    assert 0.0 < out < 100.0
    assert out == pytest.approx(78.06, abs=0.5)


def test_rsi_stays_in_range():
    rng = np.random.default_rng(7)
    out = ta.rsi(series(100 + rng.normal(0, 1, 500).cumsum()))
    assert out.min() >= 0.0 and out.max() <= 100.0
    assert not out.isna().any()


# ================================================================ #
# 2. candles_to_df hygiene
# ================================================================ #
def test_out_of_order_candles_are_sorted():
    ordered = [mk(1000 + i * 60, 100 + i) for i in range(30)]
    shuffled = ordered[15:] + ordered[:15]
    df = ta.candles_to_df(shuffled)
    assert df["timestamp"].is_monotonic_increasing


def test_indicators_are_order_independent():
    """PRE-FIX shuffling 30 identical candles moved ATR 2.000 -> 2.709 and
    RSI 50.00 -> 57.04, silently."""
    ordered = [mk(1000 + i * 60, 100 + i) for i in range(30)]
    shuffled = ordered[20:] + ordered[:20]
    d_o, d_s = ta.candles_to_df(ordered), ta.candles_to_df(shuffled)
    assert ta.atr(d_o).iloc[-1] == pytest.approx(ta.atr(d_s).iloc[-1])
    assert ta.rsi(d_o["close"]).iloc[-1] == pytest.approx(ta.rsi(d_s["close"]).iloc[-1])
    assert ta.adx(d_o).iloc[-1] == pytest.approx(ta.adx(d_s).iloc[-1])


def test_duplicate_timestamps_collapse_to_one_bar():
    ordered = [mk(1000 + i * 60, 100 + i) for i in range(30)]
    df = ta.candles_to_df(ordered + ordered[-3:])
    assert len(df) == 30
    assert df["timestamp"].nunique() == 30


def test_duplicate_keeps_the_freshest_read():
    """Matches how services/market.py folds live ticks into an open bar."""
    df = ta.candles_to_df([mk(1000, 100), mk(1000, 105)])
    assert len(df) == 1
    assert df["close"].iloc[0] == 105.0


def test_no_row_is_ever_invented():
    ordered = [mk(1000 + i * 60, 100 + i) for i in range(30)]
    df = ta.candles_to_df(ordered)
    assert set(df["timestamp"]) <= {c["timestamp"] for c in ordered}
    assert len(df) <= len(ordered)


def test_empty_input_has_numeric_dtypes():
    df = ta.candles_to_df([])
    assert df.empty
    for col in ("open", "high", "low", "close", "volume"):
        assert df[col].dtype == np.float64
    assert ta.rsi(df["close"]).empty


def test_unparseable_rows_are_dropped_not_crashed():
    good = mk(1000, 100)
    bad = {"timestamp": "nope", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}
    worse = {"timestamp": 2000, "open": None, "high": 1, "low": 1, "close": None, "volume": 0}
    df = ta.candles_to_df([good, bad, worse])
    assert len(df) == 1 and df["timestamp"].iloc[0] == 1000


def test_accepts_object_candles_too():
    class C:
        def __init__(self, ts, c):
            self.timestamp, self.open, self.high, self.low, self.close, self.volume = ts, c, c + 1, c - 1, c, 1
    df = ta.candles_to_df([C(2000, 5), C(1000, 4)])
    assert list(df["timestamp"]) == [1000, 2000]


# ================================================================ #
# 3. No NaN escapes into the enriched frame
# ================================================================ #
@pytest.mark.parametrize("n", [1, 2, 5, 14, 15, 30, 60, 200])
def test_indicators_never_raise_on_short_history(n):
    df = ta.candles_to_df([mk(1000 + i * 60, 100 + (i % 3)) for i in range(n)])
    for fn in (ta.rsi,):
        assert len(fn(df["close"])) == n
    for fn in (ta.atr, ta.adx, ta.volume_ma, ta.volume_strength,
               ta.price_action_strength, ta.volatility_ratio):
        assert len(fn(df)) == n
    ta.supertrend(df); ta.psar(df); ta.bollinger(df["close"])
    ta.stochastic(df); ta.stoch_rsi(df["close"]); ta.macd(df["close"])
    ta.find_support_resistance(df); ta.confluence_points(df)


def test_flat_market_produces_no_nan_or_inf():
    """Zero-range bars are the classic divide-by-zero source."""
    df = ta.candles_to_df([mk(1000 + i * 60, 100.0, spread=0.0) for i in range(120)])
    for name, s in (("rsi", ta.rsi(df["close"])), ("adx", ta.adx(df)),
                    ("atr", ta.atr(df)), ("vol_ratio", ta.volatility_ratio(df))):
        arr = s.to_numpy(dtype=float)
        assert not np.isnan(arr).any(), f"{name} produced NaN"
        assert not np.isinf(arr).any(), f"{name} produced inf"


def test_atr_pct_handles_zero_close():
    assert ta.atr_pct(1.0, 0.0) == 0.0
    assert ta.atr_pct(1.0, 100.0) == pytest.approx(1.0)


# ================================================================ #
# 4. No look-ahead bias
# ================================================================ #
@pytest.mark.parametrize("fn_name", ["rsi", "atr", "adx"])
def test_no_lookahead_appending_a_future_bar_does_not_change_the_past(fn_name):
    """A value at bar i must depend only on bars <= i."""
    base = [mk(1000 + i * 60, 100 + np.sin(i / 5) * 3) for i in range(120)]
    extra = base + [mk(1000 + 120 * 60, 500.0)]      # a wild future bar
    d1, d2 = ta.candles_to_df(base), ta.candles_to_df(extra)
    if fn_name == "rsi":
        a, b = ta.rsi(d1["close"]), ta.rsi(d2["close"])
    else:
        fn = getattr(ta, fn_name)
        a, b = fn(d1), fn(d2)
    assert np.allclose(a.to_numpy(dtype=float), b.iloc[:len(a)].to_numpy(dtype=float),
                       equal_nan=True), f"{fn_name} leaks future information"


def test_supertrend_and_psar_have_no_lookahead():
    base = [mk(1000 + i * 60, 100 + np.cos(i / 7) * 4) for i in range(120)]
    extra = base + [mk(1000 + 120 * 60, 1000.0)]
    d1, d2 = ta.candles_to_df(base), ta.candles_to_df(extra)
    _, dir1 = ta.supertrend(d1)
    _, dir2 = ta.supertrend(d2)
    assert np.allclose(dir1.to_numpy(), dir2.iloc[:len(dir1)].to_numpy())
    assert np.allclose(ta.psar(d1).to_numpy(), ta.psar(d2).iloc[:len(d1)].to_numpy())


# ================================================================ #
# 5. Determinism / no hidden state
# ================================================================ #
def test_repeated_calls_are_identical():
    df = ta.candles_to_df([mk(1000 + i * 60, 100 + (i % 7)) for i in range(150)])
    for fn in (ta.atr, ta.adx, ta.volatility_ratio):
        assert fn(df).equals(fn(df))
    assert ta.rsi(df["close"]).equals(ta.rsi(df["close"]))


def test_input_frame_is_not_mutated():
    df = ta.candles_to_df([mk(1000 + i * 60, 100 + i) for i in range(60)])
    before = df.copy(deep=True)
    ta.atr(df); ta.adx(df); ta.supertrend(df); ta.psar(df)
    ta.find_support_resistance(df); ta.confluence_points(df)
    pd.testing.assert_frame_equal(df, before)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
