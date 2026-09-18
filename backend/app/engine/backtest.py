"""Backtesting engine.

Deliberately reuses the exact same code the live scan loop uses —
`IndicatorCache` for indicators and `strategies.evaluate()` for signal
generation — instead of a separate reimplementation that could quietly
drift from what the live system actually does. If this says a strategy
would have won 62% of the time, that's what the live engine would have
produced given the same candles, not an approximation of it.

No lookahead: at bar i, only candles[0..i] are visible to the indicator
cache and strategy evaluation. A position's outcome is only resolved once
a later bar's timestamp reaches its expiry — using price data that is, at
that point in the backtest, in the "past" relative to when it's read, but
always AFTER the entry decision was made using only earlier data.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from ..config import RiskSettings
from ..schemas import Candle, Direction
from . import strategies
from .incremental import IndicatorCache
from .risk import RiskManager

MIN_BARS = 55  # matches the warm-up strategies.evaluate() requires


class BacktestTrade(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    opened_at: float
    closed_at: float
    asset: str
    direction: Direction
    confidence: int
    raw_confidence: Optional[int] = None
    regime: Optional[str] = None
    hour_utc: Optional[int] = None
    amount: float
    payout_pct: float
    entry_price: float
    exit_price: float
    profit: float
    status: str  # win | loss | draw
    strategies: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)


class StrategyBreakdown(BaseModel):
    name: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    pnl: float


class BacktestSummary(BaseModel):
    asset: str
    timeframe: str
    bars_evaluated: int
    from_ts: float
    to_ts: float
    starting_balance: float
    ending_balance: float
    total_trades: int
    wins: int
    losses: int
    draws: int
    win_rate: float
    net_pnl: float
    net_pnl_pct: float
    profit_factor: Optional[float]
    max_drawdown: float
    max_drawdown_pct: float
    longest_win_streak: int
    longest_loss_streak: int
    stopped_early_reason: Optional[str] = None


class BacktestResult(BaseModel):
    summary: BacktestSummary
    trades: List[BacktestTrade]
    equity_curve: List[Dict[str, float]]
    by_strategy: List[StrategyBreakdown]


def _payout_amount(amount: float, payout_pct: float) -> float:
    return round(amount * (payout_pct / 100.0), 2)


async def run_backtest(
    candles: List[Candle],
    *,
    asset: str,
    timeframe: str,
    enabled_strategies: List[str],
    confidence_threshold: int,
    duration_seconds: int,
    payout_pct: float,
    starting_balance: float,
    risk_settings: RiskSettings,
    multi_timeframe_confirmation: bool = False,
    htf_candles: Optional[List[Candle]] = None,
    calibrator=None,
    regime_tracker=None,
) -> BacktestResult:
    if len(candles) < MIN_BARS + 5:
        raise ValueError(f"Not enough historical candles for a meaningful backtest (need {MIN_BARS + 5}+, got {len(candles)})")

    indicators = IndicatorCache()
    htf_indicators = IndicatorCache() if (multi_timeframe_confirmation and htf_candles) else None
    risk = RiskManager(risk_settings)

    balance = starting_balance
    trades: List[BacktestTrade] = []
    equity_curve: List[Dict[str, float]] = [{"t": candles[MIN_BARS].timestamp, "equity": balance}]
    open_position: Optional[dict] = None  # one position at a time, same rule the live orchestrator enforces
    key = f"{asset}|{timeframe}"
    htf_key = f"{asset}|htf"
    htf_idx = 0  # advances monotonically — avoids re-scanning htf_candles from the start every bar
    htf_interval_seconds: Optional[float] = None
    if htf_candles and len(htf_candles) >= 2:
        sample = htf_candles[: min(len(htf_candles), 51)]
        deltas = sorted(sample[i + 1].timestamp - sample[i].timestamp for i in range(len(sample) - 1))
        htf_interval_seconds = deltas[len(deltas) // 2]  # median, robust to occasional gaps
    stopped_early_reason: Optional[str] = None

    WINDOW = 130  # matches the live scan loop's get_candles(..., 120) + margin
    for i in range(MIN_BARS, len(candles)):
        window = candles[max(0, i + 1 - WINDOW): i + 1]
        bar = candles[i]
        edf, warm = indicators.get_enriched(key, window)

        if open_position and bar.timestamp >= open_position["expiry_ts"]:
            entry_price = open_position["entry_price"]
            direction = open_position["direction"]
            exit_price = bar.close
            if exit_price == entry_price:
                profit, status = 0.0, "draw"
            elif (exit_price > entry_price) == (direction == Direction.CALL):
                profit, status = _payout_amount(open_position["amount"], payout_pct), "win"
            else:
                profit, status = -open_position["amount"], "loss"
            balance = round(balance + profit, 2)
            risk.record_result(status, profit, bar.timestamp)
            trades.append(BacktestTrade(
                opened_at=open_position["opened_at"], closed_at=bar.timestamp, asset=asset,
                direction=direction, confidence=open_position["confidence"],
                raw_confidence=open_position.get("raw_confidence"),
                regime=open_position.get("regime"), hour_utc=open_position.get("hour_utc"),
                amount=open_position["amount"],
                payout_pct=payout_pct, entry_price=entry_price, exit_price=exit_price,
                profit=profit, status=status, strategies=open_position["strategies"],
                reasons=open_position["reasons"],
            ))
            equity_curve.append({"t": bar.timestamp, "equity": balance})
            open_position = None

        if not warm or edf is None or open_position:
            continue

        htf_bias = None
        if htf_indicators is not None and htf_candles:
            while htf_idx < len(htf_candles) and (
                (htf_candles[htf_idx].timestamp + htf_interval_seconds <= bar.timestamp)
                if htf_interval_seconds is not None
                else (htf_candles[htf_idx].timestamp <= bar.timestamp)
            ):
                htf_idx += 1
            if htf_idx >= MIN_BARS:
                htf_window = htf_candles[max(0, htf_idx - WINDOW): htf_idx]
                htf_edf, htf_warm = htf_indicators.get_enriched(htf_key, htf_window)
                if htf_warm:
                    htf_bias, _ = strategies.htf_trend_bias(htf_edf)

        res = strategies.evaluate(edf, enabled_strategies, pre_enriched=True, htf_bias=htf_bias)
        if not res.direction:
            continue
        contributing = [name for name, d in res.votes.items() if d == res.direction.value]
        regime_adj = 0
        if regime_tracker is not None:
            regime_adj = regime_tracker.blended_adjustment(contributing, res.regime)
        adjusted_confidence = max(0, min(100, res.confidence + regime_adj))
        calibrated = calibrator.calibrate(adjusted_confidence, regime=res.regime) if calibrator else adjusted_confidence
        if calibrated < confidence_threshold:
            continue
        gate = risk.can_trade(bar.timestamp, active_trades=0, max_concurrent=1, balance=balance)
        if not gate.allowed:
            continue
        amount = risk.stake_for(balance, confidence=calibrated)
        if amount > balance:
            stopped_early_reason = f"Balance ({balance}) too low to cover next stake ({amount}) — stopped here"
            break
        hour_utc = datetime.fromtimestamp(bar.timestamp, tz=timezone.utc).hour
        open_position = {
            "opened_at": bar.timestamp, "expiry_ts": bar.timestamp + duration_seconds,
            "entry_price": bar.close, "direction": res.direction,
            "confidence": calibrated, "raw_confidence": adjusted_confidence,
            "regime": res.regime, "hour_utc": hour_utc,
            "amount": amount, "strategies": contributing, "reasons": res.reasons,
        }

    # --- summary stats ------------------------------------------------- #
    closed = [t for t in trades if t.status in ("win", "loss")]
    wins = sum(1 for t in closed if t.status == "win")
    losses = len(closed) - wins
    gross_win = sum(t.profit for t in closed if t.profit > 0)
    gross_loss = abs(sum(t.profit for t in closed if t.profit < 0))
    net = round(sum(t.profit for t in trades), 2)

    peak = starting_balance
    max_dd = 0.0
    max_dd_pct = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        dd = peak - point["equity"]
        max_dd = max(max_dd, dd)
        if peak:
            max_dd_pct = max(max_dd_pct, dd / peak * 100)

    longest_win = longest_loss = run = 0
    last_status = None
    for t in closed:
        run = run + 1 if t.status == last_status else 1
        if t.status == "win":
            longest_win = max(longest_win, run)
        else:
            longest_loss = max(longest_loss, run)
        last_status = t.status

    by_strategy_agg: Dict[str, dict] = {}
    for t in closed:
        for s in (t.strategies or ["(none)"]):
            b = by_strategy_agg.setdefault(s, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
            b["trades"] += 1
            b["pnl"] = round(b["pnl"] + t.profit, 2)
            if t.status == "win":
                b["wins"] += 1
            else:
                b["losses"] += 1
    by_strategy = [
        StrategyBreakdown(name=name, win_rate=round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0.0, **v)
        for name, v in sorted(by_strategy_agg.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
    ]

    summary = BacktestSummary(
        asset=asset, timeframe=timeframe, bars_evaluated=len(candles),
        from_ts=candles[MIN_BARS].timestamp, to_ts=candles[-1].timestamp,
        starting_balance=starting_balance, ending_balance=balance,
        total_trades=len(closed), wins=wins, losses=losses,
        draws=sum(1 for t in trades if t.status == "draw"),
        win_rate=round(wins / len(closed) * 100, 1) if closed else 0.0,
        net_pnl=net, net_pnl_pct=round(net / starting_balance * 100, 2) if starting_balance else 0.0,
        profit_factor=round(gross_win / gross_loss, 2) if gross_loss else None,
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        longest_win_streak=longest_win, longest_loss_streak=longest_loss,
        stopped_early_reason=stopped_early_reason,
    )
    return BacktestResult(summary=summary, trades=trades, equity_curve=equity_curve, by_strategy=by_strategy)
