"""End-to-end latency profiling.

Tracks five checkpoints per trading cycle:
    provider_receive  -> candles/ticks actually landed from the provider
    eval_start        -> strategies.evaluate() about to run
    signal_generated  -> a direction was produced (Signal object built)
    trade_request     -> about to call provider.place_order()
    broker_ack        -> provider.place_order() returned (broker acked it)

Most scan cycles never reach a signal, and most signals (manual mode, or
auto mode blocked by risk/correlation gates) never reach a trade — so a
record is created cheaply per asset per cycle, "rekeyed" onto the winning
signal's id if one fires, and dropped otherwise. Nothing here blocks the
scan loop; it's just perf_counter() stamps and dict bookkeeping.

Printed/logged as soon as a record closes (whichever stage it reaches), and
folded into a rolling per-hop metrics store (avg/min/max/p95) that the
orchestrator exposes via `GET /api/latency` and a periodic "latency"
WebSocket broadcast, so real end-to-end delay can be *measured*, not just
assumed from the architecture.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

logger = logging.getLogger("latency")

STAGES = ("provider_receive", "eval_start", "signal_generated", "trade_request", "broker_ack")
# Consecutive hops + the full end-to-end span, each tracked separately.
_HOPS: List[tuple] = list(zip(STAGES[:-1], STAGES[1:])) + [("provider_receive", "broker_ack")]


@dataclass
class _Record:
    asset: str
    stamps: Dict[str, float] = field(default_factory=dict)

    def mark(self, stage: str) -> None:
        # First mark wins — re-marking the same stage (e.g. a retried call)
        # shouldn't stretch the measured window.
        self.stamps.setdefault(stage, time.perf_counter())


class LatencyTracker:
    """One instance per Orchestrator (per user) — never shared across users,
    so one user's trade volume never skews another's latency stats."""

    def __init__(self, history: int = 200) -> None:
        self._records: Dict[str, _Record] = {}
        self._rolling: Dict[str, Deque[float]] = {f"{a}->{b}": deque(maxlen=history) for a, b in _HOPS}

    # -- lifecycle ----------------------------------------------------- #
    def start(self, key: str, asset: str) -> None:
        """Begin a new record, immediately stamping provider_receive — call
        this the moment candles/ticks for `asset` are actually in hand."""
        rec = _Record(asset=asset)
        rec.mark("provider_receive")
        self._records[key] = rec

    def mark(self, key: str, stage: str) -> None:
        rec = self._records.get(key)
        if rec is not None:
            rec.mark(stage)

    def rekey(self, old_key: str, new_key: str) -> None:
        """Move a record from its temporary per-asset key onto the signal id
        it produced, so the trade-execution stages (which happen later,
        possibly through the trade-engine queue) can still find it."""
        rec = self._records.pop(old_key, None)
        if rec is not None:
            self._records[new_key] = rec

    def drop(self, key: str) -> None:
        """Discard a record that will never complete (asset didn't win the
        cycle, signal skipped/expired without executing, etc.) — otherwise
        these would leak forever since nothing else clears them."""
        self._records.pop(key, None)

    def finish(self, key: str) -> Optional[Dict[str, float]]:
        """Close a record: compute per-hop deltas in ms, log/print them,
        feed the rolling metrics store, then drop it."""
        rec = self._records.pop(key, None)
        if rec is None or "provider_receive" not in rec.stamps:
            return None
        deltas: Dict[str, float] = {}
        for a, b in _HOPS:
            if a in rec.stamps and b in rec.stamps:
                ms = round((rec.stamps[b] - rec.stamps[a]) * 1000.0, 2)
                deltas[f"{a}->{b}"] = ms
                self._rolling[f"{a}->{b}"].append(ms)
        if deltas:
            line = " ".join(f"{hop}={ms}ms" for hop, ms in deltas.items())
            logger.info("[LATENCY] asset=%s %s", rec.asset, line)
            print(f"[LATENCY] asset={rec.asset} {line}", flush=True)
        return deltas

    # -- metrics --------------------------------------------------------- #
    def snapshot(self) -> Dict[str, dict]:
        """Rolling avg/min/max/p95 per hop (ms), computed over the last
        `history` completed cycles — this is what answers 'how much delay
        does the system actually have end to end', not just per-hop but
        cumulatively (see the 'provider_receive->broker_ack' entry)."""
        out: Dict[str, dict] = {}
        for hop, values in self._rolling.items():
            if not values:
                continue
            vs = sorted(values)
            p95_idx = max(0, int(len(vs) * 0.95) - 1)
            out[hop] = {
                "avg_ms": round(statistics.fmean(vs), 2),
                "min_ms": round(vs[0], 2),
                "max_ms": round(vs[-1], 2),
                "p95_ms": round(vs[p95_idx], 2),
                "samples": len(vs),
            }
        return out
