"""Health Score manager — per (asset, strategy, timeframe) validation state.

Used by the live orchestrator to gate which strategies may vote on each asset.
Does not replace StrategyPerformanceManager; both filters apply.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..services.validation_store import ValidationStore


ACTIVE_MIN = 80
TRIAL_MIN = 55


def score_to_status(score: int, *, active_min: int = ACTIVE_MIN, trial_min: int = TRIAL_MIN) -> str:
    if score >= active_min:
        return "active"
    if score >= trial_min:
        return "trial"
    return "muted"


class HealthScoreManager:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self._store = ValidationStore(user_id)
        self._cache: Dict[str, Dict[str, dict]] = {}  # asset -> strategy -> info
        self._reload()

    def _reload(self) -> None:
        self._cache.clear()
        for row in self._store.latest_health_all():
            key = f"{row['asset']}|{row['timeframe']}"
            self._cache.setdefault(key, {})[row["strategy"]] = row

    def refresh(self) -> None:
        self._reload()

    def filter_strategies(self, asset: str, timeframe: str, strategies: List[str]) -> List[str]:
        """Keep strategies that are active or trial for this asset; drop muted."""
        key = f"{asset}|{timeframe}"
        health = self._cache.get(key, {})
        if not health:
            return strategies  # no validation data yet — don't block live trading
        out = []
        for name in strategies:
            h = health.get(name)
            if h is None:
                out.append(name)  # not yet evaluated — allow
            elif h.get("status") in ("active", "trial"):
                out.append(name)
        return out

    def status_matrix(self) -> List[dict]:
        rows = []
        for key, strats in self._cache.items():
            asset, tf = key.split("|", 1)
            for name, info in strats.items():
                rows.append({
                    "asset": asset, "strategy": name, "timeframe": tf,
                    "score": info.get("score", 0), "status": info.get("status", "muted"),
                    "computed_at": info.get("computed_at"),
                    "details": info.get("details", {}),
                })
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    def apply_validation_results(
        self,
        run_id: str,
        results: List[dict],
        *,
        active_min: int = ACTIVE_MIN,
        trial_min: int = TRIAL_MIN,
    ) -> None:
        for row in results:
            status = score_to_status(int(row.get("health_score", 0)),
                                     active_min=active_min, trial_min=trial_min)
            self._store.upsert_health(
                row["asset"], row["strategy"], row["timeframe"],
                score=int(row.get("health_score", 0)),
                status=status,
                run_id=run_id,
                details={
                    "win_rate": row.get("win_rate"),
                    "net_profit": row.get("net_profit"),
                    "profit_factor": row.get("profit_factor"),
                    "max_drawdown_pct": row.get("max_drawdown_pct"),
                    "oos_consistency": row.get("oos_consistency"),
                },
            )
        self._reload()

    def sync_strategy_manager(self, strategy_manager, configured: List[str]) -> List[str]:
        """Optional global SPM sync: mute strategies with no active/trial asset health."""
        changed: List[str] = []
        by_strategy: Dict[str, List[str]] = {}
        for key, strats in self._cache.items():
            for name, info in strats.items():
                if name not in configured:
                    continue
                by_strategy.setdefault(name, []).append(info.get("status", "muted"))

        for name in configured:
            statuses = by_strategy.get(name, [])
            if not statuses:
                continue
            if all(s == "muted" for s in statuses):
                st = strategy_manager._get(name)  # noqa: SLF001 — intentional sync hook
                if st.status != "muted" and st.manual_override is None:
                    st.status = "muted"
                    st.mute_reason = "Auto-validation: low health on all assets"
                    import time
                    st.muted_at = time.time()
                    changed.append(name)
            elif any(s == "active" for s in statuses) and all(s != "trial" for s in statuses if s == "active" or s == "trial"):
                st = strategy_manager._get(name)
                if st.status == "muted" and st.manual_override is None:
                    st.status = "trial"
                    st.trial_results = []
                    changed.append(name)
        if changed:
            strategy_manager._save()
        return changed
