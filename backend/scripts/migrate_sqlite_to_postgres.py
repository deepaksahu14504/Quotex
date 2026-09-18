#!/usr/bin/env python3
"""One-off data migration: copy every row out of the old SQLite
`backend/data/app.db` into the new PostgreSQL database.

This is a DATA migration only — it does not create tables. Run
`alembic upgrade head` (or just start the app once, which calls
`db.init_db()`) first so the target schema exists, then run this script.

Usage:
    cd backend
    python scripts/migrate_sqlite_to_postgres.py --sqlite data/app.db

    # dry run (counts rows, does not write anything):
    python scripts/migrate_sqlite_to_postgres.py --sqlite data/app.db --dry-run

Safe to re-run: every insert uses `ON CONFLICT DO NOTHING` keyed on each
table's primary key, so running it twice (e.g. after fixing a connectivity
issue partway through) will not duplicate or corrupt rows. It does not
delete or modify anything in the source SQLite file.

Notes on type conversion:
  - is_active / is_admin: SQLite stored 0/1 -> converted to Python bool
    (True/False) so they land in the new BOOLEAN columns correctly.
  - *_json columns: SQLite stored a `json.dumps()` string -> parsed with
    `json.loads()` before insert so they land in the new JSONB columns as
    actual JSON, not a quoted string containing JSON text.
  - Everything else (ids, emails, encrypted blobs, epoch-second timestamps,
    numeric metrics) is copied byte-for-byte / value-for-value. In
    particular the *_enc columns are left exactly as they were — they are
    ciphertext produced by app.security.encrypt() and are decrypted the
    same way regardless of which database stored them, so no
    re-encryption step is needed here.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402

TABLES_IN_FK_ORDER = [
    # (table, columns, json_columns, bool_columns)
    ("users",
     ["id", "email", "password_hash", "is_active", "is_admin", "created_at", "updated_at"],
     [], ["is_active", "is_admin"]),
    ("broker_sessions",
     ["user_id", "provider", "email_enc", "password_enc", "ssid_enc", "cookies_enc",
      "user_agent", "account_type", "login_at", "expires_at", "updated_at"],
     [], []),
    ("candles",
     ["user_id", "asset", "timeframe", "ts", "open", "high", "low", "close", "volume"],
     [], []),
    ("backtest_runs",
     ["id", "user_id", "started_at", "finished_at", "status", "config_json", "notes"],
     ["config_json"], []),
    ("backtest_results",
     ["id", "run_id", "user_id", "asset", "strategy", "timeframe", "win_rate", "total_trades",
      "net_profit", "profit_factor", "max_drawdown", "max_drawdown_pct", "longest_win_streak",
      "longest_loss_streak", "confidence_accuracy", "health_score", "health_status", "oos_folds",
      "oos_consistency", "by_regime_json", "by_hour_json", "by_session_json", "by_timeframe_json",
      "equity_curve_json", "drawdown_curve_json", "details_json"],
     ["by_regime_json", "by_hour_json", "by_session_json", "by_timeframe_json",
      "equity_curve_json", "drawdown_curve_json", "details_json"], []),
    ("health_scores_latest",
     ["user_id", "asset", "strategy", "timeframe", "score", "status", "computed_at", "run_id", "details_json"],
     ["details_json"], []),
]


def _table_pk(table: str) -> list[str]:
    return {
        "users": ["id"],
        "broker_sessions": ["user_id"],
        "candles": ["user_id", "asset", "timeframe", "ts"],
        "backtest_runs": ["id"],
        "backtest_results": ["id"],
        "health_scores_latest": ["user_id", "asset", "strategy", "timeframe"],
    }[table]


def migrate(sqlite_path: Path, dry_run: bool) -> None:
    if not sqlite_path.exists():
        print(f"No SQLite database found at {sqlite_path} -- nothing to migrate.")
        return

    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row

    with engine.begin() as dst:
        for table, cols, json_cols, bool_cols in TABLES_IN_FK_ORDER:
            try:
                src_rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            except sqlite3.OperationalError:
                print(f"  [skip] {table}: not present in source database")
                continue

            print(f"  {table}: {len(src_rows)} row(s) found in SQLite")
            if dry_run or not src_rows:
                continue

            payload = []
            for row in src_rows:
                rec = dict(row)
                for jc in json_cols:
                    if rec.get(jc) is not None:
                        try:
                            rec[jc] = json.loads(rec[jc])
                        except (TypeError, json.JSONDecodeError):
                            rec[jc] = None
                for bc in bool_cols:
                    if rec.get(bc) is not None:
                        rec[bc] = bool(rec[bc])
                payload.append(rec)

            placeholders = ", ".join(f":{c}" for c in cols)
            pk = _table_pk(table)
            stmt = text(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT ({', '.join(pk)}) DO NOTHING"
            )
            dst.execute(stmt, payload)
            print(f"  {table}: inserted (existing rows skipped via ON CONFLICT DO NOTHING)")

    src.close()
    print("Dry run complete -- no data written." if dry_run else "Migration complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", default="data/app.db", help="Path to the old SQLite database file")
    parser.add_argument("--dry-run", action="store_true", help="Count rows only, write nothing")
    args = parser.parse_args()
    migrate(Path(args.sqlite), args.dry_run)
