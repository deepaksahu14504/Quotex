"""Signal Queue Persistence - Prevent signal loss on crash/restart.

Provides durable queue storage so signals aren't lost if the system crashes.
Backed by local per-user JSON files under each user's data directory --
no external dependency (e.g. Redis) is required or used.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class PersistentSignal:
    """Signal stored in persistent queue."""
    signal_id: str
    asset: str
    direction: str  # "CALL" or "PUT"
    confidence: float
    price: float
    timeframe: str
    created_at: float
    stored_at: float = field(default_factory=time.time)
    attempts: int = 0
    last_attempt: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PersistentSignal:
        return cls(**data)


class QueuePersistence:
    """Persist signal queue to survive restarts."""
    
    def __init__(self, storage_path: Optional[str] = None):
        """Initialize queue persistence.
        
        Args:
            storage_path: Directory to store queue files
                         Defaults to ./data/queue/
        """
        self.storage_path = Path(storage_path or "./data/queue")
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Queue file for pending signals
        self.queue_file = self.storage_path / "pending_signals.json"
        self.executed_file = self.storage_path / "executed_signals.json"
        
        # In-memory cache
        self.pending: Dict[str, PersistentSignal] = {}
        self.load()
    
    def load(self) -> None:
        """Load pending signals from storage on startup."""
        if not self.queue_file.exists():
            logger.info("[v0] No persisted signals found")
            return
        
        try:
            with open(self.queue_file, 'r') as f:
                data = json.load(f)
            
            self.pending = {
                sig_id: PersistentSignal.from_dict(sig)
                for sig_id, sig in data.items()
            }
            logger.info(f"[v0] Loaded {len(self.pending)} persisted signals from queue")
            
            # Log signals being recovered
            for sig_id, sig in list(self.pending.items())[:3]:
                logger.info(
                    f"[v0]   - {sig.asset} {sig.direction} "
                    f"(confidence: {sig.confidence:.0f}, age: {time.time() - sig.created_at:.0f}s)"
                )
            
        except Exception as e:
            logger.error(f"[v0] Failed to load persisted signals: {e}")
            self.pending = {}
    
    def add(self, signal_id: str, asset: str, direction: str, 
            confidence: float, price: float, timeframe: str, 
            created_at: float) -> None:
        """Add signal to persistent queue."""
        sig = PersistentSignal(
            signal_id=signal_id,
            asset=asset,
            direction=direction,
            confidence=confidence,
            price=price,
            timeframe=timeframe,
            created_at=created_at
        )
        self.pending[signal_id] = sig
        self.save()
    
    def remove(self, signal_id: str) -> None:
        """Remove signal from queue after execution."""
        if signal_id in self.pending:
            del self.pending[signal_id]
            self.save()
    
    def get_pending(self) -> List[PersistentSignal]:
        """Get all pending signals, ordered by age."""
        return sorted(
            self.pending.values(),
            key=lambda s: s.created_at
        )
    
    def mark_attempt(self, signal_id: str) -> None:
        """Record execution attempt."""
        if signal_id in self.pending:
            self.pending[signal_id].attempts += 1
            self.pending[signal_id].last_attempt = time.time()
            self.save()
    
    def get_stale_signals(self, max_age_seconds: int = 3600) -> List[PersistentSignal]:
        """Get signals older than max_age that should be cleaned up."""
        cutoff = time.time() - max_age_seconds
        return [s for s in self.pending.values() if s.created_at < cutoff]
    
    def cleanup_stale(self, max_age_seconds: int = 3600) -> int:
        """Remove stale signals from queue."""
        stale = self.get_stale_signals(max_age_seconds)
        for sig in stale:
            logger.warning(
                f"[v0] Removing stale signal {sig.signal_id}: "
                f"age={time.time() - sig.created_at:.0f}s, attempts={sig.attempts}"
            )
            del self.pending[sig.signal_id]
        
        if stale:
            self.save()
        return len(stale)
    
    def save(self) -> None:
        """Save queue to file."""
        try:
            data = {
                sig_id: sig.to_dict()
                for sig_id, sig in self.pending.items()
            }
            with open(self.queue_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[v0] Failed to save queue: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        sigs = list(self.pending.values())
        if not sigs:
            return {
                "pending_count": 0,
                "oldest_signal_age_s": 0,
                "avg_attempts": 0,
            }
        
        ages = [time.time() - s.created_at for s in sigs]
        return {
            "pending_count": len(sigs),
            "oldest_signal_age_s": max(ages),
            "newest_signal_age_s": min(ages),
            "avg_attempts": sum(s.attempts for s in sigs) / len(sigs),
            "total_attempts": sum(s.attempts for s in sigs),
        }
    
    def clear_all(self) -> None:
        """Clear entire queue (for testing/reset)."""
        self.pending.clear()
        try:
            self.queue_file.unlink(missing_ok=True)
            logger.info("[v0] Queue cleared")
        except Exception as e:
            logger.error(f"[v0] Failed to clear queue: {e}")


# Global instance
_persistence: Optional[QueuePersistence] = None


def get_persistence(storage_path: Optional[str] = None) -> QueuePersistence:
    """Get or create global queue persistence instance."""
    global _persistence
    if _persistence is None:
        _persistence = QueuePersistence(storage_path)
    return _persistence
