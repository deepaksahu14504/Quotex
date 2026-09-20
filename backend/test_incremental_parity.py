"""Regression tests for RCA F5 (volume-source tag) and F6 (vol_ratio warm path).

Both are LIVE-vs-BACKTEST PARITY bugs in `app/engine/incremental.py`, the
engine `orchestrator._evaluate_asset_signal()` actually trades from. The
vectorized path (`candles_to_df` + `strategies._enrich`) used by backtest got
both right, so a strategy validated in backtest behaved differently once it was
live.

F5 — THE VOLUME-SOURCE TAG WAS DROPPED
--------------------------------------
`indicators._has_real_volume()` decides whether the `volume` column is real
exchange order flow or a proxy. Its own docstring records that the
variance+sum fallback heuristic MISCLASSIFIES pyquotex tick counts as real,
because tick count varies, has nunique()>1 and sum()>0 -- which is exactly why
the provider tags every candle `volume_source="real"|"synthetic"` and the
function honours that tag first.

`get_enriched()` does not build its frame with `candles_to_df`; it assembles
each row by hand from the incremental `SymbolState` and copied over only
timestamp/open/high/low/close/volume. The tag was silently lost, so the live
frame fell through to the heuristic the tag exists to avoid and fed synthetic
tick counts into volume_ma / volume_strength as if they were order flow.

F6 — vol_ratio WAS A CONSTANT 1.0 ON THE WARM PATH
--------------------------------------------------
`volatility_ratio()` computes `atr_pct.rolling(50).mean()`. The warm path only
holds TAIL_ROWS(+1) = 61 rows, and with ATR(20) still warming up that leaves
about 41 usable values, so the rolling mean was all-NaN and the `.fillna(1.0)`
inside the function flattened the whole column to 1.0 for every live bar.

Measured on a 240-candle synthetic pyquotex series, last closed bar,
live incremental path vs vectorized reference:

    _has_real_volume   True      ->  False      (reference: False)
    vol_ma             23.85     ->  1.0        (reference: 1.0)
    vol_strength       0.92243   ->  1.0        (reference: 1.0)
    price_strength     0.40517   ->  0.42112    (reference: 0.42112)
    vol_ratio          1.00000   ->  1.02239    (reference: 1.02245)
"""
from __future__ import annotations

import numpy as np
import pytest

from app.engine import incremental
from app.engine.incremental import IndicatorCache
from app.engine import indicators
from app.engine.strategies import _enrich
from app.schemas import Candle

N = 240


def _candles(n: int = N, volume_source: str = "synthetic", seed: int = 7):
    """Synthetic pyquotex-style series: tick-count volume, no real order flow."""
    rng = np.random.default_rng(seed)
    ts = 1_700_000_000.0
    price = 1.10
    out = []
    for i in range(n):
        o = price
        c = o + rng.normal(0, 0.0004)
        h = max(o, c) + abs(rng.normal(0, 0.0002))
        l = min(o, c) - abs(rng.normal(0, 0.0002))
        out.append(Candle(
            timestamp=ts + i * 60, open=o, high=h, low=l, close=c,
            volume=float(rng.integers(1, 40)),       # tick-count proxy
            volume_source=volume_source,
        ))
        price = c
    return out


@pytest.fixture()
def warm_frame():
    """The frame the LIVE path actually serves: call get_enriched twice so the
    second call takes the incremental warm branch and returns the bounded
    TAIL_ROWS(+1) window, not the full cold rebuild."""
    candles = _candles()
    cache = IndicatorCache()
    cold, _ = cache.get_enriched("EURUSD|1m", candles)
    warm, is_warm = cache.get_enriched("EURUSD|1m", candles)
    assert is_warm
    assert len(warm) < len(cold), "expected the bounded warm window, got a full rebuild"
    return candles, cold, warm


@pytest.fixture()
def reference(warm_frame):
    """The vectorized path backtest uses."""
    candles, _cold, _warm = warm_frame
    return _enrich(indicators.candles_to_df(candles).copy())


# --------------------------------------------------------------------------- #
# F5 — the tag must survive into the live frame
# --------------------------------------------------------------------------- #

def test_candles_to_df_carries_the_volume_tag():
    df = indicators.candles_to_df(_candles(10))
    assert "_volume_source" in df.columns
    assert df["_volume_source"].iloc[-1] == "synthetic"


def test_warm_frame_keeps_the_volume_tag(warm_frame):
    """THE F5 regression: the tag existed in candles_to_df but not in the
    frame the live engine served."""
    _candles_, _cold, warm = warm_frame
    assert "_volume_source" in warm.columns, "the volume-source tag was dropped"
    assert warm["_volume_source"].iloc[-1] == "synthetic"


def test_cold_frame_keeps_the_volume_tag(warm_frame):
    _candles_, cold, _warm = warm_frame
    assert "_volume_source" in cold.columns


def test_synthetic_volume_is_not_treated_as_real(warm_frame, reference):
    """The consequence of F5: tick counts were graded as order flow."""
    _c, _cold, warm = warm_frame
    assert indicators._has_real_volume(warm) is False
    assert indicators._has_real_volume(reference) is False


def test_real_volume_is_still_treated_as_real():
    """The fix must not just hardcode False -- genuine order flow still counts."""
    candles = _candles(volume_source="real")
    cache = IndicatorCache()
    cache.get_enriched("EURUSD|1m", candles)
    _warm, _ = None, None
    edf, _ = cache.get_enriched("EURUSD|1m", candles)
    assert edf["_volume_source"].iloc[-1] == "real"
    assert indicators._has_real_volume(edf) is True


def test_vol_ma_matches_the_vectorized_path(warm_frame, reference):
    _c, _cold, warm = warm_frame
    assert warm["vol_ma"].iloc[-2] == pytest.approx(reference["vol_ma"].iloc[-2], abs=1e-9)


def test_vol_strength_matches_the_vectorized_path(warm_frame, reference):
    _c, _cold, warm = warm_frame
    assert warm["vol_strength"].iloc[-2] == pytest.approx(
        reference["vol_strength"].iloc[-2], abs=1e-9
    )


def test_price_strength_matches_the_vectorized_path(warm_frame, reference):
    """price_action_strength reads volume too, so F5 moved it as well."""
    _c, _cold, warm = warm_frame
    assert warm["price_strength"].iloc[-2] == pytest.approx(
        reference["price_strength"].iloc[-2], abs=1e-9
    )


# --------------------------------------------------------------------------- #
# F6 — vol_ratio must not be a constant
# --------------------------------------------------------------------------- #

def test_vol_ratio_is_not_flattened_to_one(warm_frame):
    """THE F6 regression: rolling(50) over a 61-row warm frame was all-NaN and
    the `.fillna(1.0)` inside volatility_ratio made every bar read 1.0."""
    _c, _cold, warm = warm_frame
    recent = warm["vol_ratio"].iloc[-10:-1].to_numpy()
    assert not np.all(recent == 1.0), "vol_ratio is a constant 1.0 across the warm window"
    assert len(set(np.round(recent, 6))) > 1


def test_vol_ratio_is_close_to_the_vectorized_path(warm_frame, reference):
    """Not exact -- atr() uses Wilder smoothing, which recurses over the whole
    series, so a shorter warm-up leaves a small seed offset. Near-parity is the
    bar; a constant 1.0 is not."""
    _c, _cold, warm = warm_frame
    assert warm["vol_ratio"].iloc[-2] == pytest.approx(
        reference["vol_ratio"].iloc[-2], abs=1e-3
    )


def test_derived_window_is_large_enough_for_the_rolling_mean():
    """volatility_ratio needs ATR(20) warm-up plus a full rolling(50). The
    window must clear both, which TAIL_ROWS alone does not."""
    from app.engine.incremental import DERIVED_WINDOW, TAIL_ROWS

    assert DERIVED_WINDOW >= 20 + 50, (
        f"DERIVED_WINDOW={DERIVED_WINDOW} cannot fill a rolling(50) over a "
        "warmed ATR(20)"
    )
    assert DERIVED_WINDOW > TAIL_ROWS


# --------------------------------------------------------------------------- #
# The windowed recompute must not change the row-count contract
# --------------------------------------------------------------------------- #

def test_row_count_contract_is_unchanged(warm_frame):
    """The fix computes over a longer window but must still hand back the same
    bounded frame -- callers index it positionally."""
    _c, cold, warm = warm_frame
    assert len(cold) == len(_c)
    assert len(warm) == incremental.TAIL_ROWS + 1


def test_window_smaller_than_the_frame_does_not_break_alignment():
    """Guard: if DERIVED_WINDOW were ever configured below the frame length,
    the positional copy-back would raise on a length mismatch."""
    candles = _candles(80)
    original = incremental.DERIVED_WINDOW
    incremental.DERIVED_WINDOW = 10          # deliberately too small
    try:
        cache = IndicatorCache()
        cache.get_enriched("K", candles)
        edf, _ = cache.get_enriched("K", candles)
        assert len(edf) == incremental.TAIL_ROWS + 1
        assert "vol_ratio" in edf.columns
    finally:
        incremental.DERIVED_WINDOW = original


def test_unrelated_columns_are_untouched_by_the_windowed_recompute(warm_frame, reference):
    """The fix must not perturb the incrementally-maintained indicators. These
    were already at parity; spot-check that they still are."""
    _c, _cold, warm = warm_frame
    for col in ("ema21", "rsi", "macd", "bb_mid", "atr", "close"):
        assert warm[col].iloc[-2] == pytest.approx(reference[col].iloc[-2], abs=1e-6), col


def test_no_forming_candle_contamination(warm_frame):
    """The last row is the still-forming candle; the last CLOSED bar must not
    have absorbed it."""
    _c, _cold, warm = warm_frame
    assert warm["close"].iloc[-2] == pytest.approx(_c[-2].close, abs=1e-12)
