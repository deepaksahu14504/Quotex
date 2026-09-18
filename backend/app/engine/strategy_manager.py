"""Strategy Performance Manager.

Every strategy in engine/strategies.py votes on equal footing today, forever,
regardless of how it's actually performing for this user. This tracks each
strategy's rolling real-trade win rate and automatically mutes one that's
losing consistently — so a strategy that stops working for current market
conditions gets sidelined without the user having to notice it in Insights
and manually turn it off.

State machine per strategy:
  active   -> normal, votes count, contributes to signals
  muted    -> excluded from voting; sitting out a cooldown
  trial    -> muted strategy's cooldown elapsed; temporarily voting again to
              collect a fresh small sample before a real verdict

A user can always override the automation (force mute or force active) via
the API; while overridden, automatic mute/reactivate logic is skipped for
that strategy until the override is cleared.

Persisted to disk (per user) so state survives a backend restart.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from ..logging_setup import log_event
from .analytics_engine import wilson_interval, z_for_confidence_level

logger = logging.getLogger("qat.strategy_manager")
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

DEFAULT_TYPICAL_PAYOUT_PCT = 80.0   # conservative reference payout used to derive the breakeven-based
                            # thresholds below -- a strategy's actual assets may pay more or
                            # less, but there's no single "the" payout at the strategy-state
                            # level (one strategy trades many assets), so this is a deliberately
                            # conservative stand-in, same spirit as rejection_reconciliation.py's
                            # typical_payout_pct default. Tunable via TradingSettings.strategy_typical_payout_pct.

DEFAULT_ROLLING_WINDOW = 50        # trades considered for an active strategy's win rate -- must
                           # comfortably exceed min_sample below (25 would make a
                           # min_sample of 30 mathematically unreachable, since results
                           # are capped at rolling_window). Tunable via TradingSettings.strategy_rolling_window.
DEFAULT_MIN_SAMPLE = 30            # minimum trades before we'll act on performance at all -- the
                           # standard n>=30 rule of thumb for approximate normality in a
                           # binomial proportion; also matches min_sample_size=30 already
                           # used in pattern_risk_validation.py and rejection_reconciliation.py.
                           # Tunable via TradingSettings.strategy_min_sample.
DEFAULT_TRIAL_SIZE = 20            # trades given during a reactivation trial -- raised from 8,
                           # still deliberately smaller than min_sample (a trial is a
                           # faster probation check, not the full mute-confirmation bar),
                           # combined with the Wilson-lower-bound trial-pass check already
                           # in place, which further compensates for the still-modest n.
                           # Tunable via TradingSettings.strategy_trial_size.
DEFAULT_BASE_COOLDOWN = 6 * 3600.0        # first cooldown before a trial: 6 hours
DEFAULT_MAX_COOLDOWN = 3 * 24 * 3600.0    # cooldown cap: 3 days
EWMA_MIN_OBSERVATIONS = 5   # minimum (win/loss) updates before an EWMA estimate is trusted for
                            # the fast-mute check -- small on purpose (EWMA's whole point is
                            # reacting faster than the rolling-window's min_sample floor allows),
                            # but not so small that 1-2 early losses alone can trigger it


def _mute_confirmed(results: List[Tuple[str, float]], threshold_pct: float, z: float = 1.96) -> bool:
    """Audit fix (H-1): at small samples, a raw win-rate comparison against
    the mute threshold is dominated by noise -- the standard error of a win
    rate at n=12 is roughly +/-14 points, so a genuinely-fine strategy running
    near 50% true win rate has a real chance of reading below the mute bar by
    chance alone, and re-checking on every new trade (rather than once)
    compounds that further. This mirrors the Wilson-lower-bound approach
    already used by recovery_engine.py and analytics_engine.py for the same
    class of "is this real or noise" question: instead of muting on the raw
    win rate, we require the Wilson *upper* bound (the most optimistic true
    win rate consistent with the sample, at the configured confidence level)
    to already sit at or below the mute threshold -- i.e. we only mute once
    the data makes a real case against the strategy, not just a bad short
    run. This naturally demands more evidence at small samples without
    changing the minimum-sample gate itself. `z` defaults to the standard
    95% two-sided value but is settable via
    TradingSettings.adaptive_wilson_confidence_level (see z_for_confidence_level())."""
    closed = [r for r in results if r[0] in ("win", "loss")]
    if not closed:
        return False
    wins = sum(1 for r in closed if r[0] == "win")
    _, upper = wilson_interval(wins, len(closed), z=z)
    return (upper * 100) <= threshold_pct


@dataclass
class StrategyState:
    name: str
    results: List[Tuple[str, float]] = field(default_factory=list)   # rolling (status, profit)
    status: str = "active"           # active | muted | trial
    muted_at: Optional[float] = None
    mute_reason: Optional[str] = None
    cooldown_seconds: float = DEFAULT_BASE_COOLDOWN
    trial_results: List[Tuple[str, float]] = field(default_factory=list)
    manual_override: Optional[str] = None   # "active" | "muted" | None
    # N3: per-regime rolling results, tracked *alongside* the global ones
    # above (additive -- the existing global mute/trial state machine is
    # untouched). A strategy that's globally fine but has been losing
    # specifically in the *current* regime gets soft-excluded from voting
    # for that regime only, via active_strategies(regime=...). New saved
    # states will have this; old persisted JSON files simply default to an
    # empty dict here (dataclass default), so this is backward compatible
    # with state files from before N3.
    regime_results: Dict[str, List[Tuple[str, float]]] = field(default_factory=dict)
    # Optional exponentially-weighted win-rate estimate, maintained
    # alongside (never instead of) the plain rolling-window results above.
    # Stays None -- and is never read anywhere -- unless
    # TradingSettings.adaptive_learning_ewma_alpha is explicitly set.
    ewma_win_rate: Optional[float] = None
    ewma_observations: int = 0  # count of (win/loss) updates folded into ewma_win_rate so far --
    # used only to require a small minimum before trusting it (below, _EWMA_MIN_OBSERVATIONS)

    def win_rate(self, results: List[Tuple[str, float]]) -> float:
        closed = [r for r in results if r[0] in ("win", "loss")]
        if not closed:
            return 0.0
        wins = sum(1 for r in closed if r[0] == "win")
        return round(wins / len(closed) * 100, 1)


class StrategyPerformanceManager:
    def __init__(
        self, data_dir: Path, *,
        min_sample: int = DEFAULT_MIN_SAMPLE, trial_size: int = DEFAULT_TRIAL_SIZE,
        rolling_window: int = DEFAULT_ROLLING_WINDOW, typical_payout_pct: float = DEFAULT_TYPICAL_PAYOUT_PCT,
        base_cooldown: float = DEFAULT_BASE_COOLDOWN, max_cooldown: float = DEFAULT_MAX_COOLDOWN,
        wilson_confidence_level: float = 0.95,
        ewma_alpha: Optional[float] = None, drift_weight: Optional[float] = None,
    ) -> None:
        self._file = data_dir / "strategy_performance.json"
        self._states: Dict[str, StrategyState] = {}
        self.min_sample = min_sample
        self.trial_size = trial_size
        self.rolling_window = max(rolling_window, min_sample + 1)  # must exceed min_sample or
        # the mute check becomes mathematically unreachable (results are capped at rolling_window)
        self.typical_payout_pct = typical_payout_pct
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self.wilson_confidence_level = wilson_confidence_level  # -> z via z_for_confidence_level(),
        # controls EVERY Wilson-bound calculation in this file
        # Both None by default (TradingSettings.adaptive_learning_ewma_alpha
        # / adaptive_learning_drift_weight) -- every codepath that reads
        # these checks for None first and is a complete no-op when unset,
        # so leaving them unset reproduces the exact pre-existing behavior.
        self.ewma_alpha = ewma_alpha
        self.drift_weight = drift_weight
        self._load()

    @property
    def _z(self) -> float:
        return z_for_confidence_level(self.wilson_confidence_level)

    @property
    def breakeven_win_rate(self) -> float:
        """Payout-implied breakeven win rate (Phase 6) -- recomputed live
        from typical_payout_pct so changing that setting immediately
        updates the mute/watch/trial-pass bars without a restart."""
        return round(100.0 / (1.0 + self.typical_payout_pct / 100.0), 1)

    @property
    def mute_win_rate(self) -> float:
        return self.breakeven_win_rate

    @property
    def trial_pass_win_rate(self) -> float:
        return self.breakeven_win_rate

    @property
    def watch_win_rate(self) -> float:
        return self.breakeven_win_rate

    # -- persistence ---------------------------------------------------- #
    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            for name, d in raw.items():
                d["results"] = [tuple(r) for r in d.get("results", [])]
                d["trial_results"] = [tuple(r) for r in d.get("trial_results", [])]
                d["regime_results"] = {
                    regime: [tuple(r) for r in results]
                    for regime, results in d.get("regime_results", {}).items()
                }
                self._states[name] = StrategyState(**d)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            raw = {name: asdict(s) for name, s in self._states.items()}
            self._file.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _get(self, name: str) -> StrategyState:
        if name not in self._states:
            self._states[name] = StrategyState(name=name)
        return self._states[name]

    # -- recording -------------------------------------------------------- #
    def record_trade(self, strategy_names: List[str], status: str, profit: float,
                      regime: Optional[str] = None) -> List[str]:
        """Call once per closed trade with the strategies that voted for its
        direction. Returns names whose status just changed (for a UI/Telegram
        notice), so the caller can announce it. `regime` is optional (N3) —
        when provided, results are also folded into a per-regime rolling
        window used only by active_strategies(regime=...) for a soft,
        regime-specific voting exclusion; it never affects the global
        mute/trial state machine below."""
        if status not in ("win", "loss", "draw"):
            return []
        changed: List[str] = []
        for name in strategy_names or []:
            st = self._get(name)
            if regime:
                bucket = st.regime_results.setdefault(regime, [])
                bucket.append((status, profit))
                st.regime_results[regime] = bucket[-self.rolling_window:]
            if st.status == "trial":
                st.trial_results.append((status, profit))
                st.trial_results = st.trial_results[-self.trial_size:]
                if len(st.trial_results) >= self.trial_size and st.manual_override is None:
                    trial_wr = st.win_rate(st.trial_results)
                    trial_wins = sum(1 for r in st.trial_results if r[0] == "win")
                    trial_closed = sum(1 for r in st.trial_results if r[0] in ("win", "loss"))
                    trial_lower, _ = wilson_interval(trial_wins, trial_closed, z=self._z) if trial_closed else (0.0, 0.0)
                    # Wilson LOWER bound (the most pessimistic true win rate
                    # consistent with the sample) must clear the pass bar --
                    # not just the raw average -- before reactivating. trial_size
                    # is even noisier than min_sample, which is why
                    # _mute_confirmed() already requires this same standard of
                    # evidence for the opposite (mute) decision; reactivation
                    # deserves at least as much caution.
                    if (trial_lower * 100) >= self.trial_pass_win_rate:
                        st.status = "active"
                        st.results = list(st.trial_results)
                        st.trial_results = []
                        st.muted_at = None
                        st.mute_reason = None
                        st.cooldown_seconds = self.base_cooldown
                        changed.append(name)
                        log_event(
                            logger, logging.INFO, "strategy_state_transition",
                            strategy=name, previous_status="trial", new_status="active",
                            trial_win_rate=trial_wr, trial_wilson_lower=round(trial_lower * 100, 2),
                            trial_pass_threshold=self.trial_pass_win_rate,
                            reason="trial_passed",
                        )
                    else:
                        st.status = "muted"
                        st.muted_at = time.time()
                        st.mute_reason = f"Trial failed — {trial_wr}% over {self.trial_size} trades"
                        st.cooldown_seconds = min(st.cooldown_seconds * 2, self.max_cooldown)
                        st.trial_results = []
                        changed.append(name)
                        log_event(
                            logger, logging.INFO, "strategy_state_transition",
                            strategy=name, previous_status="trial", new_status="muted",
                            trial_win_rate=trial_wr, trial_wilson_lower=round(trial_lower * 100, 2),
                            trial_pass_threshold=self.trial_pass_win_rate,
                            new_cooldown_seconds=st.cooldown_seconds, reason="trial_failed",
                        )
            elif st.status == "active":
                st.results.append((status, profit))
                st.results = st.results[-self.rolling_window:]
                if self.ewma_alpha is not None and status in ("win", "loss"):
                    outcome = 1.0 if status == "win" else 0.0
                    prev = st.ewma_win_rate if st.ewma_win_rate is not None else outcome
                    st.ewma_win_rate = self.ewma_alpha * outcome + (1.0 - self.ewma_alpha) * prev
                    st.ewma_observations += 1
                muted_this_round = False
                if st.manual_override is None and len(st.results) >= self.min_sample:
                    wr = st.win_rate(st.results)
                    if _mute_confirmed(st.results, self.mute_win_rate, z=self._z):
                        st.status = "muted"
                        st.muted_at = time.time()
                        st.mute_reason = f"Win rate {wr}% over last {len(st.results)} trades (statistically confirmed)"
                        st.cooldown_seconds = self.base_cooldown
                        changed.append(name)
                        muted_this_round = True
                        log_event(
                            logger, logging.INFO, "strategy_state_transition",
                            strategy=name, previous_status="active", new_status="muted",
                            win_rate=wr, sample_size=len(st.results), mute_threshold=self.mute_win_rate,
                            new_cooldown_seconds=st.cooldown_seconds, reason="win_rate_significantly_below_mute_threshold",
                        )
                if (not muted_this_round and self.ewma_alpha is not None and st.manual_override is None
                        and st.status == "active" and st.ewma_win_rate is not None
                        and st.ewma_observations >= EWMA_MIN_OBSERVATIONS
                        and (st.ewma_win_rate * 100) < self.mute_win_rate):
                    # Fast path: the rolling-window/Wilson check above needs
                    # min_sample trades and statistical confirmation before
                    # it will mute -- deliberately conservative. This EWMA
                    # check trades some of that statistical rigor for speed,
                    # which is the entire point of enabling it: catch a
                    # sudden, sharp performance drop sooner than a flat
                    # window average would. It's a point-estimate check, not
                    # Wilson-bound, so it's noisier -- that's the accepted
                    # tradeoff for reacting faster, same tradeoff the
                    # feature's own name implies.
                    st.status = "muted"
                    st.muted_at = time.time()
                    st.mute_reason = f"EWMA win rate {st.ewma_win_rate*100:.1f}% (fast-reacting check, alpha={self.ewma_alpha})"
                    st.cooldown_seconds = self.base_cooldown
                    changed.append(name)
                    log_event(
                        logger, logging.INFO, "strategy_state_transition",
                        strategy=name, previous_status="active", new_status="muted",
                        ewma_win_rate=round(st.ewma_win_rate * 100, 1), ewma_alpha=self.ewma_alpha,
                        ewma_observations=st.ewma_observations, mute_threshold=self.mute_win_rate,
                        new_cooldown_seconds=st.cooldown_seconds, reason="ewma_fast_mute",
                    )
            # muted strategies don't vote, so they can't accumulate results
            # here — see maybe_start_trials() for how they come back.
        if strategy_names:
            self._save()
        return changed

    def maybe_start_trials(self) -> List[str]:
        """Call periodically (e.g. once per scan). Moves any muted strategy
        whose cooldown has elapsed into a trial (voting again, on probation)."""
        started = []
        now = time.time()
        for name, st in self._states.items():
            if st.status == "muted" and st.manual_override is None and st.muted_at:
                if now - st.muted_at >= st.cooldown_seconds:
                    st.status = "trial"
                    st.trial_results = []
                    started.append(name)
        if started:
            self._save()
        return started

    # -- gating for the signal engine ------------------------------------ #
    def active_strategies(self, configured: List[str], regime: Optional[str] = None) -> List[str]:
        """Strategies that should actually vote right now: configured AND
        not currently muted (active, trial, or manually forced-active all
        vote; only a true "muted" sit-out is excluded).

        N3: when `regime` is given and a strategy has enough of its own
        regime-specific sample (>= min_sample) with a poor win rate there
        (<= mute_win_rate), it's also excluded for *this regime only* even
        though its global state is still "active" — e.g. a trend-following
        strategy performing fine overall but losing consistently in range
        conditions specifically. This only ever adds an extra exclusion; it
        never overrides a global mute/manual-override into voting."""
        out = []
        for name in configured:
            st = self._states.get(name)
            if st is None:
                out.append(name)  # no history yet -> fully active by default
                continue
            if st.manual_override == "muted":
                continue
            if st.manual_override == "active":
                out.append(name)
                continue
            if st.status == "muted":
                continue
            if regime:
                regime_sample = st.regime_results.get(regime, [])
                closed = [r for r in regime_sample if r[0] in ("win", "loss")]
                if len(closed) >= self.min_sample and _mute_confirmed(regime_sample, self.mute_win_rate, z=self._z):
                    continue  # soft, regime-specific sit-out -- global status is untouched
            out.append(name)
        return out

    # -- Dynamic Weighted Ensemble ----------------------------------------- #
    def performance_multiplier(
        self, name: str, floor: float = 0.7, ceiling: float = 1.3,
        low_wr: float = 30.0, mid_wr: float = 50.0, high_wr: float = 70.0,
        regime: Optional[str] = None, regime_blend: float = 0.0,
    ) -> float:
        """Maps a strategy's rolling win rate to a vote-weight multiplier,
        clamped to [floor, ceiling] so no single strategy can dominate or
        get silenced purely by this mechanism — a strategy that's actually
        performing badly enough to matter gets muted by the existing
        active/trial state machine instead, which is a harder, separate
        decision. 50% win rate maps to 1.0 (neutral); returns 1.0 with an
        insufficient sample (nothing to react to yet).

        low_wr/mid_wr/high_wr are the win-rate breakpoints for this curve
        (Stage A: configurable via TradingSettings strategy_mute_win_rate /
        strategy_trial_win_rate / strategy_promote_win_rate -- these names
        describe this multiplier curve, not the separate mute/trial state
        machine below, which has its own mute_win_rate/trial_pass_win_rate
        constants). Defaults (30/50/70) are unchanged from before.

        Dynamic Strategy Intelligence (opt-in, regime_blend default 0.0 =
        no change from prior behavior): when regime and regime_blend>0 are
        given, blends this strategy's REGIME-SPECIFIC win rate (already
        tracked in st.regime_results by record_trade(), previously used
        only for active_strategies()'s soft voting exclusion) into the win
        rate this multiplier reacts to -- a strategy performing well
        overall but poorly in the CURRENT regime gets a lower multiplier
        than a pure global win rate would give it, and vice versa. Falls
        back to the pure global win rate if there's no regime-specific
        sample yet (cold-start for that regime), same min_sample gate as
        the global check.
        """
        st = self._states.get(name)
        if st is None:
            return 1.0
        sample = st.results
        closed = [r for r in sample if r[0] in ("win", "loss")]
        if len(closed) < self.min_sample:
            return 1.0
        wr = st.win_rate(sample)  # 0-100

        if regime and regime_blend > 0:
            regime_sample = st.regime_results.get(regime, [])
            regime_closed = [r for r in regime_sample if r[0] in ("win", "loss")]
            if len(regime_closed) >= self.min_sample:
                regime_wr = st.win_rate(regime_sample)
                blend = min(max(regime_blend, 0.0), 1.0)
                wr = (1.0 - blend) * wr + blend * regime_wr

        if wr <= low_wr:
            result = floor
            reason = "win_rate_at_or_below_low_band"
        elif wr >= high_wr:
            result = ceiling
            reason = "win_rate_at_or_above_high_band"
        elif wr <= mid_wr:
            frac = (wr - low_wr) / (mid_wr - low_wr)
            result = floor + frac * (1.0 - floor)
            reason = "win_rate_below_midpoint_interpolated"
        else:
            frac = (wr - mid_wr) / (high_wr - mid_wr)
            result = 1.0 + frac * (ceiling - 1.0)
            reason = "win_rate_above_midpoint_interpolated"
        log_event(
            logger, logging.DEBUG, "performance_multiplier_computed",
            strategy=name, previous_value=1.0, new_value=round(result, 4), win_rate=wr,
            regime=regime, regime_blend=regime_blend if regime and regime_blend > 0 else 0.0,
            reason=reason,
        )
        return result

    # -- manual control ---------------------------------------------------- #
    def force_trial(self, name: str, reason: str) -> bool:
        """Force a strategy directly into "trial" state, bypassing the
        normal mute-then-wait-for-cooldown-then-trial sequence -- driven by
        drift_detection.py's Page-Hinkley alerts (Phase 4), an independent
        signal that a strategy's edge has structurally SHIFTED, not just
        that its raw win rate has degraded enough to cross the existing
        `_mute_confirmed()` threshold (which needs min_sample losing
        trades to even become eligible, by which point a real shift has
        already been costing money for a while).

        Only acts when the strategy is currently "active" with no manual
        override: a drift alert for an already-muted or already-in-trial
        strategy adds no new information the existing cooldown/trial
        machinery isn't already handling, and forcing a fresh trial early
        for an ALREADY-muted strategy would prematurely cut short a
        cooldown that exists for good reason. Returns True if the state
        actually changed."""
        st = self._get(name)
        if st.manual_override is not None or st.status != "active":
            return False
        st.status = "trial"
        st.trial_results = []
        st.muted_at = time.time()
        st.mute_reason = reason
        self._save()
        log_event(
            logger, logging.WARNING, "strategy_state_transition",
            strategy=name, previous_status="active", new_status="trial",
            reason="drift_detected", detail=reason,
        )
        return True

    def set_override(self, name: str, override: Optional[str]) -> None:
        st = self._get(name)
        st.manual_override = override
        if override == "active":
            st.status = "active"
            st.muted_at = None
            st.mute_reason = None
        elif override == "muted":
            st.status = "muted"
            st.muted_at = time.time()
            st.mute_reason = "Manually disabled"
        else:
            # Override cleared — re-derive status from actual rolling stats
            # instead of leaving it stuck in whatever the override forced.
            if len(st.results) >= self.min_sample and _mute_confirmed(st.results, self.mute_win_rate, z=self._z):
                st.status = "muted"
                st.muted_at = time.time()
                st.mute_reason = f"Win rate {st.win_rate(st.results)}% over last {len(st.results)} trades (statistically confirmed)"
            else:
                st.status = "active"
                st.muted_at = None
                st.mute_reason = None
        self._save()

    # -- reporting ---------------------------------------------------------- #
    def status_for_ui(self, configured: List[str]) -> List[dict]:
        out = []
        for name in configured:
            st = self._states.get(name) or StrategyState(name=name)
            sample = st.trial_results if st.status == "trial" else st.results
            wr = st.win_rate(sample)
            watching = st.status == "active" and 0 < len(sample) < self.min_sample and wr < self.watch_win_rate
            next_trial_at = (st.muted_at + st.cooldown_seconds) if (st.status == "muted" and st.muted_at) else None
            regime_breakdown = {
                regime: {"trades": len([r for r in results if r[0] in ("win", "loss")]),
                         "win_rate": st.win_rate(results)}
                for regime, results in st.regime_results.items()
                if any(r[0] in ("win", "loss") for r in results)
            }
            out.append({
                "name": name,
                "status": "watching" if watching else st.status,
                "win_rate": wr,
                "trades": len([r for r in sample if r[0] in ("win", "loss")]),
                "mute_reason": st.mute_reason,
                "next_trial_at": next_trial_at,
                "manual_override": st.manual_override,
                "regime_breakdown": regime_breakdown,
            })
        return out
