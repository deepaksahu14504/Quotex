"""Initial PostgreSQL schema (baseline for the SQLite -> PostgreSQL migration)

This creates the schema fresh on PostgreSQL. It is deliberately NOT a
column-for-column mechanical translation of the old `CREATE TABLE IF NOT
EXISTS` statements in the pre-migration db.py — a few types were upgraded to
use native PostgreSQL features (see MIGRATION_REPORT.md "Schema changes"):
  - is_active / is_admin: INTEGER 0/1 -> BOOLEAN
  - *_json columns:       TEXT (json.dumps'd) -> JSONB
  - PK/FK constraints:    now enforced by PostgreSQL itself (SQLite enforced
                           FKs only when `PRAGMA foreign_keys=ON` was set per
                           connection, which the old db.py never did)

Existing SQLite data is carried over separately by
scripts/migrate_sqlite_to_postgres.py, which INSERTs into these same tables
after they're created by this migration -- this file only defines
structure, it does not touch data.

Revision ID: 0001
Revises:
Create Date: 2026-07-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("email", sa.String, nullable=False),
        sa.Column("password_hash", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Double, nullable=False),
        sa.Column("updated_at", sa.Double, nullable=False),
    )
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    # Every login and every auth-dependent request looks the user up by
    # email; this is the single hottest query against this table.
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "broker_sessions",
        sa.Column("user_id", sa.String, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider", sa.String, nullable=False, server_default="pyquotex"),
        sa.Column("email_enc", sa.String),
        sa.Column("password_enc", sa.String),
        sa.Column("ssid_enc", sa.String),
        sa.Column("cookies_enc", sa.String),
        sa.Column("user_agent", sa.String),
        sa.Column("account_type", sa.String, server_default="PRACTICE"),
        sa.Column("login_at", sa.Double),
        sa.Column("expires_at", sa.Double),
        sa.Column("updated_at", sa.Double, nullable=False),
    )

    op.create_table(
        "candles",
        sa.Column("user_id", sa.String, primary_key=True),
        sa.Column("asset", sa.String, primary_key=True),
        sa.Column("timeframe", sa.String, primary_key=True),
        sa.Column("ts", sa.Double, primary_key=True),
        sa.Column("open", sa.Double, nullable=False),
        sa.Column("high", sa.Double, nullable=False),
        sa.Column("low", sa.Double, nullable=False),
        sa.Column("close", sa.Double, nullable=False),
        sa.Column("volume", sa.Double, nullable=False, server_default="0"),
    )
    # The composite primary key (user_id, asset, timeframe, ts) already
    # gives us a covering index for get_range()/get_latest_n()'s
    # `WHERE user_id=? AND asset=? AND timeframe=? ORDER BY ts` access
    # pattern (leading columns of the PK match the WHERE clause, ts is the
    # trailing/sort column) -- no separate index needed here.

    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("started_at", sa.Double, nullable=False),
        sa.Column("finished_at", sa.Double),
        sa.Column("status", sa.String, nullable=False, server_default="running"),
        sa.Column("config_json", JSONB),
        sa.Column("notes", sa.String),
    )
    # list_runs(): WHERE user_id = ? ORDER BY started_at DESC
    op.create_index("ix_backtest_runs_user_started", "backtest_runs", ["user_id", sa.text("started_at DESC")])
    # latest_run_results(): WHERE user_id = ? AND status = 'completed' ORDER BY finished_at DESC
    op.create_index(
        "ix_backtest_runs_user_status_finished",
        "backtest_runs", ["user_id", "status", sa.text("finished_at DESC")],
    )

    op.create_table(
        "backtest_results",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("run_id", sa.String, sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("asset", sa.String, nullable=False),
        sa.Column("strategy", sa.String, nullable=False),
        sa.Column("timeframe", sa.String, nullable=False),
        sa.Column("win_rate", sa.Double, nullable=False, server_default="0"),
        sa.Column("total_trades", sa.Integer, nullable=False, server_default="0"),
        sa.Column("net_profit", sa.Double, nullable=False, server_default="0"),
        sa.Column("profit_factor", sa.Double),
        sa.Column("max_drawdown", sa.Double, nullable=False, server_default="0"),
        sa.Column("max_drawdown_pct", sa.Double, nullable=False, server_default="0"),
        sa.Column("longest_win_streak", sa.Integer, nullable=False, server_default="0"),
        sa.Column("longest_loss_streak", sa.Integer, nullable=False, server_default="0"),
        sa.Column("confidence_accuracy", sa.Double, nullable=False, server_default="0"),
        sa.Column("health_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("health_status", sa.String, nullable=False, server_default="muted"),
        sa.Column("oos_folds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("oos_consistency", sa.Double, nullable=False, server_default="0"),
        sa.Column("by_regime_json", JSONB),
        sa.Column("by_hour_json", JSONB),
        sa.Column("by_session_json", JSONB),
        sa.Column("by_timeframe_json", JSONB),
        sa.Column("equity_curve_json", JSONB),
        sa.Column("drawdown_curve_json", JSONB),
        sa.Column("details_json", JSONB),
    )
    # get_run(): WHERE run_id = ? ORDER BY health_score DESC
    op.create_index("ix_backtest_results_run_health", "backtest_results", ["run_id", sa.text("health_score DESC")])
    # history_for_combo(): WHERE user_id=? AND asset=? AND strategy=? AND timeframe=?
    op.create_index(
        "ix_backtest_results_combo",
        "backtest_results", ["user_id", "asset", "strategy", "timeframe"],
    )

    op.create_table(
        "health_scores_latest",
        sa.Column("user_id", sa.String, primary_key=True),
        sa.Column("asset", sa.String, primary_key=True),
        sa.Column("strategy", sa.String, primary_key=True),
        sa.Column("timeframe", sa.String, primary_key=True),
        sa.Column("score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String, nullable=False, server_default="muted"),
        sa.Column("computed_at", sa.Double, nullable=False),
        sa.Column("run_id", sa.String),
        sa.Column("details_json", JSONB),
    )
    # latest_health_all(): WHERE user_id = ? ORDER BY score DESC
    op.create_index("ix_health_scores_user_score", "health_scores_latest", ["user_id", sa.text("score DESC")])


def downgrade() -> None:
    op.drop_table("health_scores_latest")
    op.drop_table("backtest_results")
    op.drop_table("backtest_runs")
    op.drop_table("candles")
    op.drop_table("broker_sessions")
    op.drop_table("users")
