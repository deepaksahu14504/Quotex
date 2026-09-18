"""Regression tests for backend/app/engine/validation_scoring.py.

Section 1 fails against the pre-fix code.
Run: cd backend && python3 -m pytest test_validation_scoring.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.config import ValidationSettings  # noqa: E402
from app.engine.backtest import BacktestTrade  # noqa: E402
from app.engine.validation_scoring import (  # noqa: E402
    _aggregate_trades,
    compute_health_score,
    walk_forward_slices,
)
from app.schemas import Direction  # noqa: E402

VS = ValidationSettings()


def T(opened, status, profit, conf=70, regime="trend"):
    return BacktestTrade(
        opened_at=opened, closed_at=opened + 60, asset="EURUSD",
        direction=Direction.CALL, confidence=conf, amount=10.0,
        payout_pct=85.0, entry_price=1.0, exit_price=1.1,
        profit=profit, status=status, regime=regime,
    )


def run(n, pattern):
    """pattern is a string of W/L, one char per trade."""
    return [T(1000 + i * 60, "win" if c == "W" else "loss", 8 if c == "W" else -10)
            for i, c in enumerate(pattern)][:n or len(pattern)]


# ================================================================= #
# 1. Streaks were order-dependent
# ================================================================= #
def test_streaks_are_order_independent():
    """PRE-FIX only the equity/drawdown loop sorted; the streak loop
    iterated the raw list, so the same trades gave 3 chronologically and
    2 shuffled."""
    chrono = run(0, "WLLLW")
    shuffled = [chrono[1], chrono[3], chrono[0], chrono[2], chrono[4]]
    a = _aggregate_trades(chrono, 1000.0)
    b = _aggregate_trades(shuffled, 1000.0)
    assert a["longest_loss_streak"] == b["longest_loss_streak"] == 3
    assert a["longest_win_streak"] == b["longest_win_streak"]


def test_streak_values_are_correct():
    m = _aggregate_trades(run(0, "WWLLLLWWW"), 1000.0)
    assert m["longest_loss_streak"] == 4
    assert m["longest_win_streak"] == 3


def test_all_metrics_are_order_independent():
    chrono = run(0, "WLWLLWWL")
    reversed_order = list(reversed(chrono))
    a, b = _aggregate_trades(chrono, 1000.0), _aggregate_trades(reversed_order, 1000.0)
    for k in ("win_rate", "total_trades", "net_profit", "profit_factor",
              "max_drawdown", "max_drawdown_pct",
              "longest_win_streak", "longest_loss_streak"):
        assert a[k] == b[k], f"{k}: {a[k]} != {b[k]}"


def test_equity_curve_is_chronological():
    m = _aggregate_trades(list(reversed(run(0, "WLWLW"))), 1000.0)
    ts = [p["t"] for p in m["equity_curve"]]
    assert ts == sorted(ts)


# ================================================================= #
# 2. profit_factor None meant "no losses", scored as "no profit"
# ================================================================= #
def test_zero_losses_is_not_scored_as_zero_profitability():
    """PRE-FIX `pf = metrics.get("profit_factor") or 0` collapsed None to
    0, so a flawless strategy got 0/35 for profitability."""
    perfect = _aggregate_trades(run(0, "W" * 30), 1000.0)
    assert perfect["profit_factor"] is None
    mixed = _aggregate_trades(run(0, "WWL" * 10), 1000.0)
    hp = compute_health_score(perfect, fold_win_rates=[100.0, 100.0], settings=VS)
    hm = compute_health_score(mixed, fold_win_rates=[66.0, 66.0], settings=VS)
    assert hp > hm, f"perfect={hp} must outrank mixed={hm}"


def test_no_trades_scores_zero():
    empty = _aggregate_trades([], 1000.0)
    assert compute_health_score(empty, fold_win_rates=[], settings=VS) == 0


def test_zero_losses_but_no_profit_is_not_rewarded():
    """Guard against the fix over-rewarding: the cap requires real profit."""
    m = _aggregate_trades(run(0, "W" * 20), 1000.0)
    m["net_profit"] = 0.0
    score_no_profit = compute_health_score(m, fold_win_rates=[100.0], settings=VS)
    m["net_profit"] = 160.0
    score_profit = compute_health_score(m, fold_win_rates=[100.0], settings=VS)
    assert score_no_profit < score_profit


def test_normal_profit_factor_scoring_is_unchanged():
    """The fix must only touch the None branch."""
    m = _aggregate_trades(run(0, "WWL" * 10), 1000.0)
    assert m["profit_factor"] is not None
    assert compute_health_score(m, fold_win_rates=[66.0, 66.0], settings=VS) == 87


def test_health_score_stays_in_range():
    for pattern in ("W" * 40, "L" * 40, "WL" * 20, "WWWL" * 10, "WLLL" * 10):
        m = _aggregate_trades(run(0, pattern), 1000.0)
        s = compute_health_score(m, fold_win_rates=[50.0, 60.0], settings=VS)
        assert 0 <= s <= 100, f"{pattern} -> {s}"
        assert isinstance(s, int)


def test_all_losses_ranks_far_below_a_winning_strategy():
    """An all-loss run still scores 18, not 0: OOS stability contributes a
    full 15/15 for zero-variance folds (perfectly consistent losing) and
    calibration adds the rest. That is the formula behaving as designed --
    what matters is the ORDERING, not an arbitrary ceiling."""
    losing = _aggregate_trades(run(0, "L" * 30), 1000.0)
    winning = _aggregate_trades(run(0, "WWL" * 10), 1000.0)
    lo = compute_health_score(losing, fold_win_rates=[0.0, 0.0], settings=VS)
    hi = compute_health_score(winning, fold_win_rates=[66.0, 66.0], settings=VS)
    assert lo < hi - 40, f"losing={lo} winning={hi}"
    assert lo < VS.trial_score_min, "an all-loss strategy must not reach trial status"


# ================================================================= #
# 3. Aggregation correctness
# ================================================================= #
def test_win_rate_and_counts():
    m = _aggregate_trades(run(0, "WWLW"), 1000.0)
    assert m["total_trades"] == 4
    assert m["win_rate"] == 75.0


def test_draws_are_excluded_from_closed_counts():
    trades = run(0, "WWL") + [T(9999, "draw", 0.0)]
    m = _aggregate_trades(trades, 1000.0)
    assert m["total_trades"] == 3


def test_drawdown_is_computed_from_the_equity_path():
    m = _aggregate_trades(run(0, "WLLLW"), 1000.0)
    assert m["max_drawdown"] == 30.0
    assert m["max_drawdown_pct"] > 0


def test_empty_input_is_safe():
    m = _aggregate_trades([], 1000.0)
    assert m["total_trades"] == 0 and m["win_rate"] == 0.0
    assert m["profit_factor"] is None
    assert m["equity_curve"] == [] and m["drawdown_curve"] == []


def test_grouping_buckets_sum_to_the_total():
    m = _aggregate_trades(run(0, "WLWWLLWL"), 1000.0)
    for group in ("by_regime", "by_hour", "by_session"):
        assert sum(v["trades"] for v in m[group].values()) == m["total_trades"]


def test_confidence_accuracy_is_a_percentage():
    trades = [T(1000 + i * 60, "win" if i % 10 < 9 else "loss",
                8 if i % 10 < 9 else -10, conf=90) for i in range(40)]
    m = _aggregate_trades(trades, 1000.0)
    assert 0.0 <= m["confidence_accuracy"] <= 100.0


# ================================================================= #
# 4. walk_forward_slices
# ================================================================= #
@pytest.mark.parametrize("n_bars,folds", [
    (100, 3), (200, 5), (1000, 5), (56, 3), (55, 3), (10, 3), (0, 3), (5000, 8),
])
def test_slices_are_well_formed(n_bars, folds):
    out = walk_forward_slices(n_bars, folds)
    for start, oos_start, end in out:
        assert start == 0
        assert oos_start <= end, "a slice must be indexable"
        assert end <= n_bars


def test_slices_are_contiguous_and_non_overlapping():
    out = walk_forward_slices(1000, 5)
    for (_, oos_a, end_a), (_, oos_b, _) in zip(out, out[1:]):
        assert oos_b == end_a, "OOS windows must tile without gaps or overlap"


def test_insufficient_history_returns_no_folds():
    """PRE-FIX walk_forward_slices(10, 3) returned [(0, 55, 10)] --
    oos_start beyond end, and candles[55] on a 10-bar list is an
    IndexError for any caller that isn't separately defended."""
    for n in (0, 10, 54, 55, 60, 64):
        assert walk_forward_slices(n, 3) == [], f"n_bars={n} should form no folds"
    assert walk_forward_slices(65, 3), "65 bars is enough for one fold"


def test_slice_indices_stay_in_bounds():
    """oos_start indexes into candles[:end] -- an out-of-range value would
    be an IndexError at evaluate_strategy_oos()."""
    for n in (60, 100, 500, 2000):
        for f in (1, 3, 5, 10):
            for _s, oos_start, end in walk_forward_slices(n, f):
                if end - oos_start >= 10:
                    assert oos_start < n


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
