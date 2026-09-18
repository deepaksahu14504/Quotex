"""Pipeline Visualization: read-only observability layer for the
Trading Pipeline Visualization System.

This module is strictly additive and strictly observational:
  - Nothing here is ever consulted by a trading decision. No function in
    this file returns anything a caller branches on for a trade/signal
    outcome -- every call site treats these as pure side effects.
  - Nothing here recomputes an indicator, strategy vote, or score.
    Every value stored/emitted is captured from an already-computed
    local variable at the point it already exists in the pipeline
    (orchestrator._evaluate_asset_signal, strategies.evaluate, etc.),
    then discarded there as before.
  - Every public method is wrapped internally so an exception here can
    never propagate into the caller's trading path -- worst case, a
    visualization update is silently dropped and logged, exactly like
    the existing log_event/hub.broadcast call sites elsewhere in the
    codebase already do.

Two structures, matching the two update frequencies in the pipeline:

  AssetPipelineSnapshot -- one per (asset, timeframe), continuously
  OVERWRITTEN (not accumulated). Connection/candle/indicator/market-
  context state that exists independently of any specific signal
  attempt firing.

  (Signal-attempt-level detail reuses the existing PipelineTracer /
  PipelineStage machinery in pipeline_tracer.py rather than a parallel
  system -- see the new stages added there.)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

logger = logging.getLogger("qat.pipeline_events")


@dataclass
class AssetPipelineSnapshot:
    """Continuously-overwritten live state for one (asset, timeframe).
    Every field here is either read directly from an existing cache
    (IndicatorCache._tail, MarketContextEngine) or passed in from a
    value the caller already computed this scan cycle -- nothing is
    recomputed to populate this."""
    asset: str
    timeframe: str
    updated_at: float = field(default_factory=time.time)

    ws_connected: Optional[bool] = None
    last_candle_ts: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None

    indicators: Dict[str, float] = field(default_factory=dict)   # ema/rsi/macd/atr/adx/supertrend/vwap/bb_*/psar/stoch_rsi/volume/support/resistance
    trend_strength: Optional[float] = None
    volatility: Optional[float] = None

    regime: Optional[str] = None                # trend | range | mixed
    adaptive_thresholds: Dict[str, float] = field(default_factory=dict)  # metric -> value currently in effect

    stage: str = "waiting"        # waiting | running | passed | rejected | failed
    stage_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PipelineEventBus:
    """One instance per Orchestrator (per-user, matching pipeline_tracer's
    existing per-instance pattern -- no shared/global state across users)."""

    def __init__(self, on_snapshot_change=None) -> None:
        self._snapshots: Dict[str, AssetPipelineSnapshot] = {}  # key: f"{asset}|{tf}"
        self._on_snapshot_change = on_snapshot_change  # optional async callback(asset, tf, snapshot_dict)

    def _key(self, asset: str, timeframe: str) -> str:
        return f"{asset}|{timeframe}"

    def update_snapshot(self, asset: str, timeframe: str, **fields) -> Optional[AssetPipelineSnapshot]:
        """Merge-update the live snapshot for one asset/timeframe. Returns
        the updated snapshot (for the caller to optionally broadcast) or
        None on internal failure -- never raises."""
        try:
            key = self._key(asset, timeframe)
            snap = self._snapshots.get(key)
            if snap is None:
                snap = AssetPipelineSnapshot(asset=asset, timeframe=timeframe)
                self._snapshots[key] = snap
            for k, v in fields.items():
                if hasattr(snap, k) and v is not None:
                    setattr(snap, k, v)
            snap.updated_at = time.time()
            return snap
        except Exception:
            logger.debug("[pipeline_events] update_snapshot failed", exc_info=True)
            return None

    def get_snapshot(self, asset: str, timeframe: str) -> Optional[AssetPipelineSnapshot]:
        return self._snapshots.get(self._key(asset, timeframe))

    def all_snapshots(self) -> Dict[str, Dict[str, Any]]:
        try:
            return {k: v.to_dict() for k, v in self._snapshots.items()}
        except Exception:
            return {}
