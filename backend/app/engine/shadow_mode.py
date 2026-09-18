"""Shadow Mode — validate signal-pipeline changes against live market data
without risking real money.

When enabled, every signal that clears the full pipeline (confluence,
threshold, risk gate, Precision Mode, Dynamic Weighted Ensemble, adaptive
duration, payout-aware threshold — all of it, unmodified) is recorded as a
ShadowTrade instead of being sent to the broker. At its expiry time, the
outcome is determined the same way binary options actually resolve: entry
price vs. price at expiry, using the same price feed the bot already reads
from (no broker order needed to know who would have won).

Deliberately kept separate from the real trading path's state:
  - Not fed into strategy_manager / calibration / risk.py's live counters.
    Feeding simulated outcomes into the same statistics used for real
    trading decisions would let a bug in this module's own outcome
    calculation quietly corrupt real decision-making. Shadow stats are
    purely observational.
  - Persisted to their own per-user file, never touching the real trade
    store or SQLite trade history.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

from ..logging_setup import log_event

logger = logging.getLogger("qat.shadow_mode")


@dataclass
class ShadowTrade:
    id: str
    asset: str
    direction: str          # "call" | "put"
    entry_price: float
    confidence: int
    regime: str
    strategies: List[str]
    opened_at: float
    duration: float
    payout: float
    resolved: bool = False
    exit_price: Optional[float] = None
    status: Optional[str] = None       # "win" | "loss" | "draw" (once resolved)
    simulated_profit: Optional[float] = None  # against a fixed notional stake, for a consistent win-rate/PnL view


class ShadowStore:
    """Simple JSON-file-backed store, per user. Small volume (one entry per
    shadow signal), so a plain file is fine -- no need for SQLite here."""

    NOTIONAL_STAKE = 10.0  # fixed reference stake so simulated PnL is comparable trade-to-trade

    def __init__(self, storage_path: str) -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._trades: Dict[str, ShadowTrade] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for d in raw:
                self._trades[d["id"]] = ShadowTrade(**d)
        except Exception:
            pass  # corrupt/missing file -> start fresh rather than crash

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps([asdict(t) for t in self._trades.values()], indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass  # shadow stats are observational -- never let a disk hiccup here affect the caller

    def add(self, trade: ShadowTrade) -> None:
        self._trades[trade.id] = trade
        self._save()
        log_event(logger, logging.DEBUG, "shadow_trade_opened",
                  trade_id=trade.id, asset=trade.asset, direction=trade.direction,
                  entry_price=trade.entry_price, duration=trade.duration, payout=trade.payout)

    def pending_due(self, now: float) -> List[ShadowTrade]:
        return [t for t in self._trades.values() if not t.resolved and (t.opened_at + t.duration) <= now]

    def resolve(self, trade_id: str, exit_price: float) -> Optional[ShadowTrade]:
        t = self._trades.get(trade_id)
        if t is None or t.resolved:
            return None
        t.exit_price = exit_price
        if exit_price == t.entry_price:
            t.status = "draw"
            t.simulated_profit = 0.0
        else:
            won = (exit_price > t.entry_price) if t.direction == "call" else (exit_price < t.entry_price)
            t.status = "win" if won else "loss"
            t.simulated_profit = (
                round(self.NOTIONAL_STAKE * (t.payout / 100.0), 2) if won else -self.NOTIONAL_STAKE
            )
        t.resolved = True
        self._save()
        log_event(logger, logging.DEBUG, "shadow_trade_resolved",
                  trade_id=trade_id, asset=t.asset, status=t.status,
                  entry_price=t.entry_price, exit_price=exit_price, simulated_profit=t.simulated_profit)
        return t

    def all_resolved(self, limit: int = 500) -> List[ShadowTrade]:
        """Public, read-only accessor for resolved shadow trades, most
        recent first -- added for RecoveryEngine, which needs the raw
        trade objects (not just the aggregated dict stats() returns) to
        do its own recency-ordered consistency check."""
        resolved = [t for t in self._trades.values() if t.resolved]
        resolved.sort(key=lambda t: t.opened_at, reverse=True)
        return resolved[:limit]

    def stats(self, limit: int = 500) -> dict:
        resolved = [t for t in self._trades.values() if t.resolved]
        resolved.sort(key=lambda t: t.opened_at, reverse=True)
        resolved = resolved[:limit]
        wins = sum(1 for t in resolved if t.status == "win")
        losses = sum(1 for t in resolved if t.status == "loss")
        total = wins + losses
        pnl = sum(t.simulated_profit or 0.0 for t in resolved)
        return {
            "total_resolved": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else None,
            "simulated_pnl": round(pnl, 2),
            "pending": len([t for t in self._trades.values() if not t.resolved]),
        }
