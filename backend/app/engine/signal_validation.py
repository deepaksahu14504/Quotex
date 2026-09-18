"""Signal Quality & Revalidation Framework

Provides three layers of signal quality checking:
1. Signal Revalidation - Re-check signal setup before execution
2. Dynamic Candle Boundary - Verify candle structure and timing
3. Multi-Timeframe Confirmation - Confirm on higher timeframes
4. Execution Pipeline Quality - Track execution metrics

Used by orchestrator.execute_signal() to ensure only high-quality
trades are executed, reducing false positives and slippage.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from . import indicators as ta
from .strategies import htf_trend_bias, evaluate, Direction
from .regime_transition_detector import RegimeTransitionResult
from ..schemas import Signal, EvidenceItem


@dataclass
class SignalValidation:
    """Result of signal validation check."""
    is_valid: bool
    confidence_adjustment: float  # -15 to +10 (percentage points)
    warnings: List[str]
    checks_passed: Dict[str, bool]
    revalidation_reason: str  # Why we revalidated
    evidence: List[EvidenceItem] = None  # Phase 2: one structured EvidenceItem
    # per check performed (kind="supporting" if that check's adjustment was
    # >=0, "rejecting" otherwise) -- same information as warnings/checks_passed,
    # as structured data instead of free text, for SignalDecision.rejecting_evidence.

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = []


@dataclass
class ExecutionMetrics:
    """Track execution pipeline quality."""
    signal_id: str
    generated_at: float
    submitted_at: float
    executed_at: Optional[float]
    queue_wait_ms: float  # Time in queue
    total_latency_ms: float  # Time from generation to execution
    success: bool
    reason: Optional[str]
    direction: str


class SignalQualityValidator:
    """Multi-layer validation system for trading signals.

    All thresholds below were previously fixed constants scattered through
    this class's methods. They're now constructor parameters (Stage A of
    the adaptive-thresholds rollout) so they can be read from TradingSettings
    per-user instead of requiring a code change. Every default value here is
    IDENTICAL to the number it replaces -- passing no arguments reproduces
    the exact old behavior.
    """

    def __init__(
        self,
        body_ratio_weak: float = 0.15,
        body_ratio_strong: float = 0.75,
        confluence_high: int = 4,
        confluence_medium: int = 2,
        regime_adx_floor: float = 15.0,
        veto_age_ms: float = 5000.0,
        veto_adjustment: float = -20.0,
        fresh_signal_ms: float = 1000.0,
        call_setup_rsi_high: float = 85.0,
        call_setup_rsi_low: float = 25.0,
        put_setup_rsi_high: float = 75.0,
        put_setup_rsi_low: float = 15.0,
        confluence_call_rsi_low: float = 45.0,
        confluence_call_rsi_high: float = 65.0,
        confluence_put_rsi_low: float = 35.0,
        confluence_put_rsi_high: float = 55.0,
        timing_fresh_ms: float = 500.0,
        timing_decay_ms: float = 2000.0,
        adjustment_clip_min: float = -25.0,
        adjustment_clip_max: float = 15.0,
    ):
        self.metrics_log: List[ExecutionMetrics] = []
        self.last_signal_hash: Optional[str] = None
        self.last_validation: Optional[SignalValidation] = None

        # Quality-gate thresholds -- see TradingSettings for descriptions.
        self.body_ratio_weak = body_ratio_weak
        self.body_ratio_strong = body_ratio_strong
        self.confluence_high = confluence_high
        self.confluence_medium = confluence_medium
        self.regime_adx_floor = regime_adx_floor
        self.veto_age_ms = veto_age_ms
        self.veto_adjustment = veto_adjustment
        self.fresh_signal_ms = fresh_signal_ms
        self.call_setup_rsi_high = call_setup_rsi_high
        self.call_setup_rsi_low = call_setup_rsi_low
        self.put_setup_rsi_high = put_setup_rsi_high
        self.put_setup_rsi_low = put_setup_rsi_low
        self.confluence_call_rsi_low = confluence_call_rsi_low
        self.confluence_call_rsi_high = confluence_call_rsi_high
        self.confluence_put_rsi_low = confluence_put_rsi_low
        self.confluence_put_rsi_high = confluence_put_rsi_high
        self.timing_fresh_ms = timing_fresh_ms
        self.timing_decay_ms = timing_decay_ms
        self.adjustment_clip_min = adjustment_clip_min
        self.adjustment_clip_max = adjustment_clip_max
    
    def revalidate_signal(
        self,
        signal: Signal,
        current_df: pd.DataFrame,
        htf_df: Optional[pd.DataFrame] = None,
        time_since_generation_ms: float = 0.0,
        market_context=None,
        context_asset: Optional[str] = None,
        context_timeframe: Optional[str] = None,
        regime_transition_result: Optional[RegimeTransitionResult] = None,
    ) -> SignalValidation:
        """
        Revalidate signal just before execution.
        
        Checks if the setup that generated the signal still holds true.
        Returns validation result with confidence adjustment recommendation.
        
        Args:
            signal: Original signal object
            current_df: Current 1m candle data (enriched)
            htf_df: Optional higher timeframe data (enriched)
            time_since_generation_ms: How long since signal generated
            market_context: optional MarketContextEngine (Stage C, opt-in).
                When provided and enabled, _check_setup_validity() reuses
                the SAME adaptive "this asset's overbought/oversold RSI"
                bounds (metric keys "rsi_high_bound"/"rsi_low_bound") that
                strategies.evaluate()'s dead-market gate already populates
                -- one shared adaptive-RSI concept across the pipeline,
                not a second independent one. Read-only here: this method
                never calls observe(), only strategies.evaluate() (from the
                main scan path) writes to these keys, per the Foundation
                Stabilization observation-lifecycle fix (one write per
                closed candle, from one place).
            context_asset/context_timeframe: identify which engine key to
                read; both required for market_context to actually be used.
        
        Returns:
            SignalValidation with pass/fail + confidence adjustment
        """
        if current_df is None or len(current_df) < 21:
            return SignalValidation(
                is_valid=False,
                confidence_adjustment=-20,
                warnings=["Insufficient data for revalidation"],
                checks_passed={},
                revalidation_reason="data_insufficient"
            )
        
        checks: Dict[str, bool] = {}
        adjustments: List[float] = []
        warnings: List[str] = []
        evidence: List[EvidenceItem] = []
        reason = "no_revalidation"

        def _ev(source: str, adj: float, detail: str) -> None:
            evidence.append(EvidenceItem(
                source=source, direction=signal.direction.value if signal.direction else None,
                strength=abs(adj), kind="supporting" if adj >= 0 else "rejecting", detail=detail,
            ))

        # ═══════════════════════════════════════════════════════════════ #
        # CHECK 1: SETUP STILL VALID (core strategy conditions)
        # ═══════════════════════════════════════════════════════════════ #
        setup_valid = self._check_setup_validity(signal, current_df, market_context, context_asset, context_timeframe)
        checks["setup_valid"] = setup_valid
        if not setup_valid:
            warnings.append("Original setup conditions no longer valid")
            adjustments.append(-15.0)
            _ev("signal_validation.setup_valid", -15.0, "Original setup conditions no longer valid")
            reason = "setup_invalidated"
        else:
            adjustments.append(+2.0)  # Bonus: setup still holding
            _ev("signal_validation.setup_valid", +2.0, "Setup conditions still holding")
        
        # ═══════════════════════════════════════════════════════════════ #
        # CHECK 2: DYNAMIC CANDLE BOUNDARY
        # ═══════════════════════════════════════════════════════════════ #
        candle_quality = self._check_candle_boundaries(signal, current_df)
        checks["candle_quality"] = candle_quality["valid"]
        if not candle_quality["valid"]:
            reason_text = candle_quality.get("reason", "Poor candle structure")
            adj = candle_quality.get("adjustment", -5.0)
            warnings.append(reason_text)
            adjustments.append(adj)
            _ev("signal_validation.candle_quality", adj, reason_text)
            reason = "candle_quality_poor"
        else:
            adj = candle_quality.get("adjustment", +1.0)
            adjustments.append(adj)
            _ev("signal_validation.candle_quality", adj, "Candle structure acceptable")
        
        # ═══════════════════════════════════════════════════════════════ #
        # CHECK 3: MULTI-TIMEFRAME CONFIRMATION (WITH FALLBACK)
        # ═══════════════════════════════════════════════════════════════ #
        if htf_df is not None and len(htf_df) >= 3:
            try:
                htf_bias, _htf_strength = htf_trend_bias(htf_df)
                mtf_valid = self._check_multitimeframe_confirmation(signal, htf_bias)
                checks["multitimeframe_confirmed"] = mtf_valid["valid"]
                if mtf_valid["valid"]:
                    detail = f"HTF bias: {htf_bias}"
                    adj = mtf_valid.get("adjustment", +8.0)
                    warnings.append(detail)
                    adjustments.append(adj)
                    _ev("signal_validation.mtf", adj, detail)
                    reason = "multitimeframe_confirmed"
                else:
                    detail = mtf_valid.get("reason", "HTF bias conflicted")
                    adj = mtf_valid.get("adjustment", -5.0)
                    warnings.append(detail)
                    adjustments.append(adj)
                    _ev("signal_validation.mtf", adj, detail)
            except Exception as e:
                # HTF check crashed - use fallback logic
                detail = f"HTF validation error (fallback): {str(e)[:50]}"
                warnings.append(detail)
                checks["multitimeframe_confirmed"] = None
                adjustments.append(+1.0)  # Slight benefit of doubt for very fresh signals
                _ev("signal_validation.mtf", +1.0, detail)
        else:
            checks["multitimeframe_confirmed"] = None  # No HTF data
            # Fallback: if no HTF data but signal is very fresh, less strict
            if time_since_generation_ms < self.timing_fresh_ms:
                adjustments.append(+2.0)
                warnings.append("No HTF data (fresh signal accepted)")
                _ev("signal_validation.mtf", +2.0, "No HTF data (fresh signal accepted)")
            else:
                warnings.append("No HTF data for confirmation")
                _ev("signal_validation.mtf", 0.0, "No HTF data for confirmation")
        
        # ═══════════════════════════════════════════════════════════════ #
        # CHECK 4: TIMING DECAY (age of signal)
        # ═══════════════════════════════════════════════════════════════ #
        timing_penalty = self._check_timing_decay(time_since_generation_ms)
        checks["timing_decay"] = timing_penalty <= 0
        if timing_penalty < 0:
            detail = f"Signal age decay: {timing_penalty:.1f}%"
            warnings.append(detail)
            adjustments.append(timing_penalty)
            _ev("signal_validation.timing_decay", timing_penalty, detail)
            reason = "timing_decay"
        
        # ═══════════════════════════════════════════════════════════════ #
        # CHECK 5: MARKET CONDITION DETERIORATION
        # ═══════════════════════════════════════════════════════════════ #
        regime_check = self._check_regime_stability(signal, current_df)
        checks["regime_stable"] = regime_check["stable"]
        if not regime_check["stable"]:
            detail = regime_check.get("reason", "Market regime shifted")
            adj = regime_check.get("adjustment", -8.0)
            warnings.append(detail)
            adjustments.append(adj)
            _ev("signal_validation.regime_stable", adj, detail)
            reason = "regime_unstable"
        else:
            adj = regime_check.get("adjustment", 0.0)
            adjustments.append(adj)
            _ev("signal_validation.regime_stable", adj, "Market regime stable")
        
        # ═══════════════════════════════════════════════════════════════ #
        # CHECK 6: CONFLUENCE STRENGTH (multiple confirmations)
        # ═══════════════════════════════════════════════════════════════ #
        confluence_check = self._check_confluence_strength(signal, current_df)
        checks["confluence_valid"] = confluence_check["valid"]
        if confluence_check["valid"]:
            adj = confluence_check.get("adjustment", +3.0)
            detail = f"Confluence {confluence_check.get('level', 'medium')}"
            adjustments.append(adj)
            warnings.append(detail)
            _ev("signal_validation.confluence", adj, detail)
        else:
            adj = confluence_check.get("adjustment", -10.0)
            adjustments.append(adj)
            _ev("signal_validation.confluence", adj, f"Confluence weak ({confluence_check.get('level', 'low')})")
            reason = "confluence_weak"

        # ═══════════════════════════════════════════════════════════════ #
        # CHECK 7: REGIME TRANSITION DETECTOR
        # ═══════════════════════════════════════════════════════════════ #
        # Re-applies the SAME confidence-influencing (never direction-
        # forcing) signal the main scan already applied when this signal
        # was first generated -- catches the case where the regime shifted
        # in the time between generation and this revalidation check.
        # regime_transition_result is the detector's cached latest result
        # (read-only; no new computation happens here, no look-ahead).
        checks["regime_transition_stable"] = True
        if regime_transition_result is not None:
            rt = regime_transition_result
            checks["regime_transition_stable"] = not rt.paused and rt.transition_probability < 0.6
            if rt.paused:
                detail = f"Regime transition detector: paused ({rt.reason})"
                adj = -12.0
                warnings.append(detail)
                adjustments.append(adj)
                _ev("regime_transition_detector", adj, detail)
                reason = "regime_transition_paused"
            elif rt.transition_probability >= 0.6:
                detail = f"Regime transition detector: elevated transition probability ({rt.transition_probability:.2f})"
                adj = -round(rt.transition_probability * 10.0, 1)
                warnings.append(detail)
                adjustments.append(adj)
                _ev("regime_transition_detector", adj, detail)
            else:
                detail = f"Regime transition detector: stable ({rt.regime.value})"
                adj = 1.0
                adjustments.append(adj)
                _ev("regime_transition_detector", adj, detail)

        # ═══════════════════════════════════════════════════════════════ #
        # FINAL DECISION
        # ═══════════════════════════════════════════════════════════════ #
        total_adjustment = sum(adjustments)
        
        # Veto conditions (immediate rejection)
        if not setup_valid and time_since_generation_ms > self.veto_age_ms:
            veto_detail = f"Setup invalid and signal aged >{self.veto_age_ms:.0f}ms"
            evidence.append(EvidenceItem(
                source="signal_validation.veto", direction=signal.direction.value if signal.direction else None,
                strength=abs(total_adjustment), kind="rejecting", detail=veto_detail,
            ))
            return SignalValidation(
                is_valid=False,
                confidence_adjustment=total_adjustment,
                warnings=warnings + [veto_detail],
                checks_passed=checks,
                revalidation_reason="veto_aged_invalid",
                evidence=evidence,
            )
        
        # Valid if: setup still valid OR very fresh OR strong HTF confirmation
        is_valid = (
            setup_valid or
            time_since_generation_ms < self.fresh_signal_ms or
            checks.get("multitimeframe_confirmed", False)
        )
        
        # If setup degraded significantly, reject
        if setup_valid is False and total_adjustment < self.veto_adjustment:
            is_valid = False
            reason = "setup_degraded_too_much"
        
        return SignalValidation(
            is_valid=is_valid,
            confidence_adjustment=np.clip(total_adjustment, self.adjustment_clip_min, self.adjustment_clip_max),
            warnings=warnings,
            checks_passed=checks,
            revalidation_reason=reason,
            evidence=evidence,
        )
    
    def _check_setup_validity(
        self, signal: Signal, df: pd.DataFrame,
        market_context=None, context_asset: Optional[str] = None, context_timeframe: Optional[str] = None,
    ) -> bool:
        """Re-verify the core strategy conditions that generated this signal."""
        if len(df) < 3:
            return False
        
        row = df.iloc[-1]
        prev = df.iloc[-2]

        call_rsi_high, call_rsi_low = self.call_setup_rsi_high, self.call_setup_rsi_low
        put_rsi_high, put_rsi_low = self.put_setup_rsi_high, self.put_setup_rsi_low
        if market_context is not None and getattr(market_context.cfg, "enabled", False) and getattr(market_context.cfg, "signal_validation_enabled", True) and context_asset and context_timeframe:
            # Reuses the SAME adaptive "this asset's overbought/oversold
            # RSI" bounds strategies.evaluate()'s dead-market gate already
            # maintains (metric keys "rsi_high_bound"/"rsi_low_bound") --
            # read-only here, no observe() call (see revalidate_signal
            # docstring for why).
            call_rsi_high = market_context.adaptive_threshold(
                context_asset, context_timeframe, "rsi_high_bound",
                static_default=call_rsi_high, percentile_override=getattr(market_context.cfg, "rsi_high_percentile", 0.90),
            )
            put_rsi_high = market_context.adaptive_threshold(
                context_asset, context_timeframe, "rsi_high_bound",
                static_default=put_rsi_high, percentile_override=getattr(market_context.cfg, "rsi_high_percentile", 0.90),
            )
            call_rsi_low = market_context.adaptive_threshold(
                context_asset, context_timeframe, "rsi_low_bound",
                static_default=call_rsi_low, percentile_override=getattr(market_context.cfg, "rsi_low_percentile", 0.10),
            )
            put_rsi_low = market_context.adaptive_threshold(
                context_asset, context_timeframe, "rsi_low_bound",
                static_default=put_rsi_low, percentile_override=getattr(market_context.cfg, "rsi_low_percentile", 0.10),
            )
        
        # For CALL signals
        if signal.direction == Direction.CALL:
            # Must still be in uptrend or breakout
            return (
                (row["ema9"] > row["ema21"]) and
                (row["close"] > row["ema9"] or row["macd"] > row["macd_sig"]) and
                row["rsi"] < call_rsi_high and  # Not overbought into oblivion
                row["rsi"] > call_rsi_low  # Reasonable level
            )
        
        # For PUT signals
        if signal.direction == Direction.PUT:
            # Must still be in downtrend or breakdown
            return (
                (row["ema9"] < row["ema21"]) and
                (row["close"] < row["ema9"] or row["macd"] < row["macd_sig"]) and
                row["rsi"] > put_rsi_low and  # Not oversold into oblivion
                row["rsi"] < put_rsi_high  # Reasonable level
            )
        
        return False
    
    def _check_candle_boundaries(
        self, signal: Signal, df: pd.DataFrame
    ) -> Dict[str, any]:
        """
        Check dynamic candle boundaries:
        - Candle hasn't closed yet (signal before close = best)
        - Candle body size (weak bodies = less reliable)
        - Candle position in timeframe (early candle = more time)
        """
        if len(df) < 2:
            return {"valid": False, "reason": "Insufficient data", "adjustment": -10}
        
        current = df.iloc[-1]
        
        # Candle structure quality
        candle_range = current["high"] - current["low"]
        body_size = abs(current["close"] - current["open"])
        body_ratio = body_size / max(candle_range, 1e-9)
        
        # Weak body = signal unreliable
        if body_ratio < self.body_ratio_weak:
            return {
                "valid": False,
                "reason": f"Weak candle body ({body_ratio:.1%})",
                "adjustment": -8
            }
        
        # Very strong body = more reliable
        if body_ratio > self.body_ratio_strong:
            return {
                "valid": True,
                "reason": f"Strong candle body ({body_ratio:.1%})",
                "adjustment": +4
            }
        
        # Normal body = neutral
        return {"valid": True, "adjustment": 0}
    
    def _check_multitimeframe_confirmation(
        self, signal: Signal, htf_bias: Optional[Direction]
    ) -> Dict[str, any]:
        """
        Multi-timeframe confirmation:
        - Same direction on HTF = +8 confidence
        - Opposite direction on HTF = -8 confidence
        - No clear HTF bias = neutral
        """
        if htf_bias is None:
            return {"valid": True, "adjustment": 0, "reason": "No HTF bias"}
        
        if htf_bias == signal.direction:
            return {
                "valid": True,
                "adjustment": +8,
                "reason": f"HTF confirms {htf_bias}"
            }
        else:
            return {
                "valid": False,
                "adjustment": -10,
                "reason": f"HTF conflicts ({htf_bias} vs {signal.direction})"
            }
    
    def _check_timing_decay(self, ms_since_generation: float) -> float:
        """
        Decay confidence based on signal age. Breakpoints are configurable
        (timing_fresh_ms / timing_decay_ms / veto_age_ms) -- defaults match
        the original fixed 500/2000/5000ms bands:
        - <500ms: 0% decay (fresh)
        - 500-2000ms: linear -1% -> -4%
        - 2000-5000ms: linear -5% -> -10%
        - >=5000ms: -15% flat (veto range)

        Audit fix (H-3): the previous formula didn't actually reach these
        documented endpoints -- -0.5*(ms/1000) tops out at -1.0 at 2000ms,
        not -4.0 -- and jumped discontinuously by several points at each
        band boundary (e.g. -1.0 -> -5.0 right at 2000ms) instead of the
        graduated decay the bands describe. This version linearly
        interpolates within each band so it starts and ends exactly on the
        documented value for that band. The small step from band 2's end
        (-4) to band 3's start (-5), and from band 3's end (-10) to band 4
        (-15), is intentional and matches the original spec -- each band
        boundary is a deliberately slightly harsher regime, not a bug.
        """
        if ms_since_generation < self.timing_fresh_ms:
            return 0.0
        if ms_since_generation < self.timing_decay_ms:
            span = max(self.timing_decay_ms - self.timing_fresh_ms, 1e-9)
            frac = (ms_since_generation - self.timing_fresh_ms) / span
            return -1.0 + frac * (-4.0 - (-1.0))
        if ms_since_generation < self.veto_age_ms:
            span = max(self.veto_age_ms - self.timing_decay_ms, 1e-9)
            frac = (ms_since_generation - self.timing_decay_ms) / span
            return -5.0 + frac * (-10.0 - (-5.0))
        return -15.0
    
    def _check_regime_stability(self, signal: Signal, df: pd.DataFrame) -> Dict[str, any]:
        """Check if market regime has shifted since signal generation."""
        if len(df) < 50:
            return {"stable": True, "adjustment": 0}
        
        current = df.iloc[-1]
        prev_50 = df.iloc[-50]
        
        # ADX trend strength
        if current["adx"] < self.regime_adx_floor and current["adx"] < prev_50["adx"] * 0.7:
            return {
                "stable": False,
                "adjustment": -5,
                "reason": "Trend weakening (ADX falling)"
            }
        
        # Check for contra-move
        if signal.direction == Direction.CALL:
            if current["close"] < current["ema50"]:
                return {
                    "stable": False,
                    "adjustment": -8,
                    "reason": "Price fell below EMA50 (regime shift)"
                }
        
        if signal.direction == Direction.PUT:
            if current["close"] > current["ema50"]:
                return {
                    "stable": False,
                    "adjustment": -8,
                    "reason": "Price rose above EMA50 (regime shift)"
                }
        
        return {"stable": True, "adjustment": 0}
    
    def _check_confluence_strength(self, signal: Signal, df: pd.DataFrame) -> Dict[str, any]:
        """Check if multiple confluences still support the signal."""
        if len(df) < 3:
            return {"valid": False, "adjustment": -15}
        
        current = df.iloc[-1]
        confluence_count = 0
        
        # Count supporting factors
        if signal.direction == Direction.CALL:
            if current["close"] > current["ema21"]:
                confluence_count += 1
            if current["ema21"] > current["ema50"]:
                confluence_count += 1
            if self.confluence_call_rsi_low < current["rsi"] < self.confluence_call_rsi_high:
                confluence_count += 1
            if current["macd"] > current["macd_sig"]:
                confluence_count += 1
            if current["st_dir"] == 1:
                confluence_count += 1
        
        elif signal.direction == Direction.PUT:
            if current["close"] < current["ema21"]:
                confluence_count += 1
            if current["ema21"] < current["ema50"]:
                confluence_count += 1
            if self.confluence_put_rsi_low < current["rsi"] < self.confluence_put_rsi_high:
                confluence_count += 1
            if current["macd"] < current["macd_sig"]:
                confluence_count += 1
            if current["st_dir"] == -1:
                confluence_count += 1
        
        if confluence_count >= self.confluence_high:
            return {
                "valid": True,
                "adjustment": +5,
                "level": "high",
                "count": confluence_count
            }
        elif confluence_count >= self.confluence_medium:
            return {
                "valid": True,
                "adjustment": +2,
                "level": "medium",
                "count": confluence_count
            }
        else:
            return {
                "valid": False,
                "adjustment": -8,
                "level": "low",
                "count": confluence_count
            }
    
    def log_execution_metric(
        self,
        signal_id: str,
        generated_at: float,
        submitted_at: float,
        executed_at: Optional[float],
        success: bool,
        reason: Optional[str],
        direction: str
    ) -> None:
        """Log execution metrics for pipeline quality tracking."""
        queue_wait = (executed_at - submitted_at) * 1000 if executed_at else None
        total_latency = (executed_at - generated_at) * 1000 if executed_at else None
        
        metric = ExecutionMetrics(
            signal_id=signal_id,
            generated_at=generated_at,
            submitted_at=submitted_at,
            executed_at=executed_at,
            queue_wait_ms=queue_wait or 0,
            total_latency_ms=total_latency or 0,
            success=success,
            reason=reason,
            direction=direction
        )
        self.metrics_log.append(metric)
    
    def get_execution_quality_report(self) -> Dict[str, any]:
        """Get overall execution pipeline quality metrics."""
        if not self.metrics_log:
            return {"total_trades": 0}
        
        recent = self.metrics_log[-100:]  # Last 100 trades
        successful = [m for m in recent if m.success]
        failed = [m for m in recent if not m.success]
        
        calls = [m for m in recent if m.direction == "CALL"]
        puts = [m for m in recent if m.direction == "PUT"]
        
        return {
            "total_trades": len(recent),
            "success_rate": len(successful) / len(recent) if recent else 0,
            "failed_trades": len(failed),
            "avg_queue_wait_ms": np.mean([m.queue_wait_ms for m in successful if m.queue_wait_ms > 0]) if successful else 0,
            "avg_total_latency_ms": np.mean([m.total_latency_ms for m in successful if m.total_latency_ms > 0]) if successful else 0,
            "call_success_rate": len([m for m in calls if m.success]) / len(calls) if calls else 0,
            "put_success_rate": len([m for m in puts if m.success]) / len(puts) if puts else 0,
            "failure_reasons": self._analyze_failures(failed)
        }
    
    def _analyze_failures(self, failed: List[ExecutionMetrics]) -> Dict[str, int]:
        """Analyze failure reasons."""
        reasons = {}
        for m in failed:
            key = m.reason or "unknown"
            reasons[key] = reasons.get(key, 0) + 1
        return reasons
