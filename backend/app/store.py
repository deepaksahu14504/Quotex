"""Simple JSON-backed persistence for trade history (single-user, file-based).

Swappable for SQLite/Postgres later without touching call sites.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Deque, List, Optional

from .config import DATA_DIR, user_data_dir
from .schemas import TradeRecord

TRADES_FILE = DATA_DIR / "trades.json"  # legacy single-user path
MAX_TRADES = 1000


class TradeStore:
    """Per-user trade history when `user_id` is given; falls back to the
    legacy shared file otherwise (kept for the single-operator/no-auth path).

    `scope_dir`, when given, isolates this store to a specific account
    scope (used for Tournament accounts -- see orchestrator.py's
    switch_account_mode) instead of the normal per-user file. Live and
    Demo never pass this -- they keep using the exact same file they
    always have, so this is purely additive and changes nothing about
    existing behavior."""

    def __init__(self, user_id: Optional[str] = None, scope_dir: Optional[Path] = None) -> None:
        self.user_id = user_id
        if scope_dir is not None:
            self._file: Path = scope_dir / "trades.json"
        else:
            self._file: Path = (user_data_dir(user_id) / "trades.json") if user_id else TRADES_FILE
        self._trades: Deque[TradeRecord] = deque(maxlen=MAX_TRADES)
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
                for t in raw:
                    self._trades.append(TradeRecord.model_validate(t))
            except Exception:
                pass

    def _persist(self) -> None:
        """Audit fix (M-6): this is the account's entire trade ledger --
        RiskManager, AnalyticsEngine, ConfidenceCalibrator,
        StrategyPerformanceManager, and PatternRiskEngine all depend on it
        surviving a restart intact. The previous implementation wrote
        directly to trades.json on every single trade event; a crash,
        OOM kill, or power loss mid-write could leave a truncated/corrupt
        file, and _load()'s broad except-pass would then silently treat
        that as "no trade history" rather than surfacing the problem.

        Fixed by writing to a temp file in the same directory (same
        filesystem, so the follow-up replace is atomic) and only then
        swapping it into place -- the real file is either the old
        complete version or the new complete version, never a partial
        write."""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps([t.model_dump() for t in self._trades], default=str, indent=2)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._file.parent), prefix=f".{self._file.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            pass

    def add(self, trade: TradeRecord) -> None:
        self._trades.append(trade)
        self._persist()

    def update(self, trade: TradeRecord) -> None:
        for i, t in enumerate(self._trades):
            if t.id == trade.id:
                self._trades[i] = trade
                break
        self._persist()

    def recent(self, limit: int = 100) -> List[TradeRecord]:
        return list(self._trades)[-limit:][::-1]

    def all(self) -> List[TradeRecord]:
        return list(self._trades)

    def remove(self, trade_id: str) -> bool:
        for i, t in enumerate(self._trades):
            if t.id == trade_id:
                del self._trades[i]
                self._persist()
                return True
        return False

    def remove_many(self, ids: List[str]) -> int:
        idset = set(ids)
        kept = [t for t in self._trades if t.id not in idset]
        removed = len(self._trades) - len(kept)
        self._trades.clear()
        self._trades.extend(kept)
        self._persist()
        return removed

    def clear(self) -> int:
        n = len(self._trades)
        self._trades.clear()
        self._persist()
        return n


# Legacy shared store — no longer used once multi-user auth is enabled
# (Orchestrator now creates one TradeStore per user_id). Left in place only
# so any leftover single-user script/import doesn't crash.
store = TradeStore()
