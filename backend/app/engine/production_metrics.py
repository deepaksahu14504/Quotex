"""Production-grade metrics and monitoring for trading system.

Tracks:
- Signal quality metrics
- Execution success rates
- Performance per strategy
- Real-time health indicators
- Alert thresholds
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class SignalMetric:
    """Individual signal metric."""
    signal_id: str
    asset: str
    direction: str
    confidence: float
    generated_at: float
    executed_at: Optional[float]
    queue_wait_ms: float
    execution_success: bool
    execution_reason: Optional[str]
    strategies_voting: List[str]
    result_pnl: Optional[float] = None


@dataclass
class StrategyMetrics:
    """Per-strategy performance tracking."""
    name: str
    signals_generated: int = 0
    signals_executed: int = 0
    signals_won: int = 0
    signals_lost: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    accuracy: float = 0.0
    last_updated: float = field(default_factory=time.time)
    
    def update_pnl(self, pnl: float, won: bool) -> None:
        if won:
            self.signals_won += 1
        else:
            self.signals_lost += 1
        self.total_pnl += pnl
        self.recalculate()
    
    def recalculate(self) -> None:
        total = self.signals_won + self.signals_lost
        if total > 0:
            self.win_rate = self.signals_won / total
            self.accuracy = self.win_rate * 100


class ProductionMetrics:
    """Production monitoring and metrics collection."""
    
    def __init__(self, max_history: int = 10000):
        """Initialize metrics tracking.
        
        Args:
            max_history: Maximum number of signal metrics to keep
        """
        self.max_history = max_history
        self.signal_history: deque = deque(maxlen=max_history)
        self.strategy_metrics: Dict[str, StrategyMetrics] = {}
        
        # Real-time counters
        self.signals_generated = 0
        self.signals_executed = 0
        self.signals_failed = 0
        self.trades_won = 0
        self.trades_lost = 0
        
        # Timestamps
        self.start_time = time.time()
        self.last_reset = self.start_time
    
    def record_signal(
        self,
        signal_id: str,
        asset: str,
        direction: str,
        confidence: float,
        generated_at: float,
        strategies: List[str],
    ) -> None:
        """Record signal generation."""
        self.signals_generated += 1
        
        metric = SignalMetric(
            signal_id=signal_id,
            asset=asset,
            direction=direction,
            confidence=confidence,
            generated_at=generated_at,
            executed_at=None,
            queue_wait_ms=0,
            execution_success=False,
            execution_reason=None,
            strategies_voting=strategies
        )
        self.signal_history.append(metric)
        
        # Initialize strategy metrics
        for strat in strategies:
            if strat not in self.strategy_metrics:
                self.strategy_metrics[strat] = StrategyMetrics(strat)
            self.strategy_metrics[strat].signals_generated += 1
    
    def record_execution(
        self,
        signal_id: str,
        executed_at: float,
        success: bool,
        queue_wait_ms: float = 0,
        reason: Optional[str] = None,
    ) -> None:
        """Record signal execution attempt."""
        # Find signal in history
        for metric in reversed(self.signal_history):
            if metric.signal_id == signal_id:
                metric.executed_at = executed_at
                metric.execution_success = success
                metric.queue_wait_ms = queue_wait_ms
                metric.execution_reason = reason
                
                if success:
                    self.signals_executed += 1
                else:
                    self.signals_failed += 1
                break
    
    def record_trade_result(
        self,
        signal_id: str,
        pnl: float,
        won: bool,
    ) -> None:
        """Record trade outcome."""
        if won:
            self.trades_won += 1
        else:
            self.trades_lost += 1
        
        # Find signal and update strategy metrics
        for metric in reversed(self.signal_history):
            if metric.signal_id == signal_id:
                metric.result_pnl = pnl
                for strat in metric.strategies_voting:
                    if strat in self.strategy_metrics:
                        self.strategy_metrics[strat].update_pnl(pnl, won)
                break
    
    def get_overall_report(self) -> Dict[str, Any]:
        """Get overall system health report."""
        uptime_seconds = time.time() - self.start_time
        uptime_hours = uptime_seconds / 3600
        
        # Calculate rates
        execution_rate = (
            (self.signals_executed / self.signals_generated * 100)
            if self.signals_generated > 0 else 0
        )
        
        total_trades = self.trades_won + self.trades_lost
        win_rate = (
            (self.trades_won / total_trades * 100)
            if total_trades > 0 else 0
        )
        
        # Recent 100 signals
        recent = list(self.signal_history)[-100:]
        recent_success = sum(
            1 for m in recent
            if m.execution_success
        )
        recent_success_rate = (
            (recent_success / len(recent) * 100)
            if recent else 0
        )
        
        recent_queues = [
            m.queue_wait_ms for m in recent
            if m.queue_wait_ms > 0
        ]
        avg_queue_ms = (
            sum(recent_queues) / len(recent_queues)
            if recent_queues else 0
        )
        
        return {
            "uptime_hours": round(uptime_hours, 1),
            "signals_generated": self.signals_generated,
            "signals_executed": self.signals_executed,
            "signals_failed": self.signals_failed,
            "execution_rate_pct": round(execution_rate, 1),
            "trades_won": self.trades_won,
            "trades_lost": self.trades_lost,
            "win_rate_pct": round(win_rate, 1),
            "recent_100_success_rate_pct": round(recent_success_rate, 1),
            "avg_queue_wait_ms": round(avg_queue_ms, 0),
            "timestamp": datetime.now().isoformat(),
        }
    
    def get_strategy_report(self) -> Dict[str, Dict[str, Any]]:
        """Get per-strategy performance report."""
        return {
            strat: {
                "signals_generated": metrics.signals_generated,
                "signals_executed": metrics.signals_executed,
                "signals_won": metrics.signals_won,
                "signals_lost": metrics.signals_lost,
                "win_rate_pct": round(metrics.win_rate * 100, 1),
                "total_pnl": round(metrics.total_pnl, 2),
            }
            for strat, metrics in self.strategy_metrics.items()
        }
    
    def get_health_indicators(self) -> Dict[str, bool]:
        """Get system health indicators."""
        report = self.get_overall_report()
        
        return {
            "execution_healthy": report["execution_rate_pct"] > 90,
            "queue_healthy": report["avg_queue_wait_ms"] < 500,
            "win_rate_healthy": report["win_rate_pct"] > 50,
            "recent_healthy": report["recent_100_success_rate_pct"] > 92,
            "overall_healthy": (
                report["execution_rate_pct"] > 90 and
                report["avg_queue_wait_ms"] < 500 and
                report["recent_100_success_rate_pct"] > 92
            ),
        }
    
    def get_alerts(self, threshold: Optional[Dict[str, float]] = None) -> List[Dict[str, str]]:
        """Generate alerts based on threshold violations."""
        if threshold is None:
            threshold = {
                "execution_rate": 90.0,
                "queue_wait_ms": 500.0,
                "win_rate": 50.0,
                "recent_success_rate": 92.0,
            }
        
        alerts = []
        report = self.get_overall_report()
        
        if report["execution_rate_pct"] < threshold["execution_rate"]:
            alerts.append({
                "severity": "warning",
                "metric": "execution_rate",
                "message": f"Execution rate {report['execution_rate_pct']:.1f}% below threshold {threshold['execution_rate']}%",
            })
        
        if report["avg_queue_wait_ms"] > threshold["queue_wait_ms"]:
            alerts.append({
                "severity": "warning",
                "metric": "queue_wait",
                "message": f"Queue wait {report['avg_queue_wait_ms']:.0f}ms above threshold {threshold['queue_wait_ms']:.0f}ms",
            })
        
        if report["win_rate_pct"] < threshold["win_rate"]:
            alerts.append({
                "severity": "warning",
                "metric": "win_rate",
                "message": f"Win rate {report['win_rate_pct']:.1f}% below threshold {threshold['win_rate']}%",
            })
        
        if report["recent_100_success_rate_pct"] < threshold["recent_success_rate"]:
            alerts.append({
                "severity": "critical",
                "metric": "recent_success",
                "message": f"Recent success rate {report['recent_100_success_rate_pct']:.1f}% below threshold {threshold['recent_success_rate']}%",
            })
        
        return alerts
    
    def print_report(self) -> None:
        """Print formatted report to logs."""
        report = self.get_overall_report()
        health = self.get_health_indicators()
        alerts = self.get_alerts()
        
        status = "HEALTHY" if health["overall_healthy"] else "DEGRADED"
        
        logger.info(f"""
╔════════════════════════════════════════════════════════╗
║              PRODUCTION SYSTEM REPORT                  ║
║              Status: {status:<35} ║
╚════════════════════════════════════════════════════════╝

Uptime: {report['uptime_hours']:.1f}h
Signals: {report['signals_generated']} generated, {report['signals_executed']} executed ({report['execution_rate_pct']:.1f}%)
Trades: {report['trades_won']}W / {report['trades_lost']}L (Win Rate: {report['win_rate_pct']:.1f}%)
Recent (100): Success {report['recent_100_success_rate_pct']:.1f}% | Queue {report['avg_queue_wait_ms']:.0f}ms

Health:
  - Execution: {'✓' if health['execution_healthy'] else '✗'}
  - Queue: {'✓' if health['queue_healthy'] else '✗'}
  - Win Rate: {'✓' if health['win_rate_healthy'] else '✗'}
  - Recent: {'✓' if health['recent_healthy'] else '✗'}

Alerts: {len(alerts)}
{chr(10).join(f"  ⚠ {a['message']}" for a in alerts) if alerts else '  None'}
        """)


# Global instance
_metrics: Optional[ProductionMetrics] = None


def get_metrics(max_history: int = 10000) -> ProductionMetrics:
    """Get or create global metrics instance."""
    global _metrics
    if _metrics is None:
        _metrics = ProductionMetrics(max_history)
    return _metrics
