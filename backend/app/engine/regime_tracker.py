"""Market Regime Detection.

The strategy engine already classifies each bar as "trend" (ADX >= 25),
"range" (ADX < 18), or "mixed" (see `strategies._regime`) — but that label
was only ever used for a cosmetic "Regime: trend (ADX 32)" line in a
signal's reasons, never acted on. Some strategies (Supertrend, EMA Ribbon,
ADX Trend) are trend-followers that should do better in a trending regime;
others (Mean Reversion, Bollinger Bounce, StochRSI) are built for chop and
should do better ranging. This tracks each strategy's REAL win rate broken
down by regime and nudges confidence up or down when the current regime
matches (or doesn't match) what that strategy has actually been good at —
without ever fully vetoing a strategy the way Strategy Performance Manager's
mute does; this is a soft weighting on top of it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MIN_SAMPLES = 10          # minimum (strategy, regime) trades before adjusting anything
MAX_ADJUSTMENT = 8        # cap on the confidence nudge, either direction
REFRESH_EVERY_N_TRADES = 10
REFRESH_MAX_AGE = 3600.0

REGIME_LABELS = {"trend": "Trending", "range": "Ranging", "mixed": "Mixed"}


@dataclass
class RegimeCell:
    regime: str
    trades: int
    wins: int

    @property
    def win_rate(self) -> float:
        return round(self.wins / self.trades * 100, 1) if self.trades else 0.0


@dataclass
class RegimePerformanceTracker:
    _cells: Dict[Tuple[str, str], RegimeCell] = field(default_factory=dict)
    _overall: Dict[str, float] = field(default_factory=dict)  # strategy -> overall win rate across regimes
    _built: bool = False
    _last_built_at: float = 0.0
    _last_built_count: int = 0

    def needs_refresh(self, current_closed_count: int) -> bool:
        if not self._built:
            return current_closed_count >= MIN_SAMPLES
        if current_closed_count - self._last_built_count >= REFRESH_EVERY_N_TRADES:
            return True
        return (time.time() - self._last_built_at) >= REFRESH_MAX_AGE

    def refresh(self, trades) -> None:
        cells: Dict[Tuple[str, str], RegimeCell] = {}
        per_strategy_totals: Dict[str, List[int]] = {}  # strategy -> [trades, wins]
        closed = 0
        for t in trades:
            status = str(getattr(t.status, "value", t.status))
            if status not in ("win", "loss"):
                continue
            regime = getattr(t, "regime", None)
            if not regime:
                continue
            closed += 1
            for strat in (getattr(t, "strategies", None) or ["(none)"]):
                key = (strat, regime)
                cell = cells.setdefault(key, RegimeCell(regime=regime, trades=0, wins=0))
                cell.trades += 1
                if status == "win":
                    cell.wins += 1
                totals = per_strategy_totals.setdefault(strat, [0, 0])
                totals[0] += 1
                if status == "win":
                    totals[1] += 1

        self._cells = cells
        self._overall = {s: (w / t * 100 if t else 0.0) for s, (t, w) in per_strategy_totals.items()}
        self._last_built_at = time.time()
        self._last_built_count = closed
        self._built = len(cells) > 0

    def adjustment(self, strategy_name: str, regime: str) -> int:
        """Confidence-point nudge (-MAX_ADJUSTMENT..+MAX_ADJUSTMENT) for this
        strategy given the current regime, based on how much better/worse it
        performs in this regime vs its own average performance in OTHER
        regimes. Zero until there's enough of a sample for this specific
        (strategy, regime) pair.

        Audit fix (L-4): previously compared against self._overall, which is
        this strategy's win rate blended across ALL regimes -- including the
        very cell being evaluated. That self-inclusion biases the delta
        toward zero, especially when one regime dominates a strategy's trade
        count (e.g. if 80% of a strategy's trades happened in "trend",
        overall ~= trend's own win rate, so trend's delta collapses toward 0
        even if trend genuinely is this strategy's best regime, while the
        thinner regimes get comparatively exaggerated deltas from the same
        effect). Using a leave-one-out baseline (performance in every OTHER
        regime) removes that bias."""
        cell = self._cells.get((strategy_name, regime))
        if not cell or cell.trades < MIN_SAMPLES:
            return 0
        other_trades = 0
        other_wins = 0
        for (strat, cell_regime), other_cell in self._cells.items():
            if strat == strategy_name and cell_regime != regime:
                other_trades += other_cell.trades
                other_wins += other_cell.wins
        if other_trades == 0:
            return 0  # no out-of-regime baseline to compare against yet
        baseline = other_wins / other_trades * 100
        delta = cell.win_rate - baseline
        return int(max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, round(delta / 5))))

    def blended_adjustment(self, strategy_names: List[str], regime: str) -> int:
        """Average adjustment across the strategies that actually voted for
        the signal's direction — this is what the orchestrator applies to
        the raw confluence score."""
        if not strategy_names:
            return 0
        adjustments = [self.adjustment(name, regime) for name in strategy_names]
        return int(round(sum(adjustments) / len(adjustments)))

    def status_for_ui(self, configured: List[str]) -> List[dict]:
        out = []
        for name in configured:
            row = {"name": name, "overall_win_rate": round(self._overall.get(name, 0.0), 1), "regimes": {}}
            for regime in ("trend", "range", "mixed"):
                cell = self._cells.get((name, regime))
                row["regimes"][regime] = {
                    "label": REGIME_LABELS[regime],
                    "trades": cell.trades if cell else 0,
                    "win_rate": cell.win_rate if cell else 0.0,
                    "adjustment": self.adjustment(name, regime),
                }
            out.append(row)
        return out
