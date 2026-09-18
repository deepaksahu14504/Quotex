"""Walk-forward OOS metrics and Health Score computation."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .backtest import BacktestResult, BacktestTrade, MIN_BARS, run_backtest
from ..config import RiskSettings, ValidationSettings
from ..schemas import Candle


SESSION_BUCKETS = {
    "asia": range(0, 8),
    "europe": range(8, 16),
    "us": range(16, 24),
}


def _hour_bucket(ts: float) -> int:
    return datetime.fromtimestamp(ts, tz=timezone.utc).hour


def _session_for_hour(h: int) -> str:
    for name, hours in SESSION_BUCKETS.items():
        if h in hours:
            return name
    return "us"


def _aggregate_trades(trades: List[BacktestTrade], starting_balance: float) -> dict:
    # Sorted ONCE, chronologically, and used for every order-dependent
    # metric below.
    #
    # Previously only the equity/drawdown loop sorted (`for t in
    # sorted(closed, key=...)`) while the streak loop iterated the raw
    # `closed` list in whatever order it arrived. Streaks are inherently
    # order-dependent, so the same five trades produced
    # longest_loss_streak=3 in chronological order and 2 shuffled --
    # verified in test_validation_scoring.py. That number feeds
    # compute_health_score()'s streak_penalty and therefore the
    # active/trial/disabled decision.
    #
    # In today's single caller the list happens to arrive roughly
    # chronological (folds are contiguous and appended in order), so this
    # is a latent bug rather than one firing in production -- but the
    # function had no right to assume that, and sorting here also removes
    # the second sort the equity loop was doing.
    closed = sorted(
        (t for t in trades if t.status in ("win", "loss")),
        key=lambda x: (x.closed_at, x.opened_at),
    )
    wins = sum(1 for t in closed if t.status == "win")
    losses = len(closed) - wins
    gross_win = sum(t.profit for t in closed if t.profit > 0)
    gross_loss = abs(sum(t.profit for t in closed if t.profit < 0))
    net = round(sum(t.profit for t in trades), 2)

    balance = starting_balance
    peak = starting_balance
    max_dd = 0.0
    max_dd_pct = 0.0
    equity_curve: List[dict] = []
    drawdown_curve: List[dict] = []
    for t in closed:
        balance = round(balance + t.profit, 2)
        peak = max(peak, balance)
        dd = peak - balance
        max_dd = max(max_dd, dd)
        dd_pct_point = round(dd / peak * 100, 2) if peak else 0
        max_dd_pct = max(max_dd_pct, dd_pct_point)
        equity_curve.append({"t": t.closed_at, "equity": balance})
        drawdown_curve.append({"t": t.closed_at, "dd": round(dd, 2), "dd_pct": dd_pct_point})

    longest_win = longest_loss = run = 0
    last_status = None
    for t in closed:
        run = run + 1 if t.status == last_status else 1
        if t.status == "win":
            longest_win = max(longest_win, run)
        else:
            longest_loss = max(longest_loss, run)
        last_status = t.status

    by_regime: Dict[str, dict] = {}
    by_hour: Dict[str, dict] = {}
    by_session: Dict[str, dict] = {}
    conf_buckets: Dict[str, dict] = {}

    for t in closed:
        regime = t.regime or "mixed"
        rb = by_regime.setdefault(regime, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        rb["trades"] += 1
        rb["pnl"] = round(rb["pnl"] + t.profit, 2)
        if t.status == "win":
            rb["wins"] += 1
        else:
            rb["losses"] += 1

        h = _hour_bucket(t.opened_at)
        hb = by_hour.setdefault(str(h), {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        hb["trades"] += 1
        hb["pnl"] = round(hb["pnl"] + t.profit, 2)
        if t.status == "win":
            hb["wins"] += 1
        else:
            hb["losses"] += 1

        sess = _session_for_hour(h)
        sb = by_session.setdefault(sess, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        sb["trades"] += 1
        sb["pnl"] = round(sb["pnl"] + t.profit, 2)
        if t.status == "win":
            sb["wins"] += 1
        else:
            sb["losses"] += 1

        bucket = f"{(t.confidence // 10) * 10}-{(t.confidence // 10) * 10 + 9}"
        cb = conf_buckets.setdefault(bucket, {"predicted": [], "actual": []})
        cb["predicted"].append(t.confidence / 100.0)
        cb["actual"].append(1.0 if t.status == "win" else 0.0)

    conf_accuracy = 0.0
    if conf_buckets:
        errors = []
        for b in conf_buckets.values():
            if not b["predicted"]:
                continue
            pred = sum(b["predicted"]) / len(b["predicted"])
            act = sum(b["actual"]) / len(b["actual"])
            errors.append(abs(pred - act))
        if errors:
            conf_accuracy = round(max(0.0, 1.0 - (sum(errors) / len(errors))) * 100, 1)

    for group in (by_regime, by_hour, by_session):
        for v in group.values():
            v["win_rate"] = round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0.0

    return {
        "win_rate": round(wins / len(closed) * 100, 1) if closed else 0.0,
        "total_trades": len(closed),
        "net_profit": net,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
        "confidence_accuracy": conf_accuracy,
        "by_regime": by_regime,
        "by_hour": by_hour,
        "by_session": by_session,
        "equity_curve": equity_curve,
        "drawdown_curve": drawdown_curve,
    }


def compute_health_score(metrics: dict, *, fold_win_rates: List[float], settings: ValidationSettings,
                         correlation_penalty: float = 0.0) -> int:
    """Weighted 0–100 score from OOS metrics."""
    wr = metrics.get("win_rate", 0)
    pf = metrics.get("profit_factor")
    dd_pct = metrics.get("max_drawdown_pct", 0)
    trades = metrics.get("total_trades", 0)
    conf_acc = metrics.get("confidence_accuracy", 50)
    net_profit = metrics.get("net_profit", 0)

    # Profitability (0–35)
    #
    # `profit_factor` is None when gross_loss == 0 -- i.e. the strategy had
    # NO losing trades, which is the best possible outcome, not a missing
    # measurement. The previous `pf = metrics.get("profit_factor") or 0`
    # collapsed that None to 0 and scored it 0/35: a 100%-win-rate strategy
    # scored 82 while a 66.7%-win-rate one scored 87, so the better
    # strategy ranked lower. health_score drives score_to_status(), which
    # decides active/trial/disabled, so this directly suppressed the
    # strategies that were performing best.
    #
    # Mathematically PF is infinite with zero losses, so it earns the cap --
    # but only when the strategy actually made money and actually traded.
    # No trades, or no profit, still scores 0.
    if pf is None:
        pf_score = 35.0 if (trades > 0 and net_profit > 0) else 0.0
    elif pf:
        pf_score = min(35, max(0, (pf - 0.8) * 20))
    else:
        pf_score = 0.0
    pnl_per = net_profit / max(trades, 1)
    pnl_score = min(15, max(0, pnl_per * 3))

    # Win rate (0–20)
    wr_score = min(20, max(0, (wr - 45) * 0.8))

    # Robustness (0–25) — drawdown & streak penalty
    dd_penalty = min(25, dd_pct * 1.2)
    streak_penalty = min(10, metrics.get("longest_loss_streak", 0) * 2)
    robust = max(0, 25 - dd_penalty - streak_penalty * 0.5)

    # OOS stability (0–15)
    stability = 15.0
    if len(fold_win_rates) >= 2:
        mean = sum(fold_win_rates) / len(fold_win_rates)
        var = sum((x - mean) ** 2 for x in fold_win_rates) / len(fold_win_rates)
        stability = max(0, 15 - math.sqrt(var) * 0.4)

    # Calibration (0–10)
    cal = min(10, conf_acc / 10)

    # Sample size gate
    if trades < settings.min_trades_for_score:
        sample_factor = trades / max(settings.min_trades_for_score, 1)
    else:
        sample_factor = 1.0

    raw = (pf_score + pnl_score + wr_score + robust + stability + cal - correlation_penalty) * sample_factor
    return int(max(0, min(100, round(raw))))


#: A fold needs at least this many OOS bars to say anything meaningful --
#: the same threshold the loop below already applied to non-fallback folds.
MIN_OOS_BARS = 10


def walk_forward_slices(n_bars: int, folds: int, min_bars: int = MIN_BARS) -> List[Tuple[int, int, int]]:
    """Return list of (start_idx, oos_start_idx, end_idx) for each fold.

    Returns an EMPTY list when there is not enough history to form even one
    fold. Previously the `or [(0, min_bars, n_bars)]` fallback fired
    unconditionally, so calling this with fewer bars than `min_bars`
    returned a malformed slice with oos_start > end -- e.g.
    walk_forward_slices(10, 3) gave [(0, 55, 10)]. Indexing
    `candles[oos_start]` with that is an IndexError. The one caller in this
    module is defended by its own `len(slice_candles) < MIN_BARS + 5`
    check, so this was latent rather than firing, but the function had no
    business returning a slice that cannot be indexed.
    """
    if n_bars < min_bars + MIN_OOS_BARS:
        return []
    usable = n_bars - min_bars
    if usable < folds * 20:
        folds = max(1, usable // 20)
    if folds < 1:
        return [(0, min_bars, n_bars)]
    slice_size = usable // folds
    out = []
    for i in range(folds):
        oos_start = min_bars + i * slice_size
        end = min_bars + (i + 1) * slice_size if i < folds - 1 else n_bars
        if end - oos_start >= MIN_OOS_BARS:
            out.append((0, oos_start, end))
    # The guard at the top guarantees at least one fold is formable, so a
    # single full-range fallback here is always well-formed.
    return out or [(0, min_bars, n_bars)]


async def evaluate_strategy_oos(
    candles: List[Candle],
    *,
    asset: str,
    strategy: str,
    timeframe: str,
    duration_seconds: int,
    confidence_threshold: int,
    payout_pct: float,
    starting_balance: float,
    risk_settings: RiskSettings,
    validation_settings: ValidationSettings,
    htf_candles: Optional[List[Candle]] = None,
    multi_timeframe_confirmation: bool = False,
    calibrator=None,
    regime_tracker=None,
) -> dict:
    folds = walk_forward_slices(len(candles), validation_settings.walk_forward_folds)
    all_oos_trades: List[BacktestTrade] = []
    fold_win_rates: List[float] = []

    for _start, oos_start, end in folds:
        slice_candles = candles[:end]
        if len(slice_candles) < MIN_BARS + 5:
            continue
        try:
            result: BacktestResult = await run_backtest(
                slice_candles,
                asset=asset,
                timeframe=timeframe,
                enabled_strategies=[strategy],
                confidence_threshold=confidence_threshold,
                duration_seconds=duration_seconds,
                payout_pct=payout_pct,
                starting_balance=starting_balance,
                risk_settings=risk_settings,
                multi_timeframe_confirmation=multi_timeframe_confirmation and bool(htf_candles),
                htf_candles=htf_candles,
                calibrator=calibrator,
                regime_tracker=regime_tracker,
            )
        except ValueError:
            continue
        oos_ts = slice_candles[oos_start].timestamp
        oos_trades = [t for t in result.trades if t.opened_at >= oos_ts and strategy in (t.strategies or [])]
        all_oos_trades.extend(oos_trades)
        closed = [t for t in oos_trades if t.status in ("win", "loss")]
        if closed:
            wins = sum(1 for t in closed if t.status == "win")
            fold_win_rates.append(wins / len(closed) * 100)

    metrics = _aggregate_trades(all_oos_trades, starting_balance)
    oos_consistency = 0.0
    if len(fold_win_rates) >= 2:
        mean = sum(fold_win_rates) / len(fold_win_rates)
        oos_consistency = round(100 - math.sqrt(sum((x - mean) ** 2 for x in fold_win_rates) / len(fold_win_rates)), 1)

    health_score = compute_health_score(
        metrics, fold_win_rates=fold_win_rates, settings=validation_settings,
    )

    from .health_score import score_to_status
    return {
        "asset": asset,
        "strategy": strategy,
        "timeframe": timeframe,
        **metrics,
        "health_score": health_score,
        "health_status": score_to_status(
            health_score,
            active_min=validation_settings.active_score_min,
            trial_min=validation_settings.trial_score_min,
        ),
        "oos_folds": len(folds),
        "oos_consistency": oos_consistency,
        "by_timeframe": {timeframe: {"trades": metrics["total_trades"], "win_rate": metrics["win_rate"],
                                     "pnl": metrics["net_profit"]}},
        "details": {"fold_win_rates": fold_win_rates, "evaluated_at": time.time()},
    }
