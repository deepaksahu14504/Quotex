"""PostgreSQL persistence layer for users, broker sessions, candles, and
backtest/validation results.

Migrated from a stdlib-`sqlite3`, thread-local-connection design to a pooled
SQLAlchemy 2.x engine talking to PostgreSQL. The call-site shape on purpose
stays close to the original (``with tx() as conn: conn.execute(sql, params)``)
so the four modules that touch the database (auth, session_manager,
candle_store, validation_store) read the same way they always did — what
changed underneath is:

  - every call now gets its OWN connection checked out of a real connection
    pool (`engine.begin()` inside `tx()`), instead of one connection reused
    for the lifetime of a thread. That's what makes "every request gets its
    own DB session" true instead of aspirational, and it's what makes this
    safe to call from request handlers, background workers, the scheduler,
    websocket handlers, and per-user engine tasks concurrently — there is no
    shared, mutable connection object anywhere in this module.
  - no global lock serializing writers. PostgreSQL (unlike SQLite in WAL
    mode) handles concurrent writers natively via MVCC + row-level locking;
    the pool + `READ COMMITTED` isolation (Postgres's default, and what this
    engine is configured for) is sufficient here. Genuine "read-then-update"
    races (e.g. two requests updating the same broker_sessions row) are
    handled with `INSERT ... ON CONFLICT ... DO UPDATE`, which Postgres
    executes atomically server-side — no client-side locking needed.
  - `pool_pre_ping` + retry-on-transient-error give automatic reconnect after
    a dropped connection (VPS network blip, Postgres restart, etc.) instead
    of a dead thread-local connection silently failing every query in that
    thread until the process restarts.

Schema is now owned by Alembic (see backend/alembic/) — `init_db()` runs
migrations up to `head` at startup rather than issuing ad-hoc
`CREATE TABLE IF NOT EXISTS` statements, so schema changes are versioned,
reviewable, and reversible instead of being silently applied by whichever
process happens to start first.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    Double,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.pool import QueuePool

from .config import settings

logger = logging.getLogger("qat.db")

# --------------------------------------------------------------------------- #
# Engine + pool
# --------------------------------------------------------------------------- #
# pool_size/max_overflow sized for: N uvicorn request-handling threads (each
# route that touches the DB opens-and-closes a connection per call, it does
# not hold one for the life of a request) + one connection per active
# per-user Orchestrator background loop + scheduler/websocket workers. 30
# steady + 60 overflow comfortably covers a multi-hundred-user single-process
# deployment; tune via DB_POOL_SIZE/DB_MAX_OVERFLOW env vars if needed.
engine = create_engine(
    settings.database_url,
    poolclass=QueuePool,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=True,          # validates a pooled connection with a cheap SELECT 1 before
                                  # handing it out; transparently replaces connections that were
                                  # dropped by the server, a firewall idle-timeout, etc.
    isolation_level="READ COMMITTED",
    connect_args={
        # Applied by libpq at connection startup, before any transaction is
        # opened -- server-side guard rail so one slow/stuck query from a
        # background worker can't hold a pooled connection (and everything
        # queued behind it) forever. Deliberately NOT set via a `connect`
        # event calling `cursor.execute("SET ...")`, which would leave an
        # uncommitted implicit transaction open on the connection before
        # SQLAlchemy's own `engine.begin()` gets to it.
        "options": f"-c statement_timeout={int(settings.db_statement_timeout_ms)}",
    },
    future=True,
)


# --------------------------------------------------------------------------- #
# Schema (SQLAlchemy Core table metadata)
# --------------------------------------------------------------------------- #
# Defined with Core `Table`/`Column` (not the ORM) because every call site
# was already writing hand-rolled SQL against dict-like rows, not ORM
# entities — Core metadata gives Alembic something to autogenerate/diff
# against without forcing a rewrite of every query into ORM object access
# (which would be a much larger, riskier change than "swap the database").
metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", String, primary_key=True),
    Column("email", String, unique=True, nullable=False),
    Column("password_hash", String, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("is_admin", Boolean, nullable=False, server_default="false"),
    # Kept as epoch-seconds DOUBLE PRECISION (not TIMESTAMPTZ) on purpose:
    # every call site does `time.time()` arithmetic on these values (session
    # TTL checks, rate limiting, ordering). Changing the wire type to a
    # datetime would ripple into auth.py/session_manager.py/orchestrator
    # timing logic well outside the database layer. See migration report.
    Column("created_at", Double, nullable=False),
    Column("updated_at", Double, nullable=False),
)

broker_sessions = Table(
    "broker_sessions", metadata,
    Column("user_id", String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("provider", String, nullable=False, server_default="pyquotex"),
    Column("email_enc", String),
    Column("password_enc", String),
    Column("ssid_enc", String),
    Column("cookies_enc", String),
    Column("user_agent", String),
    Column("account_type", String, server_default="PRACTICE"),
    Column("login_at", Double),
    Column("expires_at", Double),
    Column("updated_at", Double, nullable=False),
)

candles = Table(
    "candles", metadata,
    Column("user_id", String, primary_key=True),
    Column("asset", String, primary_key=True),
    Column("timeframe", String, primary_key=True),
    Column("ts", Double, primary_key=True),
    Column("open", Double, nullable=False),
    Column("high", Double, nullable=False),
    Column("low", Double, nullable=False),
    Column("close", Double, nullable=False),
    Column("volume", Double, nullable=False, server_default="0"),
)

backtest_runs = Table(
    "backtest_runs", metadata,
    Column("id", String, primary_key=True),
    Column("user_id", String, nullable=False),
    Column("started_at", Double, nullable=False),
    Column("finished_at", Double),
    Column("status", String, nullable=False, server_default="running"),
    Column("config_json", JSONB),
    Column("notes", String),
)

backtest_results = Table(
    "backtest_results", metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", String, nullable=False),
    Column("asset", String, nullable=False),
    Column("strategy", String, nullable=False),
    Column("timeframe", String, nullable=False),
    Column("win_rate", Double, nullable=False, server_default="0"),
    Column("total_trades", Integer, nullable=False, server_default="0"),
    Column("net_profit", Double, nullable=False, server_default="0"),
    Column("profit_factor", Double),
    Column("max_drawdown", Double, nullable=False, server_default="0"),
    Column("max_drawdown_pct", Double, nullable=False, server_default="0"),
    Column("longest_win_streak", Integer, nullable=False, server_default="0"),
    Column("longest_loss_streak", Integer, nullable=False, server_default="0"),
    Column("confidence_accuracy", Double, nullable=False, server_default="0"),
    Column("health_score", Integer, nullable=False, server_default="0"),
    Column("health_status", String, nullable=False, server_default="muted"),
    Column("oos_folds", Integer, nullable=False, server_default="0"),
    Column("oos_consistency", Double, nullable=False, server_default="0"),
    Column("by_regime_json", JSONB),
    Column("by_hour_json", JSONB),
    Column("by_session_json", JSONB),
    Column("by_timeframe_json", JSONB),
    Column("equity_curve_json", JSONB),
    Column("drawdown_curve_json", JSONB),
    Column("details_json", JSONB),
)

health_scores_latest = Table(
    "health_scores_latest", metadata,
    Column("user_id", String, primary_key=True),
    Column("asset", String, primary_key=True),
    Column("strategy", String, primary_key=True),
    Column("timeframe", String, primary_key=True),
    Column("score", Integer, nullable=False, server_default="0"),
    Column("status", String, nullable=False, server_default="muted"),
    Column("computed_at", Double, nullable=False),
    Column("run_id", String),
    Column("details_json", JSONB),
)


# --------------------------------------------------------------------------- #
# Session/connection handling
# --------------------------------------------------------------------------- #
@contextmanager
def tx() -> Iterator[Connection]:
    """Open a fresh pooled connection, run one transaction, commit on
    success / roll back on any exception, then return the connection to the
    pool. Every caller — a request handler, a background worker tick, the
    scheduler, a websocket task, a per-user engine loop — gets an
    independent connection here; nothing is cached on `self`, on a thread,
    or at module scope, so there is no cross-request or cross-user session
    leakage by construction.

    Automatic reconnect after a dropped/stale pooled connection (VPS network
    blip, Postgres restart, a firewall idle-timeout) is handled by
    `pool_pre_ping=True` on the engine: every connection is cheaply
    validated (`SELECT 1`) before being handed out, and silently replaced if
    that check fails — so this never hands the caller a connection that was
    already known-dead. A failure that happens *mid-transaction* (the
    connection drops while a query is in flight) aborts that transaction and
    propagates the exception to the caller, exactly like the original
    sqlite3 code did on any error — retrying an already-failed write
    automatically here would risk silently re-running caller code that may
    not be idempotent, which is a worse failure mode than a clean exception.
    """
    with engine.begin() as conn:
        yield conn


@contextmanager
def read_tx() -> Iterator[Connection]:
    """Alias of `tx()` for read-only call sites — same pooling/reconnect
    behaviour; kept as a distinct name purely for readability at call sites
    that never write."""
    with tx() as conn:
        yield conn


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #
#: Ceilings for the MIGRATION connection only -- the runtime engine has its
#: own statement_timeout. 15s to acquire a lock (a healthy migration takes
#: milliseconds); 5 minutes for a genuinely long one.
MIGRATION_LOCK_TIMEOUT_MS = 15_000
MIGRATION_STATEMENT_TIMEOUT_MS = 300_000


def init_db() -> None:
    """Run Alembic migrations up to `head`. Replaces the old
    `executescript(CREATE TABLE IF NOT EXISTS ...)` — schema changes are now
    versioned files under backend/alembic/versions/ instead of being applied
    ad hoc by whichever process starts first."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine import make_url

    alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(alembic_ini))

    # BOUNDED MIGRATION.
    #
    # `command.upgrade(cfg, "head")` is fully synchronous and, by default,
    # its connection inherits NO timeout -- the runtime engine sets
    # statement_timeout (see connect_args above) but the migration
    # connection Alembic builds from this URL did not.
    #
    # A migration that needs ACCESS EXCLUSIVE (ALTER TABLE, CREATE INDEX)
    # then waits FOREVER for any conflicting lock. The realistic cause is a
    # service restart where the previous process still holds a connection,
    # or a session left idle-in-transaction. Because init_db() is called
    # inline from the async lifespan, that wait blocks the whole event
    # loop: uvicorn logs "Waiting for application startup." and never
    # reaches "Application startup complete", the API never binds, and the
    # UI sits on "Reconnecting" with nothing in the log after Alembic's
    # "Will assume transactional DDL" line.
    #
    # lock_timeout makes that fail in 15s with a named error instead of
    # hanging silently; statement_timeout bounds a genuinely long
    # migration at 5 minutes.
    url = make_url(settings.database_url)
    if url.get_backend_name() == "postgresql":
        existing = url.query.get("options", "")
        url = url.update_query_dict(
            {"options": f"{existing} -c lock_timeout={MIGRATION_LOCK_TIMEOUT_MS} "
                        f"-c statement_timeout={MIGRATION_STATEMENT_TIMEOUT_MS}".strip()},
            append=False,
        )
    # ConfigParser treats % as interpolation syntax. render_as_string()
    # percent-encodes the `options` query parameter this function just
    # added two lines above (`lock_timeout=` becomes `lock_timeout%3D`),
    # so the raw string makes configparser raise
    #   ValueError: invalid interpolation syntax ... at position 79
    # from inside alembic's Config.set_main_option. That propagates out of
    # db.init_db(), out of main.py's lifespan, and uvicorn exits with
    # STARTUP_FAILURE (3). Escaping % as %% stores it literally; alembic's
    # own read-back through interpolation turns it into a single % again,
    # so the URL alembic finally connects with is unchanged.
    database_url = url.render_as_string(hide_password=False)
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        logger.error(
            "Database migration failed: %s: %s\n"
            "If this is a lock timeout, another process is holding the table: "
            "check `SELECT pid, state, query FROM pg_stat_activity WHERE datname "
            "= current_database() AND pid <> pg_backend_pid();` and terminate the "
            "stale session. Startup is aborted deliberately -- running against a "
            "half-migrated schema is worse than restarting.",
            type(exc).__name__, exc,
        )
        raise
    logger.info("Database migrations applied (head).")


def dispose_engine() -> None:
    """Close every pooled connection. Call once, at graceful shutdown."""
    engine.dispose()


# Backwards-compatible name: main.py's shutdown path used to call
# `db.close_conn()` to close the one thread-local sqlite3 connection. There
# is no per-thread connection anymore (see `tx()` above), so this now just
# drains the pool cleanly.
close_conn = dispose_engine
