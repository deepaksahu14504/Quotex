"""
Pipeline Visibility & Tracing System
Provides complete visibility into signal flow from generation to execution
Logs every step with timing, metrics, and bug detection
"""

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum
import logging

logger = logging.getLogger("pipeline_tracer")


class PipelineStage(str, Enum):
    """Stages in signal pipeline"""
    DATA_FETCH = "data_fetch"
    INDICATOR_CALC = "indicator_calc"
    STRATEGY_EVAL = "strategy_eval"
    MARKET_CONTEXT = "market_context"        # visualization addition -- regime/adaptive-threshold snapshot
    CONFIDENCE_SCORE = "confidence_score"
    PRECISION_GATE = "precision_gate"        # visualization addition -- only meaningful when precision_mode_enabled
    SIGNAL_REVALIDATE = "signal_revalidate"
    PATTERN_RISK = "pattern_risk"            # visualization addition -- shadow-only, mirrors orchestrator's existing shadow assessment
    RECOVERY_CHECK = "recovery_check"        # visualization addition -- shadow-only, mirrors RecoveryEngine's existing advisory role
    QUEUE_ADD = "queue_add"
    QUEUE_WAIT = "queue_wait"
    RISK_CHECK = "risk_check"
    ASSET_CHECK = "asset_check"
    EXECUTION = "execution"
    COMPLETION = "completion"


@dataclass
class StageMetric:
    """Metrics for a single pipeline stage"""
    stage: PipelineStage
    timestamp: float
    duration_ms: float = 0.0
    success: bool = True
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    warning: Optional[str] = None


@dataclass
class PipelineTrace:
    """Complete trace of a signal through the pipeline"""
    signal_id: str
    asset: str
    timeframe: str
    direction: str
    created_at: float
    stages: List[StageMetric] = field(default_factory=list)
    
    def total_duration_ms(self) -> float:
        """Total pipeline duration"""
        if not self.stages:
            return 0.0
        return self.stages[-1].timestamp - self.created_at
    
    def stage_durations(self) -> Dict[str, float]:
        """Duration per stage"""
        return {s.stage.value: s.duration_ms for s in self.stages}
    
    def successful(self) -> bool:
        """Was entire pipeline successful"""
        return all(s.success for s in self.stages)
    
    def errors(self) -> List[str]:
        """All errors in pipeline"""
        return [s.error for s in self.stages if s.error]
    
    def warnings(self) -> List[str]:
        """All warnings in pipeline"""
        return [s.warning for s in self.stages if s.warning]


class PipelineTracer:
    """Traces signal flow and detects issues"""
    
    def __init__(self, max_traces: int = 1000, on_stage=None):
        self.traces: Dict[str, PipelineTrace] = {}
        self.max_traces = max_traces
        self.active_traces: Dict[str, Dict[str, Any]] = {}  # In-flight traces
        # Visualization addition: optional sync callback(signal_id, stage, metric_dict)
        # invoked after every mark_stage/start_trace/end_trace. Deliberately a plain
        # callback, not an awaited coroutine -- the caller (Orchestrator) is
        # responsible for scheduling the actual WS broadcast (which IS async)
        # via asyncio.create_task, so a slow/failing visualization push can
        # never block or fail the tracing call itself. Wrapped in try/except
        # here as a second layer of protection.
        self._on_stage = on_stage

    def _notify(self, signal_id: str, event: str, payload: Dict[str, Any]) -> None:
        if self._on_stage is None:
            return
        try:
            self._on_stage(signal_id, event, payload)
        except Exception:
            logger.debug("[TRACE] on_stage callback failed", exc_info=True)
    
    def start_trace(self, signal_id: str, asset: str, timeframe: str, direction: str) -> None:
        """Start tracing a signal"""
        trace = PipelineTrace(
            signal_id=signal_id,
            asset=asset,
            timeframe=timeframe,
            direction=direction,
            created_at=time.time()
        )
        self.traces[signal_id] = trace
        self.active_traces[signal_id] = {"start_time": time.time()}
        logger.info(f"[TRACE] Started: {signal_id} ({asset} {direction})")
        self._notify(signal_id, "trace_started", {
            "asset": asset, "timeframe": timeframe, "direction": direction,
            "created_at": trace.created_at,
        })
    
    def mark_stage(self, signal_id: str, stage: PipelineStage, 
                   data: Dict[str, Any] = None, error: str = None, 
                   warning: str = None) -> None:
        """Mark completion of a stage"""
        if signal_id not in self.traces:
            logger.warning(f"[TRACE] Unknown signal_id: {signal_id}")
            return
        
        trace = self.traces[signal_id]
        active = self.active_traces.get(signal_id, {})
        now = time.time()
        start_time = active.get(f"stage_{stage.value}", now)
        duration_ms = (now - start_time) * 1000
        
        metric = StageMetric(
            stage=stage,
            timestamp=now,
            duration_ms=duration_ms,
            success=error is None,
            data=data or {},
            error=error,
            warning=warning
        )
        trace.stages.append(metric)
        
        log_msg = f"[TRACE] {stage.value}: {signal_id} ({duration_ms:.1f}ms)"
        if error:
            logger.error(f"{log_msg} - ERROR: {error}")
        elif warning:
            logger.warning(f"{log_msg} - WARNING: {warning}")
        else:
            logger.info(log_msg)

        self._notify(signal_id, "stage_update", {
            "stage": stage.value, "timestamp": now, "duration_ms": duration_ms,
            "success": metric.success, "data": metric.data, "error": error, "warning": warning,
        })
    
    def end_trace(self, signal_id: str) -> Optional[PipelineTrace]:
        """Mark trace as complete"""
        trace = self.traces.get(signal_id)
        if not trace:
            return None
        
        # Log summary
        total_ms = trace.total_duration_ms()
        status = "✅ SUCCESS" if trace.successful() else "❌ FAILED"
        errors = trace.errors()
        warnings = trace.warnings()
        
        logger.info(
            f"[TRACE] COMPLETE: {signal_id} | {status} | "
            f"Total: {total_ms:.1f}ms | "
            f"Stages: {len(trace.stages)} | "
            f"Errors: {len(errors)} | Warnings: {len(warnings)}"
        )
        
        # Cleanup old traces
        if len(self.traces) > self.max_traces:
            oldest_id = min(self.traces.keys(), 
                          key=lambda k: self.traces[k].created_at)
            del self.traces[oldest_id]
        
        if signal_id in self.active_traces:
            del self.active_traces[signal_id]
        
        self._notify(signal_id, "trace_completed", {
            "successful": trace.successful(), "total_duration_ms": total_ms,
            "stage_count": len(trace.stages),
        })
        return trace
    
    def get_trace(self, signal_id: str) -> Optional[PipelineTrace]:
        """Get completed trace"""
        return self.traces.get(signal_id)
    
    def detect_bugs(self, trace: PipelineTrace) -> List[Dict[str, Any]]:
        """Detect common bugs and issues in pipeline"""
        bugs = []
        
        # Bug 1: Excessive stage latency
        for stage in trace.stages:
            if stage.duration_ms > 100:  # >100ms per stage is concerning
                bugs.append({
                    "type": "SLOW_STAGE",
                    "stage": stage.stage.value,
                    "duration_ms": stage.duration_ms,
                    "severity": "HIGH" if stage.duration_ms > 500 else "MEDIUM"
                })
        
        # Bug 2: Missing critical stages
        expected_stages = [
            PipelineStage.STRATEGY_EVAL,
            PipelineStage.CONFIDENCE_SCORE,
            PipelineStage.EXECUTION
        ]
        actual_stages = [s.stage for s in trace.stages]
        for expected in expected_stages:
            if expected not in actual_stages and trace.successful():
                bugs.append({
                    "type": "MISSING_STAGE",
                    "stage": expected.value,
                    "severity": "HIGH"
                })
        
        # Bug 3: Failed revalidation
        revalidate_stage = next((s for s in trace.stages 
                               if s.stage == PipelineStage.SIGNAL_REVALIDATE), None)
        if revalidate_stage and not revalidate_stage.success:
            bugs.append({
                "type": "REVALIDATION_FAILED",
                "error": revalidate_stage.error,
                "severity": "MEDIUM"
            })
        
        # Bug 4: Queue wait too long
        queue_wait_stage = next((s for s in trace.stages 
                               if s.stage == PipelineStage.QUEUE_WAIT), None)
        if queue_wait_stage and queue_wait_stage.duration_ms > 2000:
            bugs.append({
                "type": "QUEUE_CONGESTION",
                "wait_ms": queue_wait_stage.duration_ms,
                "severity": "HIGH"
            })
        
        # Bug 5: Risk rejection
        risk_stage = next((s for s in trace.stages 
                         if s.stage == PipelineStage.RISK_CHECK), None)
        if risk_stage and not risk_stage.success:
            bugs.append({
                "type": "RISK_REJECTED",
                "error": risk_stage.error,
                "severity": "MEDIUM"
            })
        
        # Bug 6: Asset blocking
        asset_stage = next((s for s in trace.stages 
                          if s.stage == PipelineStage.ASSET_CHECK), None)
        if asset_stage and not asset_stage.success:
            bugs.append({
                "type": "ASSET_BLOCKED",
                "error": asset_stage.error,
                "severity": "LOW"
            })
        
        return bugs
    
    def get_stats(self) -> Dict[str, Any]:
        """Get pipeline statistics"""
        completed = list(self.traces.values())
        if not completed:
            return {}
        
        successful = [t for t in completed if t.successful()]
        failed = [t for t in completed if not t.successful()]
        
        durations = [t.total_duration_ms() for t in completed]
        
        return {
            "total_signals": len(completed),
            "successful": len(successful),
            "failed": len(failed),
            "success_rate": len(successful) / len(completed) if completed else 0,
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
            "min_duration_ms": min(durations) if durations else 0,
            "max_duration_ms": max(durations) if durations else 0,
            "active_traces": len(self.active_traces),
            "recent_errors": list(set(
                error for t in failed for error in t.errors()
            ))[:5]
        }
    
    def export_json(self, signal_id: str) -> Optional[Dict]:
        """Export trace as JSON for visualization"""
        trace = self.traces.get(signal_id)
        if not trace:
            return None
        
        return {
            "signal_id": trace.signal_id,
            "asset": trace.asset,
            "timeframe": trace.timeframe,
            "direction": trace.direction,
            "created_at": datetime.fromtimestamp(trace.created_at).isoformat(),
            "total_duration_ms": trace.total_duration_ms(),
            "successful": trace.successful(),
            "stages": [
                {
                    "stage": s.stage.value,
                    "timestamp": s.timestamp,
                    "duration_ms": s.duration_ms,
                    "success": s.success,
                    "data": s.data,
                    "error": s.error,
                    "warning": s.warning
                }
                for s in trace.stages
            ],
            "bugs": self.detect_bugs(trace),
            "warnings": trace.warnings(),
            "errors": trace.errors()
        }


# Global tracer instance
_tracer = PipelineTracer()


def get_tracer() -> PipelineTracer:
    """Get global tracer instance"""
    return _tracer


def trace_stage(signal_id: str, stage: PipelineStage, 
                data: Dict = None, error: str = None, warning: str = None) -> None:
    """Convenience function to mark a stage"""
    _tracer.mark_stage(signal_id, stage, data, error, warning)
