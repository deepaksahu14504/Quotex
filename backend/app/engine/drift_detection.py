"""Drift detection per (strategy, regime) -- Phase 4.

Uses the Page-Hinkley test, a sequential change-detection algorithm, to
detect a structural shift in a strategy's win rate within a specific
regime, DIRECTLY -- rather than waiting for the existing win-rate mute
check (strategy_manager.py's `_mute_confirmed()`, gated at MIN_SAMPLE=30 as of Phase 6)
to eventually notice the symptom once performance has degraded enough to
cross a fixed threshold.

Why Page-Hinkley specifically (not a fixed rolling-window comparison):
a naive "compare last N vs previous N" approach requires picking an
arbitrary window size, and is blind to a change that happens mid-window
(it gets averaged away). Page-Hinkley instead accumulates evidence of a
sustained shift from a running baseline and fires as soon as that
accumulated evidence crosses a threshold -- with a known, tunable
false-alarm rate, not an ad hoc window choice.

Feeds `StrategyPerformanceManager.force_trial()` (strategy_manager.py) as
an ADDITIONAL, INDEPENDENT trigger into "trial" state -- the existing
mute/trial/cooldown machinery is completely untouched; this only adds a
second way to get from "active" to "trial" faster when there's direct
statistical evidence of a shift, not just a symptom.

IMPORTANT CALIBRATION NOTE (do not skip): `delta` and `threshold` below
are literature-standard STARTING POINTS (matching common defaults for a
[0,1]-scale stream, e.g. scikit-multiflow's PageHinkley), not values
derived from this system's own observed variance. Per the earlier audit,
a drift detector's sensitivity must eventually be justified against the
actual variance of this system's per-(strategy, regime) win rates
(available from DecisionStore/RejectedOutcomeStore data once enough
history accumulates -- see Phase 6). Treat these as provisional until
that validation happens.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..logging_setup import log_event

logger = logging.getLogger("qat.drift_detection")

DEFAULT_DELTA = 0.005       # tolerance: minimum magnitude of change worth accumulating evidence for
DEFAULT_THRESHOLD = 25.0    # lambda: accumulated evidence required before firing -- higher = fewer
                            # false alarms but slower detection. NOT yet validated against this
                            # system's real variance (see module docstring).
DEFAULT_MIN_SAMPLES = 20    # don't allow ANY detection before this many observations for a
                            # (strategy, regime) cell -- avoids firing on early noise


@dataclass
class PageHinkleyState:
    """One running Page-Hinkley accumulator for a single (strategy, regime)
    cell, monitoring the LOSS indicator (1 = loss, 0 = win) for an
    INCREASE -- which corresponds to the strategy's win rate DEGRADING in
    this regime. Resets to fresh state after firing, so it's monitoring
    for the NEXT shift from the new baseline, not re-firing on stale
    accumulated evidence."""
    mean: float = 0.0
    cumulative_sum: float = 0.0
    sample_count: int = 0
    last_alert_at: Optional[float] = None
    total_alerts: int = 0

    def observe(self, loss_indicator: float, delta: float, threshold: float, min_samples: int) -> Tuple[bool, float]:
        """Feed one observation (0.0 or 1.0). Returns (fired, peak_cumulative_sum)
        -- state is reset internally when it fires, so peak_cumulative_sum is
        the value AT THE MOMENT of firing, before that reset, letting callers
        distinguish a marginal threshold crossing from a severe one."""
        self.sample_count += 1
        # Running mean, updated incrementally (Welford-style, no need to
        # store the full history).
        self.mean += (loss_indicator - self.mean) / self.sample_count
        self.cumulative_sum = max(0.0, self.cumulative_sum + (loss_indicator - self.mean - delta))
        if self.sample_count < min_samples:
            return False, 0.0
        if self.cumulative_sum > threshold:
            peak = self.cumulative_sum
            self.last_alert_at = time.time()
            self.total_alerts += 1
            # Reset so this cell starts monitoring fresh from the new
            # baseline -- otherwise it would fire on every subsequent
            # observation until enough NEW evidence pulled it back down.
            self.mean = 0.0
            self.cumulative_sum = 0.0
            self.sample_count = 0
            return True, peak
        return False, 0.0


@dataclass
class DriftAlert:
    strategy: str
    regime: str
    detected_at: float
    magnitude: float          # the cumulative_sum value at the moment of detection --
                              # larger = more evidence of a bigger/faster shift, not a
                              # normalized "how bad" score
    sample_count_since_reset: int


class DriftDetector:
    """Per-user, disk-persisted -- one PageHinkleyState per (strategy,
    regime) key. Same lightweight JSON-file pattern as ShadowStore /
    RejectedOutcomeStore (small state: a handful of floats per cell, not a
    growing log)."""

    def __init__(
        self, storage_path: str, *,
        delta: float = DEFAULT_DELTA, threshold: float = DEFAULT_THRESHOLD, min_samples: int = DEFAULT_MIN_SAMPLES,
        cooldown_seconds: float = 3600.0,
    ) -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.delta = delta
        self.threshold = threshold
        self.min_samples = min_samples
        self.cooldown_seconds = cooldown_seconds
        self._states: Dict[Tuple[str, str], PageHinkleyState] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for key_str, state_dict in raw.items():
                strategy, regime = key_str.split("||", 1)
                self._states[(strategy, regime)] = PageHinkleyState(**state_dict)
        except Exception:
            logger.warning("[drift_detection] failed to load %s", self._path, exc_info=True)

    def _save(self) -> None:
        try:
            out = {f"{k[0]}||{k[1]}": asdict(v) for k, v in self._states.items()}
            self._path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        except Exception:
            logger.warning("[drift_detection] failed to persist %s", self._path, exc_info=True)

    def observe(self, strategy_names: List[str], regime: str, won: bool, drew: bool = False) -> List[DriftAlert]:
        """Feed one resolved trade's outcome, crediting EVERY contributing
        strategy (same multi-credit convention already established in
        regime_tracker.py for exactly this kind of multi-strategy-consensus
        attribution -- a trade with 3 co-signing strategies updates all 3
        cells, not just one). Draws are excluded (same convention as every
        other win-rate computation in this codebase). Returns any alerts
        that fired this call (usually zero or one, but multiple strategies
        could independently cross their own threshold on the same trade).
        A cell that alerted within the last cooldown_seconds is suppressed
        from alerting again, even if the underlying Page-Hinkley test's own
        reset-and-reaccumulate would otherwise fire -- prevents rapid
        repeat force_trial() calls for the same (strategy, regime)."""
        if drew or not strategy_names or not regime:
            return []
        loss_indicator = 0.0 if won else 1.0
        alerts: List[DriftAlert] = []
        now = time.time()
        for name in strategy_names:
            key = (name, regime)
            state = self._states.setdefault(key, PageHinkleyState())
            in_cooldown = state.last_alert_at is not None and (now - state.last_alert_at) < self.cooldown_seconds
            fired, peak = state.observe(loss_indicator, self.delta, self.threshold, self.min_samples)
            if fired and in_cooldown:
                log_event(logger, logging.INFO, "drift_alert_suppressed_cooldown", strategy=name, regime=regime,
                          seconds_since_last_alert=round(now - state.last_alert_at, 1) if state.last_alert_at else None)
                continue
            if fired:
                alert = DriftAlert(
                    strategy=name, regime=regime, detected_at=state.last_alert_at,
                    magnitude=peak,  # the actual accumulated evidence at the moment of firing,
                    # not just the static threshold it crossed -- lets callers tell a marginal
                    # crossing from a severe one (e.g. drift_auto_mute's "2x threshold" rule)
                    sample_count_since_reset=0,
                )
                alerts.append(alert)
                log_event(logger, logging.WARNING, "drift_detected",
                          strategy=name, regime=regime, threshold=self.threshold, delta=self.delta, magnitude=peak)
        self._save()
        return alerts

    def status_for_ui(self) -> List[dict]:
        """Read-only snapshot of every tracked cell's current accumulated
        evidence -- lets a user see a cell approaching its threshold
        before it actually fires, not just after."""
        return [
            {
                "strategy": k[0], "regime": k[1], "sample_count": v.sample_count,
                "cumulative_sum": round(v.cumulative_sum, 3), "threshold": self.threshold,
                "total_alerts": v.total_alerts, "last_alert_at": v.last_alert_at,
            }
            for k, v in sorted(self._states.items())
        ]
