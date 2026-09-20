"""Pytest session setup.

Puts the vendored `pyquotex` broker library on `sys.path` once, for every test
module, using the same resolver the runtime uses
(`app.pyquotex_vendor.ensure_pyquotex_on_path`).

Before this existed, six test modules each did their own
`sys.path.append(... / "vendor" / "pyquotex")` -- a directory that is empty in
a fresh clone (RCA F1) -- so they failed at collection with
`ModuleNotFoundError: No module named 'pyquotex'` and never ran at all. The
resolver also finds `vendor/old-pyquotex`, which is the copy actually
committed.

The entry is appended, not prepended: the vendor tree has its own top-level
`app.py`, and prepending it would shadow this backend's `app` package.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.pyquotex_vendor import ensure_pyquotex_on_path  # noqa: E402

ensure_pyquotex_on_path()


# --------------------------------------------------------------------------- #
# Shared throwaway PostgreSQL fixture
# --------------------------------------------------------------------------- #
# A few tables (broker_sessions, health scores, ...) are PostgreSQL-only -- the
# schema uses JSONB -- so SQLite cannot stand in for them. Tests that need a
# real database request this fixture; it starts a self-contained server from
# the `pgserver` package (see requirements-dev.txt) and points app.db at it for
# the duration.
#
# The fixture is lazy: nothing here runs unless a test actually asks for `pg`,
# so the rest of the suite neither needs nor pays for a database.
import os  # noqa: E402

import pytest  # noqa: E402

_TEST_PGDATA = os.environ.get("QAT_TEST_PGDATA", "/tmp/qat_test_pgdata")


@pytest.fixture(scope="session")
def pg():
    """Start a throwaway PostgreSQL, point app.db at it, run migrations."""
    try:
        import pgserver
    except ImportError:
        pytest.skip(
            "pgserver provides the throwaway PostgreSQL this test needs "
            "(pip install pgserver)"
        )
    import sqlalchemy as sa

    from app import db
    from app.config import settings

    server = pgserver.get_server(_TEST_PGDATA, cleanup_mode=None)
    admin_uri = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)

    admin = sa.create_engine(admin_uri, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = 'qat_test'")
        ).scalar():
            conn.execute(sa.text("CREATE DATABASE qat_test"))
    admin.dispose()

    # Swap the database name and KEEP the query string: pgserver connects over
    # a unix socket expressed as `?host=<pgdata>`, and plain string surgery on
    # the URI silently drops it (which makes psycopg fall back to
    # /var/run/postgresql and fail).
    app_uri = sa.engine.make_url(admin_uri).set(database="qat_test").render_as_string(
        hide_password=False
    )

    # BOTH have to move. `db.tx()` resolves the module-global `engine` at call
    # time, but `init_db()` runs Alembic and builds its own URL from
    # `settings.database_url` -- swapping only the engine leaves the migration
    # connecting to the default localhost:5432.
    original_engine = db.engine
    original_url = settings.database_url
    db.engine = sa.create_engine(app_uri, pool_pre_ping=True)
    settings.database_url = app_uri
    db.init_db()
    try:
        yield app_uri
    finally:
        db.dispose_engine()
        db.engine = original_engine
        settings.database_url = original_url
