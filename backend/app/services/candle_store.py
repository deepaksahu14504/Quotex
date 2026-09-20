"""PostgreSQL read-through cache for historical candles.

Validation workers fetch missing ranges from the MarketProvider and persist
deduped rows so repeated backtests do not hammer the broker API.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from sqlalchemy import text

from ..db import tx
from ..schemas import Candle


class CandleStore:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def get_range(
        self,
        asset: str,
        timeframe: str,
        *,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> List[Candle]:
        clauses = ["user_id = :user_id", "asset = :asset", "timeframe = :timeframe"]
        params: dict = {"user_id": self.user_id, "asset": asset, "timeframe": timeframe}
        if from_ts is not None:
            clauses.append("ts >= :from_ts")
            params["from_ts"] = from_ts
        if to_ts is not None:
            clauses.append("ts <= :to_ts")
            params["to_ts"] = to_ts
        sql = (
            f"SELECT ts, open, high, low, close, volume, volume_source FROM candles "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts ASC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        with tx() as conn:
            rows = conn.execute(text(sql), params).mappings().fetchall()
        return [
            Candle(timestamp=r["ts"], open=r["open"], high=r["high"],
                   low=r["low"], close=r["close"], volume=r["volume"] or 0.0,
                   # RCA C.1: read the tag back. NULL means the row was
                   # written before migration 0002 existed and has no
                   # recorded provenance -- "real" preserves the old
                   # behaviour for those pre-existing public-provider
                   # rows. Rows written from 0002 onwards carry their
                   # true tag, so a pyquotex tick-count can no longer be
                   # laundered into "real" volume on the way through PG.
                   volume_source=r["volume_source"] or "real")
            for r in rows
        ]

    def get_latest_n(self, asset: str, timeframe: str, count: int) -> List[Candle]:
        with tx() as conn:
            rows = conn.execute(
                text(
                    """SELECT ts, open, high, low, close, volume, volume_source FROM candles
                       WHERE user_id = :user_id AND asset = :asset AND timeframe = :timeframe
                       ORDER BY ts DESC LIMIT :count"""
                ),
                {"user_id": self.user_id, "asset": asset, "timeframe": timeframe, "count": count},
            ).mappings().fetchall()
        rows = list(reversed(rows))
        return [
            Candle(timestamp=r["ts"], open=r["open"], high=r["high"],
                   low=r["low"], close=r["close"], volume=r["volume"] or 0.0,
                   # RCA C.1: NULL (pre-0002 row, no recorded provenance)
                   # keeps the old "real" default; anything written since
                   # carries its true tag.
                   volume_source=r["volume_source"] or "real")
            for r in rows
        ]

    def upsert_many(self, asset: str, timeframe: str, candles: List[Candle]) -> int:
        """SQLite's `INSERT OR REPLACE` is replaced with a standard
        PostgreSQL `INSERT ... ON CONFLICT (pk) DO UPDATE` upsert, executed
        as one batched statement (executemany-equivalent) instead of one
        round trip per row.

        RCA C.1: `volume_source` IS persisted (migration 0002). It used to be
        dropped here with a note claiming the pyquotex live path bypasses PG;
        that claim does not hold. `orchestrator._run_scan` upserts every scan
        cycle, and `orchestrator.py` / `validation_service.fetch_or_load` read
        the rows back through `get_latest_n`, which preferred the cache over
        the network. Every such row came back tagged "real" no matter what the
        provider said, re-enabling volume mathematics on tick counts -- exactly
        what `indicators._has_real_volume()`'s own docstring exists to prevent.
        """
        if not candles:
            return 0
        rows = [
            {
                "user_id": self.user_id, "asset": asset, "timeframe": timeframe,
                "ts": c.timestamp, "open": c.open, "high": c.high, "low": c.low,
                "close": c.close, "volume": c.volume,
                "volume_source": c.volume_source,
            }
            for c in candles
        ]
        with tx() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO candles (user_id, asset, timeframe, ts, open, high, low, close, volume, volume_source)
                    VALUES (:user_id, :asset, :timeframe, :ts, :open, :high, :low, :close, :volume, :volume_source)
                    ON CONFLICT (user_id, asset, timeframe, ts) DO UPDATE SET
                        open = excluded.open, high = excluded.high, low = excluded.low,
                        close = excluded.close, volume = excluded.volume,
                        volume_source = excluded.volume_source
                    """
                ),
                rows,
            )
        return len(rows)

    async def fetch_or_load(
        self,
        asset: str,
        timeframe: str,
        bars: int,
        fetcher: Callable[[], object],
    ) -> List[Candle]:
        """Return at least `bars` candles, loading from cache first."""
        cached = self.get_latest_n(asset, timeframe, bars)
        if len(cached) >= bars:
            return cached[-bars:]
        fresh = await fetcher()  # type: ignore[misc]
        if fresh:
            self.upsert_many(asset, timeframe, fresh)
            cached = self.get_latest_n(asset, timeframe, bars)
        return cached[-bars:] if len(cached) > bars else cached

    def returns_series(self, asset: str, timeframe: str, count: int = 120) -> List[float]:
        """Log returns for correlation guard."""
        candles = self.get_latest_n(asset, timeframe, count + 1)
        if len(candles) < 3:
            return []
        out: List[float] = []
        for i in range(1, len(candles)):
            prev, cur = candles[i - 1].close, candles[i].close
            if prev > 0 and cur > 0:
                out.append((cur - prev) / prev)
        return out

    def count(self, asset: str, timeframe: str) -> int:
        with tx() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) AS c FROM candles WHERE user_id = :user_id AND asset = :asset AND timeframe = :timeframe"),
                {"user_id": self.user_id, "asset": asset, "timeframe": timeframe},
            ).mappings().fetchone()
        return int(row["c"]) if row else 0

    def last_updated(self, asset: str, timeframe: str) -> Optional[float]:
        with tx() as conn:
            row = conn.execute(
                text(
                    """SELECT MAX(ts) AS ts FROM candles
                       WHERE user_id = :user_id AND asset = :asset AND timeframe = :timeframe"""
                ),
                {"user_id": self.user_id, "asset": asset, "timeframe": timeframe},
            ).mappings().fetchone()
        return float(row["ts"]) if row and row["ts"] is not None else None

    def prune_older_than(self, days: float = 30.0) -> int:
        """Deletes candle rows older than `days` days (for this user, across
        all assets/timeframes). Returns the number of rows deleted.

        Previously there was no retention at all -- every candle ever seen
        was upserted and never removed, so this table grew without bound
        for the lifetime of a 24/7 deployment (disk usage and query time
        both degrade as it grows). Old candles beyond a month or so have no
        remaining use here: live trading only reads the last ~100-200 bars
        (IndicatorCache warm-up window), and backtests/validation define
        their own explicit date range rather than relying on however much
        history happens to still be sitting in this table."""
        cutoff = time.time() - days * 86400.0
        with tx() as conn:
            result = conn.execute(
                text("DELETE FROM candles WHERE user_id = :user_id AND ts < :cutoff"),
                {"user_id": self.user_id, "cutoff": cutoff},
            )
            return result.rowcount
