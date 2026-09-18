"""Persistence for auto-validation runs and per (asset, strategy) results.

JSON-shaped columns (config, by_regime/by_hour/..., equity/drawdown curves,
details) are stored as PostgreSQL JSONB.

IMPORTANT (bug fix, 2026-08): these values were previously passed as bare
Python dict/list objects through `sqlalchemy.text()`. A raw `text()` construct
carries NO type information, so SQLAlchemy hands the value straight to the
DBAPI -- and psycopg 3 refuses it:

    ProgrammingError: cannot adapt type 'dict' using placeholder '%s'

(lists fared no better: psycopg adapts a Python list to a PostgreSQL ARRAY,
which then fails against a `jsonb` column). The practical effect was that
`create_run()` -- the very first statement of every validation run -- raised,
ValidationService caught it, and the Validation page permanently showed
"failed" with no results ever being written.

The fix is to declare the JSON-shaped bind parameters explicitly with
`bindparam(..., type_=JSONB)`, so SQLAlchemy applies its JSON bind processor
(`json.dumps`) before the value reaches the driver. Reads need no change:
psycopg loads `jsonb` back into Python objects automatically.
"""
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from ..db import tx


# --------------------------------------------------------------------------- #
# Statements (JSON binds typed once, at module import)
# --------------------------------------------------------------------------- #
_INSERT_RUN = text(
    """INSERT INTO backtest_runs (id, user_id, started_at, status, config_json, notes)
       VALUES (:id, :user_id, :started_at, 'running', :config, :notes)"""
).bindparams(bindparam("config", type_=JSONB))

_FINISH_RUN = text(
    """UPDATE backtest_runs SET finished_at = :finished_at, status = :status,
       notes = COALESCE(:notes, notes) WHERE id = :id AND user_id = :user_id"""
)

_INSERT_RESULT = text(
    """INSERT INTO backtest_results (
        id, run_id, user_id, asset, strategy, timeframe,
        win_rate, total_trades, net_profit, profit_factor,
        max_drawdown, max_drawdown_pct, longest_win_streak, longest_loss_streak,
        confidence_accuracy, health_score, health_status,
        oos_folds, oos_consistency,
        by_regime_json, by_hour_json, by_session_json, by_timeframe_json,
        equity_curve_json, drawdown_curve_json, details_json
    ) VALUES (
        :id, :run_id, :user_id, :asset, :strategy, :timeframe,
        :win_rate, :total_trades, :net_profit, :profit_factor,
        :max_drawdown, :max_drawdown_pct, :longest_win_streak, :longest_loss_streak,
        :confidence_accuracy, :health_score, :health_status,
        :oos_folds, :oos_consistency,
        :by_regime, :by_hour, :by_session, :by_timeframe,
        :equity_curve, :drawdown_curve, :details
    )"""
).bindparams(
    bindparam("by_regime", type_=JSONB),
    bindparam("by_hour", type_=JSONB),
    bindparam("by_session", type_=JSONB),
    bindparam("by_timeframe", type_=JSONB),
    bindparam("equity_curve", type_=JSONB),
    bindparam("drawdown_curve", type_=JSONB),
    bindparam("details", type_=JSONB),
)

_UPSERT_HEALTH = text(
    """INSERT INTO health_scores_latest
       (user_id, asset, strategy, timeframe, score, status, computed_at, run_id, details_json)
       VALUES (:user_id, :asset, :strategy, :timeframe, :score, :status, :computed_at, :run_id, :details)
       ON CONFLICT (user_id, asset, strategy, timeframe) DO UPDATE SET
         score = excluded.score,
         status = excluded.status,
         computed_at = excluded.computed_at,
         run_id = excluded.run_id,
         details_json = excluded.details_json"""
).bindparams(bindparam("details", type_=JSONB))


def _as_json_obj(value, fallback):
    """Guard against a caller handing us something the JSONB binder can't
    serialize (e.g. a numpy scalar container or None where a dict is
    expected). Keeps one malformed row from aborting the whole run."""
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    return fallback


class ValidationStore:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def create_run(self, config: dict, notes: str = "") -> str:
        run_id = uuid.uuid4().hex[:16]
        with tx() as conn:
            conn.execute(
                _INSERT_RUN,
                {"id": run_id, "user_id": self.user_id, "started_at": time.time(),
                 "config": _as_json_obj(config, {}), "notes": notes},
            )
        return run_id

    def finish_run(self, run_id: str, status: str, notes: Optional[str] = None) -> None:
        with tx() as conn:
            conn.execute(
                _FINISH_RUN,
                {"finished_at": time.time(), "status": status, "notes": notes,
                 "id": run_id, "user_id": self.user_id},
            )

    def save_result(self, run_id: str, row: dict) -> str:
        rid = uuid.uuid4().hex[:16]
        with tx() as conn:
            conn.execute(
                _INSERT_RESULT,
                {
                    "id": rid, "run_id": run_id, "user_id": self.user_id,
                    "asset": row["asset"], "strategy": row["strategy"], "timeframe": row["timeframe"],
                    "win_rate": row.get("win_rate", 0), "total_trades": row.get("total_trades", 0),
                    "net_profit": row.get("net_profit", 0), "profit_factor": row.get("profit_factor"),
                    "max_drawdown": row.get("max_drawdown", 0), "max_drawdown_pct": row.get("max_drawdown_pct", 0),
                    "longest_win_streak": row.get("longest_win_streak", 0),
                    "longest_loss_streak": row.get("longest_loss_streak", 0),
                    "confidence_accuracy": row.get("confidence_accuracy", 0),
                    "health_score": row.get("health_score", 0), "health_status": row.get("health_status", "muted"),
                    "oos_folds": row.get("oos_folds", 0), "oos_consistency": row.get("oos_consistency", 0),
                    "by_regime": _as_json_obj(row.get("by_regime"), {}),
                    "by_hour": _as_json_obj(row.get("by_hour"), {}),
                    "by_session": _as_json_obj(row.get("by_session"), {}),
                    "by_timeframe": _as_json_obj(row.get("by_timeframe"), {}),
                    "equity_curve": _as_json_obj(row.get("equity_curve"), []),
                    "drawdown_curve": _as_json_obj(row.get("drawdown_curve"), []),
                    "details": _as_json_obj(row.get("details"), {}),
                },
            )
        return rid

    def upsert_health(self, asset: str, strategy: str, timeframe: str, *,
                      score: int, status: str, run_id: str, details: dict) -> None:
        with tx() as conn:
            conn.execute(
                _UPSERT_HEALTH,
                {"user_id": self.user_id, "asset": asset, "strategy": strategy, "timeframe": timeframe,
                 "score": score, "status": status, "computed_at": time.time(), "run_id": run_id,
                 "details": _as_json_obj(details, {})},
            )

    def latest_health_all(self) -> List[dict]:
        with tx() as conn:
            rows = conn.execute(
                text(
                    """SELECT asset, strategy, timeframe, score, status, computed_at, run_id, details_json
                       FROM health_scores_latest WHERE user_id = :user_id ORDER BY score DESC"""
                ),
                {"user_id": self.user_id},
            ).mappings().fetchall()
        out = []
        for r in rows:
            out.append({
                "asset": r["asset"], "strategy": r["strategy"], "timeframe": r["timeframe"],
                "score": r["score"], "status": r["status"], "computed_at": r["computed_at"],
                "run_id": r["run_id"],
                "details": r["details_json"] or {},
            })
        return out

    def health_for_asset(self, asset: str, timeframe: str) -> Dict[str, dict]:
        with tx() as conn:
            rows = conn.execute(
                text(
                    """SELECT strategy, score, status, computed_at, details_json
                       FROM health_scores_latest
                       WHERE user_id = :user_id AND asset = :asset AND timeframe = :timeframe"""
                ),
                {"user_id": self.user_id, "asset": asset, "timeframe": timeframe},
            ).mappings().fetchall()
        return {
            r["strategy"]: {
                "score": r["score"], "status": r["status"],
                "computed_at": r["computed_at"],
                "details": r["details_json"] or {},
            }
            for r in rows
        }

    def list_runs(self, limit: int = 20) -> List[dict]:
        with tx() as conn:
            rows = conn.execute(
                text(
                    """SELECT id, started_at, finished_at, status, config_json, notes
                       FROM backtest_runs WHERE user_id = :user_id ORDER BY started_at DESC LIMIT :limit"""
                ),
                {"user_id": self.user_id, "limit": limit},
            ).mappings().fetchall()
        return [
            {
                "id": r["id"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                "status": r["status"], "config": r["config_json"] or {},
                "notes": r["notes"],
            }
            for r in rows
        ]

    def get_run(self, run_id: str) -> Optional[dict]:
        with tx() as conn:
            run = conn.execute(
                text("SELECT * FROM backtest_runs WHERE id = :id AND user_id = :user_id"),
                {"id": run_id, "user_id": self.user_id},
            ).mappings().fetchone()
            if not run:
                return None
            results = conn.execute(
                text(
                    """SELECT * FROM backtest_results
                       WHERE run_id = :run_id AND user_id = :user_id
                       ORDER BY health_score DESC"""
                ),
                {"run_id": run_id, "user_id": self.user_id},
            ).mappings().fetchall()
        return {
            "id": run["id"], "started_at": run["started_at"], "finished_at": run["finished_at"],
            "status": run["status"], "config": run["config_json"] or {},
            "notes": run["notes"],
            "results": [self._row_to_result(r) for r in results],
        }

    def latest_run_results(self) -> List[dict]:
        with tx() as conn:
            run = conn.execute(
                text(
                    """SELECT id FROM backtest_runs WHERE user_id = :user_id AND status = 'completed'
                       ORDER BY finished_at DESC LIMIT 1"""
                ),
                {"user_id": self.user_id},
            ).mappings().fetchone()
            if not run:
                return []
            rows = conn.execute(
                text(
                    """SELECT * FROM backtest_results
                       WHERE run_id = :run_id AND user_id = :user_id
                       ORDER BY health_score DESC"""
                ),
                {"run_id": run["id"], "user_id": self.user_id},
            ).mappings().fetchall()
        return [self._row_to_result(r) for r in rows]

    def history_for_combo(self, asset: str, strategy: str, timeframe: str, limit: int = 12) -> List[dict]:
        with tx() as conn:
            rows = conn.execute(
                text(
                    """SELECT br.finished_at, br.id AS run_id, r.health_score, r.win_rate,
                              r.net_profit, r.total_trades, r.max_drawdown_pct
                       FROM backtest_results r
                       JOIN backtest_runs br ON br.id = r.run_id
                       WHERE r.user_id = :user_id AND r.asset = :asset AND r.strategy = :strategy
                         AND r.timeframe = :timeframe AND br.status = 'completed'
                       ORDER BY br.finished_at DESC LIMIT :limit"""
                ),
                {"user_id": self.user_id, "asset": asset, "strategy": strategy,
                 "timeframe": timeframe, "limit": limit},
            ).mappings().fetchall()
        return [
            {
                "finished_at": r["finished_at"],
                "run_id": r["run_id"],
                "health_score": r["health_score"],
                "win_rate": r["win_rate"],
                "net_profit": r["net_profit"],
                "total_trades": r["total_trades"],
                "max_drawdown_pct": r["max_drawdown_pct"],
            }
            for r in rows
        ]

    @staticmethod
    def _row_to_result(r) -> dict:
        return {
            "id": r["id"], "asset": r["asset"], "strategy": r["strategy"],
            "timeframe": r["timeframe"], "win_rate": r["win_rate"],
            "total_trades": r["total_trades"], "net_profit": r["net_profit"],
            "profit_factor": r["profit_factor"], "max_drawdown": r["max_drawdown"],
            "max_drawdown_pct": r["max_drawdown_pct"],
            "longest_win_streak": r["longest_win_streak"],
            "longest_loss_streak": r["longest_loss_streak"],
            "confidence_accuracy": r["confidence_accuracy"],
            "health_score": r["health_score"], "health_status": r["health_status"],
            "oos_folds": r["oos_folds"], "oos_consistency": r["oos_consistency"],
            "by_regime": r["by_regime_json"] or {},
            "by_hour": r["by_hour_json"] or {},
            "by_session": r["by_session_json"] or {},
            "by_timeframe": r["by_timeframe_json"] or {},
            "equity_curve": r["equity_curve_json"] or [],
            "drawdown_curve": r["drawdown_curve_json"] or [],
            "details": r["details_json"] or {},
        }
