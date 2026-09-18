#!/usr/bin/env python3
"""READ-ONLY diagnostic for the orphaned `0002_fi_convergence` revision.

Run this ON THE PRODUCTION BOX. It performs SELECTs only -- no DDL, no
DML, no alembic stamp, no writes of any kind. It prints nothing that
could contain a credential.

    cd /path/to/backend && python3 diagnose_alembic_state.py

Why this exists: `0002_fi_convergence` could not be recovered from any
available source, and the repository that was audited diverges from the
deployed schema in at least four independent ways (see the accompanying
report). Reconstructing the migration without seeing the real schema
would be guessing. This dumps exactly what is needed to stop guessing.

Send the full output back. It contains table/column names and types, not
data and not secrets.
"""
from __future__ import annotations

import os
import sys

try:
    from sqlalchemy import create_engine, text
except ImportError:
    sys.exit("sqlalchemy not importable -- run this inside the app's venv")

APP_TABLES = (
    "users", "broker_sessions", "candles", "backtest_runs",
    "backtest_results", "health_scores_latest", "admin_audit_log",
)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            sys.path.insert(0, os.getcwd())
            from app.config import settings  # type: ignore
            url = settings.database_url
        except Exception as exc:
            return int(bool(sys.stderr.write(
                f"Could not resolve DATABASE_URL: {type(exc).__name__}\n")))

    # Deliberately never printed.
    engine = create_engine(url, pool_pre_ping=True)

    with engine.connect() as c:
        print("=" * 68)
        print("1. ALEMBIC STATE")
        print("=" * 68)
        rows = c.execute(text(
            "SELECT version_num FROM alembic_version")).fetchall()
        print(f"  alembic_version rows: {[r[0] for r in rows]}")
        print("  (more than one row means a multi-head graph)")

        print()
        print("=" * 68)
        print("2. TABLES PRESENT")
        print("=" * 68)
        rows = c.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY table_name")
        ).fetchall()
        present = [r[0] for r in rows]
        for t in present:
            mark = "" if t in APP_TABLES or t == "alembic_version" else "   <-- NOT created by 0001"
            print(f"  {t}{mark}")
        missing = [t for t in APP_TABLES if t not in present]
        if missing:
            print(f"\n  expected-but-absent: {missing}")

        print()
        print("=" * 68)
        print("3. COLUMN TYPES  (this is what 0002 most likely changed)")
        print("=" * 68)
        for t in APP_TABLES:
            if t not in present:
                continue
            rows = c.execute(text(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t "
                "ORDER BY ordinal_position"), {"t": t}).fetchall()
            print(f"\n  [{t}]")
            for name, dtype, nullable, default in rows:
                d = f" default={default}" if default else ""
                flag = ""
                if name in ("created_at", "updated_at", "login_at", "expires_at", "ts"):
                    # The app requires epoch-seconds DOUBLE here: db.py:113-119
                    # documents it, and auth.py:57 types created_at as float.
                    flag = "   <-- app expects double precision" \
                        if "double" not in dtype else "   <-- matches app"
                print(f"    {name:22s} {dtype:28s} null={nullable}{d}{flag}")

        print()
        print("=" * 68)
        print("4. INDEXES + CONSTRAINTS")
        print("=" * 68)
        rows = c.execute(text(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() ORDER BY tablename, indexname")
        ).fetchall()
        for tbl, idx, ddl in rows:
            print(f"  {tbl}.{idx}\n      {ddl}")

        print()
        rows = c.execute(text(
            "SELECT conrelid::regclass AS tbl, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace = current_schema()::regnamespace "
            "ORDER BY 1, 2")).fetchall()
        for tbl, name, ddl in rows:
            print(f"  {tbl}.{name}: {ddl}")

        print()
        print("=" * 68)
        print("5. ROW COUNTS  (so nothing is destroyed unnoticed)")
        print("=" * 68)
        for t in present:
            try:
                n = c.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
                print(f"  {t:28s} {n}")
            except Exception as exc:
                print(f"  {t:28s} <count failed: {type(exc).__name__}>")

    print()
    print("=" * 68)
    print("READ-ONLY DIAGNOSTIC COMPLETE — nothing was modified.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
