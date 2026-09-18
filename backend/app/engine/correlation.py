"""Rolling correlation guard — avoids stacking correlated positions."""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional, Tuple

from ..services.candle_store import CandleStore
from ..logging_setup import log_event

logger = logging.getLogger("qat.correlation")


def _pearson(a: List[float], b: List[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 8:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


class CorrelationGuard:
    def __init__(
        self, candle_store: CandleStore, *, threshold: float = 0.85, lookback: int = 120,
        cache_ttl_seconds: float = 900.0,
    ) -> None:
        self.store = candle_store
        self.threshold = threshold
        self.lookback = lookback
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: Dict[Tuple[str, str], Tuple[Optional[float], float]] = {}

    def correlation(self, asset_a: str, asset_b: str, timeframe: str) -> Optional[float]:
        if asset_a == asset_b:
            return 1.0
        key = tuple(sorted((asset_a, asset_b))) + (timeframe,)
        cached = self._cache.get(key)
        if cached is not None:
            value, computed_at = cached
            if time.time() - computed_at < self.cache_ttl_seconds:
                return value
        ra = self.store.returns_series(asset_a, timeframe, self.lookback)
        rb = self.store.returns_series(asset_b, timeframe, self.lookback)
        c = _pearson(ra, rb)
        self._cache[key] = (c, time.time())
        return c

    def blocks_trade(
        self,
        candidate_asset: str,
        candidate_direction: str,
        open_assets: List[Tuple[str, str]],
        timeframe: str,
    ) -> Optional[str]:
        """Return a denial reason if correlated exposure exists."""
        for asset, direction in open_assets:
            if asset == candidate_asset:
                log_event(logger, logging.DEBUG, "correlation_guard_rejected",
                          candidate_asset=candidate_asset, blocking_asset=asset, reason="same_asset_already_open")
                return f"Already have open position on {asset}"
            corr = self.correlation(candidate_asset, asset, timeframe)
            if corr is None:
                continue
            if abs(corr) >= self.threshold and direction == candidate_direction:
                log_event(logger, logging.DEBUG, "correlation_guard_rejected",
                          candidate_asset=candidate_asset, blocking_asset=asset, correlation=round(corr, 3),
                          threshold=self.threshold, direction=candidate_direction,
                          reason="correlated_same_direction_exposure")
                return f"Correlation {corr:.2f} with open {asset} (same direction risk)"
        log_event(logger, logging.DEBUG, "correlation_guard_passed", candidate_asset=candidate_asset, open_asset_count=len(open_assets))
        return None

    def penalize_score(self, asset: str, peer_assets: List[str], timeframe: str) -> float:
        """Validation penalty when strategy performance overlaps correlated assets."""
        if not peer_assets:
            return 0.0
        corrs = [abs(self.correlation(asset, p, timeframe) or 0) for p in peer_assets if p != asset]
        if not corrs:
            return 0.0
        avg = sum(corrs) / len(corrs)
        if avg >= self.threshold:
            return min(15.0, (avg - self.threshold) * 100)
        return 0.0

    def invalidate_cache(self) -> None:
        self._cache.clear()
