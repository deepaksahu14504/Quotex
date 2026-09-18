"""Risk manager — the mandatory gate before any order is placed.

It owns the daily counters, enforces limits, computes stake (incl. optional
capped martingale), and produces the global pause/kill state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

from ..config import RiskSettings
from ..logging_setup import log_event

logger = logging.getLogger("qat.risk")


@dataclass
class RiskGate:
    allowed: bool
    reason: str = ""


@dataclass
class RiskManager:
    settings: RiskSettings
    wins: int = 0
    losses: int = 0
    draws: int = 0
    consecutive_losses: int = 0
    trades_today: int = 0
    daily_pnl: float = 0.0
    last_loss_ts: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0
    _martingale_step: int = 0
    paused: bool = False
    pause_reason: Optional[str] = None
    # Precision Mode: per-asset consecutive-loss cooldown, separate from the
    # account-wide consecutive_losses pause above. Lets the bot keep trading
    # other assets while sitting out one that's specifically been losing.
    _asset_loss_streak: dict = field(default_factory=dict)
    _asset_cooldown_until: dict = field(default_factory=dict)

    def reset_day(self) -> None:
        self.wins = self.losses = self.draws = 0
        self.consecutive_losses = 0
        self.trades_today = 0
        self.daily_pnl = 0.0
        self._martingale_step = 0
        self.paused = False
        self.pause_reason = None

    def update_equity(self, balance: float) -> None:
        """Track peak equity for drawdown circuit breaker."""
        self.current_equity = balance
        if self.peak_equity <= 0:
            self.peak_equity = balance
        else:
            self.peak_equity = max(self.peak_equity, balance)
        self._check_drawdown()

    def _check_drawdown(self) -> None:
        s = self.settings
        if not s.drawdown_breaker_enabled or self.peak_equity <= 0:
            return
        dd = self.peak_equity - self.current_equity
        dd_pct = dd / self.peak_equity * 100
        if s.max_drawdown_amount > 0 and dd >= s.max_drawdown_amount:
            self.pause(f"Drawdown circuit breaker: -${dd:.2f} from peak")
        elif dd_pct >= s.max_drawdown_pct:
            self.pause(f"Drawdown circuit breaker: -{dd_pct:.1f}% from peak")

    def can_trade(self, now: float, active_trades: int, max_concurrent: int = 1,
                  balance: Optional[float] = None) -> RiskGate:
        if balance is not None:
            self.update_equity(balance)
        if self.paused:
            log_event(logger, logging.DEBUG, "risk_gate_rejected", reason="already_paused", pause_reason=self.pause_reason)
            return RiskGate(False, self.pause_reason or "Paused")
        if self.daily_pnl <= -abs(self.settings.daily_loss_limit):
            self.pause("Daily loss limit reached")
            log_event(logger, logging.WARNING, "risk_gate_rejected", reason="daily_loss_limit_breached",
                      daily_pnl=self.daily_pnl, daily_loss_limit=self.settings.daily_loss_limit)
            return RiskGate(False, self.pause_reason)
        if self.consecutive_losses >= self.settings.max_consecutive_losses:
            self.pause(f"{self.consecutive_losses} losses in a row")
            log_event(logger, logging.WARNING, "risk_gate_rejected", reason="max_consecutive_losses_breached",
                      consecutive_losses=self.consecutive_losses, max_consecutive_losses=self.settings.max_consecutive_losses)
            return RiskGate(False, self.pause_reason)
        if self.trades_today >= self.settings.max_trades_per_day:
            log_event(logger, logging.DEBUG, "risk_gate_rejected", reason="max_trades_per_day_reached",
                      trades_today=self.trades_today, max_trades_per_day=self.settings.max_trades_per_day)
            return RiskGate(False, "Max trades for today reached")
        if active_trades >= max_concurrent:
            log_event(logger, logging.DEBUG, "risk_gate_rejected", reason="max_concurrent_positions_open",
                      active_trades=active_trades, max_concurrent=max_concurrent)
            return RiskGate(False, f"Max {max_concurrent} positions open")
        if self.last_loss_ts and (now - self.last_loss_ts) < self.settings.cooldown_seconds:
            remaining = int(self.settings.cooldown_seconds - (now - self.last_loss_ts))
            log_event(logger, logging.DEBUG, "risk_gate_rejected", reason="post_loss_cooldown_active",
                      remaining_seconds=remaining, cooldown_seconds=self.settings.cooldown_seconds)
            return RiskGate(False, f"Cooldown {remaining}s after loss")
        log_event(logger, logging.DEBUG, "risk_gate_passed", active_trades=active_trades, max_concurrent=max_concurrent)
        return RiskGate(True)

    def stake_for(self, balance: float, confidence: Optional[int] = None, adaptive_multiplier: float = 1.0) -> float:
        s = self.settings
        if s.stake_mode == "percent":
            base = max(1.0, round(balance * (s.stake_percent / 100.0), 2))
        else:
            base = s.stake_amount
        stake_base = base

        if s.confidence_based_sizing and confidence is not None:
            # N1: previously every trade got the exact same stake regardless
            # of whether confidence barely cleared the threshold or hit 95%+
            # — no risk-adjusted sizing at all. Scale linearly between
            # confidence_size_floor (at the threshold) and
            # confidence_size_ceiling (at 100), a fractional-Kelly-style
            # de-risk on marginal signals / up-size on strong ones. Opt-in
            # (default False) so existing flat-stake behavior is unchanged
            # unless explicitly enabled.
            ref_low = float(self.settings.confidence_threshold)
            ref_high = 100.0
            if ref_high > ref_low:
                frac = (float(confidence) - ref_low) / (ref_high - ref_low)
                frac = max(0.0, min(1.0, frac))
            else:
                frac = 1.0
            multiplier = s.confidence_size_floor + frac * (s.confidence_size_ceiling - s.confidence_size_floor)
            base = round(base * multiplier, 2)

        martingale_step_applied = 0
        if s.martingale_enabled and self._martingale_step > 0:
            martingale_step_applied = min(self._martingale_step, s.martingale_max_steps)
            base = round(base * (s.martingale_multiplier ** martingale_step_applied), 2)

        # Adaptive Risk (opt-in, computed by the caller from this asset's
        # own recent trend-strength percentile -- risk.py itself has no
        # dependency on MarketContextEngine, keeping this module's own
        # dependency graph and testability unchanged). Default 1.0 is an
        # exact no-op: every existing caller that doesn't pass this
        # argument gets bit-for-bit the same stake as before. When passed,
        # it's a final, tightly-bounded nudge (never overriding stake_mode,
        # confidence-based sizing, or martingale -- it multiplies their
        # combined result) so Adaptive Risk can only modestly trim or
        # up-size stake, never dramatically change exposure.
        before_adaptive = base
        base = round(base * adaptive_multiplier, 2)
        uncapped_stake = max(1.0, base)

        # HARD SAFETY CAP (2026-08): everything above this line computes a
        # stake with zero awareness of the account balance in fixed/martingale
        # mode -- martingale multiplies the fixed stake_amount by
        # (multiplier ** step) with no ceiling of any kind. Observed in
        # production: a $5.10 base stake reached $430.80 after a losing
        # streak, on an account whose balance was in the single-to-low-double
        # digits. That is not a large trade, it is a stake that exceeds the
        # entire account -- an order like that either wipes the account if
        # the broker accepts it, or is silently ignored if the broker refuses
        # it for insufficient funds (indistinguishable, from this client, from
        # every other silent broker failure this system has hit).
        #
        # This cap is UNCONDITIONAL: it is not opt-in and cannot be bypassed
        # by stake_mode, confidence-based sizing, or martingale settings,
        # because a config mistake in any one of those is exactly the failure
        # this exists to catch.
        cap_pct = max(0.0, min(100.0, float(getattr(s, "max_stake_pct_of_balance", 25.0))))
        balance_cap = round(balance * (cap_pct / 100.0), 2) if balance > 0 else 0.0
        final_stake = min(uncapped_stake, balance_cap) if balance_cap > 0 else 0.0

        if final_stake < 1.0:
            # Even the minimum viable stake would breach the cap on this
            # balance. Reporting 0.0 rather than silently forcing $1.00 (which
            # could itself be most of a very small account) -- the caller must
            # treat 0.0 as "cannot trade" and refuse with a clear reason
            # instead of sending an order anyway.
            log_event(
                logger, logging.WARNING, "stake_capped_to_zero",
                balance=balance, uncapped_stake=uncapped_stake, cap_pct=cap_pct,
                reason="balance too low to fund even a $1 stake within the configured cap",
            )
            return 0.0

        if final_stake < uncapped_stake:
            log_event(
                logger, logging.WARNING, "stake_capped",
                balance=balance, uncapped_stake=uncapped_stake, capped_stake=final_stake,
                cap_pct=cap_pct, martingale_step=martingale_step_applied,
                reason="stake exceeded max_stake_pct_of_balance and was reduced",
            )

        log_event(
            logger, logging.DEBUG, "stake_computed",
            previous_value=stake_base, new_value=final_stake, confidence=confidence,
            confidence_based_sizing=s.confidence_based_sizing, martingale_step=martingale_step_applied,
            adaptive_multiplier=adaptive_multiplier, before_adaptive_multiplier=before_adaptive,
            uncapped_stake=uncapped_stake, balance_cap=balance_cap,
            reason="stake_mode_plus_confidence_sizing_plus_martingale_plus_adaptive_risk_plus_balance_cap",
        )
        return final_stake

    def pause(self, reason: str) -> None:
        self.paused = True
        self.pause_reason = reason
        log_event(logger, logging.WARNING, "trading_paused", reason=reason)

    # -- Precision Mode: per-asset loss-streak cooldown ------------------- #
    def asset_on_cooldown(self, asset: str, now: float) -> Optional[str]:
        """Returns a reason string if `asset` is currently sitting out a
        loss-streak cooldown, else None."""
        until = self._asset_cooldown_until.get(asset)
        if until and now < until:
            remaining = int(until - now)
            return f"{asset} on precision cooldown ({remaining}s remaining after consecutive losses)"
        return None

    def record_asset_result(self, asset: str, status: str, now: float,
                             streak_limit: int, cooldown_seconds: float) -> None:
        """Call once per closed trade (any mode) to maintain the per-asset
        streak. Only actually enforced when Precision Mode reads
        asset_on_cooldown() — safe to always record."""
        if status == "loss":
            streak = self._asset_loss_streak.get(asset, 0) + 1
            self._asset_loss_streak[asset] = streak
            if streak >= streak_limit:
                self._asset_cooldown_until[asset] = now + cooldown_seconds
        elif status == "win":
            self._asset_loss_streak[asset] = 0
            self._asset_cooldown_until.pop(asset, None)
        # draws don't reset or extend the streak

    def extend_cooldown(self, asset: str, seconds: float, now: float) -> None:
        """Externally impose or extend a cooldown on `asset`, independent of
        the loss-streak mechanism above. Only ever lengthens the cooldown
        (never shortens one already in place from record_asset_result) --
        used by Pattern Risk Engine enforcement to react to an elevated
        risk score for a trade that already executed, by delaying the
        *next* one on this asset rather than trying to (unsafely) unwind
        the trade that already happened. `asset_on_cooldown()` is the only
        thing that reads this, and only when Precision Mode is on -- safe
        to call unconditionally."""
        if seconds <= 0:
            return
        until = now + seconds
        current = self._asset_cooldown_until.get(asset, 0.0)
        self._asset_cooldown_until[asset] = max(current, until)

    def resume(self) -> None:
        previous_reason = self.pause_reason
        self.paused = False
        self.pause_reason = None
        # Forgive the streak/cooldown when the user manually resumes.
        self.consecutive_losses = 0
        self.last_loss_ts = 0.0
        log_event(logger, logging.INFO, "trading_resumed", previous_pause_reason=previous_reason)
        # Reset peak to current equity so user gets a fresh drawdown baseline.
        if self.current_equity > 0:
            self.peak_equity = self.current_equity

    def record_result(self, status: str, profit: float, now: float) -> None:
        self.trades_today += 1
        self.daily_pnl = round(self.daily_pnl + profit, 2)
        if status == "win":
            self.wins += 1
            self.consecutive_losses = 0
            self._martingale_step = 0
        elif status == "loss":
            self.losses += 1
            self.consecutive_losses += 1
            self.last_loss_ts = now
            if self.settings.martingale_enabled:
                self._martingale_step += 1
        else:  # draw / error
            self.draws += 1
