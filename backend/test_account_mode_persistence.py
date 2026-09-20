"""Regression tests for RCA F4 — Demo/Live account mode must survive a restart.

THE BUG
-------
`switch_account_mode()` switched the live broker connection, updated
`runtime.trading.is_demo` / `account_mode` (persisted to runtime_settings.json,
which drives the UI and `TradeRecord.is_demo`) and called
`provider.switch_account()` -- but nothing ever rewrote
`broker_sessions.account_type`.

On the next restart `_broker_settings()` read that stale column and
`build_provider()` rebuilt the provider with the WRONG `is_demo`, while
`_connect_provider()` re-applied only *tournament* routing. Net effect: after a
Demo -> Live switch and a restart the bot traded the DEMO account while the UI
and the whole trade log said LIVE (and the mirror case -- Live -> Demo -- would
have placed REAL orders on restart).

THE FIX, in three parts, each asserted below
--------------------------------------------
  1. `switch_account_mode()` calls `session_manager.set_account_type()` so the
     column a restart reads from is kept in step.
  2. `_broker_settings()` treats the saved account mode as the source of truth
     and reconciles a disagreeing stored row (covers rows written before the
     fix).
  3. `_connect_provider()` re-applies the full account mode -- demo, live AND
     tournament -- and PAUSES trading if a LIVE account cannot be verified,
     rather than silently trading an unverified account.

The `pg` fixture lives in conftest.py: `broker_sessions` is PostgreSQL-only, so
these run against a throwaway server and skip when pgserver is absent.
"""
from __future__ import annotations

import time

import pytest
import sqlalchemy as sa

from app import db
from app.session_manager import (
    account_type_for,
    is_demo_for,
    session_manager,
)

TEST_USER = "f4-account-mode-user"


@pytest.fixture()
def user_row(pg):
    """A broker_sessions row logged in as DEMO, plus the users row its FK needs."""
    from app.security import encrypt  # noqa: F401  (ensures the key is loaded)

    with db.tx() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, email, password_hash, created_at, updated_at) "
                "VALUES (:id, :email, :ph, :now, :now) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": TEST_USER, "email": f"{TEST_USER}@example.invalid", "ph": "x", "now": time.time()},
        )
    session_manager.save_credentials(
        TEST_USER, "broker@example.invalid", "pw", is_demo=True
    )
    yield TEST_USER
    with db.tx() as conn:
        conn.execute(sa.text("DELETE FROM broker_sessions WHERE user_id = :u"), {"u": TEST_USER})
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": TEST_USER})


# --------------------------------------------------------------------------- #
# The PRACTICE <-> REAL mapping (the contract the whole bug turns on)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("is_demo,expected", [(True, "PRACTICE"), (False, "REAL")])
def test_account_type_for(is_demo, expected):
    assert account_type_for(is_demo) == expected


@pytest.mark.parametrize("account_type,expected", [
    ("PRACTICE", True), ("REAL", False), (None, True), ("", True), ("practice", True),
])
def test_is_demo_for(account_type, expected):
    """Anything that is not exactly REAL must fail towards DEMO, never towards
    placing real-money orders."""
    assert is_demo_for(account_type) is expected


@pytest.mark.parametrize("is_demo", [True, False])
def test_mapping_round_trips(is_demo):
    assert is_demo_for(account_type_for(is_demo)) is is_demo


# --------------------------------------------------------------------------- #
# Persisted round-trip through the real table
# --------------------------------------------------------------------------- #

def test_save_credentials_persists_the_account_type(user_row):
    creds = session_manager.get_credentials(user_row)
    assert creds is not None
    assert creds.quotex_is_demo is True

    session_manager.save_credentials(user_row, "broker@example.invalid", "pw", is_demo=False)
    assert session_manager.get_credentials(user_row).quotex_is_demo is False


def test_set_account_type_updates_only_the_account_type(user_row):
    """The mode switch must not disturb the stored email/password/user-agent."""
    session_manager.save_credentials(
        user_row, "broker@example.invalid", "pw", is_demo=True, user_agent="UA/1.0"
    )
    before = session_manager.get_credentials(user_row)

    assert session_manager.set_account_type(user_row, False) is True

    after = session_manager.get_credentials(user_row)
    assert after.quotex_is_demo is False, "account_type did not persist"
    assert after.quotex_email == before.quotex_email
    assert after.quotex_password == before.quotex_password
    assert after.quotex_user_agent == before.quotex_user_agent


def test_set_account_type_reports_missing_row(pg):
    assert session_manager.set_account_type("no-such-user-f4", False) is False


def test_demo_live_demo_cycle_persists(user_row):
    """The exact scenario from the RCA, replayed against the real table."""
    assert session_manager.get_credentials(user_row).quotex_is_demo is True   # logged in demo
    session_manager.set_account_type(user_row, False)                          # -> LIVE
    assert session_manager.get_credentials(user_row).quotex_is_demo is False
    session_manager.set_account_type(user_row, True)                           # -> DEMO
    assert session_manager.get_credentials(user_row).quotex_is_demo is True


# --------------------------------------------------------------------------- #
# Orchestrator wiring
# --------------------------------------------------------------------------- #

class _FakeProvider:
    """Records switch_account calls; `result` controls success."""

    name = "pyquotex"

    def __init__(self, result=True, connects=True):
        self.calls = []
        self.result = result
        self.connects = connects
        self._is_demo = True
        self._tournament_id = None
        self._connected = False

    def supports_tournaments(self):
        return True

    @property
    def connected(self):
        return self._connected

    async def connect(self):
        self._connected = self.connects
        return self.connects

    async def switch_account(self, mode, tournament_id=None):
        self.calls.append((mode, tournament_id))
        if self.result:
            self._is_demo = mode in ("demo", "tournament")
            self._tournament_id = tournament_id if mode == "tournament" else None
        return self.result

    async def get_balance(self):
        return 10_000.0, "USD"


def _make_orchestrator(monkeypatch, provider_result=True):
    """Build a real Orchestrator with the broker/provider edges stubbed."""
    from app.config import RuntimeSettings
    from app.orchestrator import Orchestrator
    from app.session_manager import BrokerCredentials

    creds = BrokerCredentials(
        quotex_email="broker@example.invalid", quotex_password="pw", quotex_is_demo=True
    )
    monkeypatch.setattr(session_manager, "get_credentials", lambda uid: creds)
    monkeypatch.setattr(session_manager, "set_account_type", lambda uid, d: True)

    runtime = RuntimeSettings()
    runtime.trading.is_demo = True
    runtime.trading.account_mode = "demo"
    orch = Orchestrator(TEST_USER, runtime)
    orch.provider = _FakeProvider(provider_result)
    orch.state.provider = "pyquotex"
    # Keep the test off the shared backend/data directory. `save` is a method
    # on the pydantic model, so it has to be patched on the CLASS -- setting it
    # on the instance raises "object has no field 'save'".
    monkeypatch.setattr(RuntimeSettings, "save", lambda self, user_id=None: None)
    return orch


def test_broker_settings_honours_the_saved_account_mode(monkeypatch, pg, user_row):
    """Part 2 of the fix: the saved mode is the source of truth.

    Stored row says PRACTICE (stale, written before the fix); the user has
    since selected LIVE. The provider must be built for LIVE.
    """
    orch = _make_orchestrator(monkeypatch)
    session_manager.save_credentials(user_row, "broker@example.invalid", "pw", is_demo=True)
    orch.runtime.trading.is_demo = False
    orch.runtime.trading.account_mode = "live"

    assert orch._broker_settings().quotex_is_demo is False


def test_broker_settings_leaves_a_consistent_row_alone(monkeypatch, pg, user_row):
    orch = _make_orchestrator(monkeypatch)
    session_manager.save_credentials(user_row, "broker@example.invalid", "pw", is_demo=True)
    orch.runtime.trading.is_demo = True
    orch.runtime.trading.account_mode = "demo"

    assert orch._broker_settings().quotex_is_demo is True


@pytest.mark.asyncio
async def test_switch_account_mode_persists_the_account_type(monkeypatch, pg, user_row):
    """Part 1 of the fix: switching must write the column a restart reads."""
    orch = _make_orchestrator(monkeypatch)
    saved = []
    monkeypatch.setattr(
        session_manager, "set_account_type", lambda uid, d: saved.append((uid, d)) or True
    )
    monkeypatch.setattr(orch, "_rebuild_account_scoped_stores", lambda d: None)
    monkeypatch.setattr(orch, "_account_scope_dir", lambda m, t: "/tmp/f4-scope")

    ok, _msg = await orch.switch_account_mode("live")
    assert ok is True
    assert saved == [(TEST_USER, False)], "switch_account_mode did not persist is_demo=False"

    ok, _msg = await orch.switch_account_mode("demo")
    assert ok is True
    assert saved[-1] == (TEST_USER, True)


@pytest.mark.asyncio
async def test_reapply_account_mode_covers_live_not_just_tournament(monkeypatch, pg, user_row):
    """Part 3 of the fix. The old code had ONE branch, for tournament."""
    orch = _make_orchestrator(monkeypatch)
    orch.runtime.trading.account_mode = "live"
    orch.runtime.trading.is_demo = False

    await orch._reapply_account_mode()

    assert orch.provider.calls == [("live", None)]
    assert orch.state.is_demo is False
    assert not orch.risk.paused


@pytest.mark.asyncio
async def test_reapply_account_mode_covers_demo(monkeypatch, pg, user_row):
    orch = _make_orchestrator(monkeypatch)
    orch.runtime.trading.account_mode = "demo"
    orch.runtime.trading.is_demo = True

    await orch._reapply_account_mode()

    assert orch.provider.calls == [("demo", None)]
    assert orch.state.is_demo is True


@pytest.mark.asyncio
async def test_reapply_account_mode_covers_tournament(monkeypatch, pg, user_row):
    orch = _make_orchestrator(monkeypatch)
    orch.runtime.trading.account_mode = "tournament"
    orch.runtime.trading.tournament_id = 4242
    orch.runtime.trading.is_demo = True

    await orch._reapply_account_mode()

    assert orch.provider.calls == [("tournament", 4242)]
    assert orch.state.tournament_id == 4242


@pytest.mark.asyncio
async def test_unverifiable_live_account_pauses_trading(monkeypatch, pg, user_row):
    """The safety half of part 3: never trade a LIVE account we could not
    verify. Coming up on demo-when-live-was-asked-for produces a trade log
    that lies; the reverse risks real money, so it must stop."""
    orch = _make_orchestrator(monkeypatch, provider_result=False)
    orch.runtime.trading.account_mode = "live"
    orch.runtime.trading.is_demo = False
    assert orch.risk.paused is False

    await orch._reapply_account_mode()

    assert orch.risk.paused is True, "LIVE could not be verified but trading continued"


@pytest.mark.asyncio
async def test_unverifiable_demo_account_does_not_pause(monkeypatch, pg, user_row):
    """Wrong-but-harmless direction: warn, keep trading."""
    orch = _make_orchestrator(monkeypatch, provider_result=False)
    orch.runtime.trading.account_mode = "demo"
    orch.runtime.trading.is_demo = True

    await orch._reapply_account_mode()

    assert orch.risk.paused is False


@pytest.mark.asyncio
async def test_tournament_without_an_id_falls_back_to_demo(monkeypatch, pg, user_row):
    """No tournament id -> nothing to route into; must land on a known
    account rather than leave the session wherever it happened to be."""
    orch = _make_orchestrator(monkeypatch)
    orch.runtime.trading.account_mode = "tournament"
    orch.runtime.trading.tournament_id = None
    orch.runtime.trading.is_demo = True

    await orch._reapply_account_mode()

    assert orch.provider.calls == [("demo", None)]


# --------------------------------------------------------------------------- #
# The WIRING: _connect_provider must actually call the re-apply
# --------------------------------------------------------------------------- #
#
# The tests above call `_reapply_account_mode()` directly, so they would still
# pass if `_connect_provider` never invoked it -- which is precisely the
# pre-fix behaviour (a single `if account_mode == "tournament"` branch). These
# go through `_connect_provider()` so the call site itself is under test.

@pytest.mark.asyncio
async def test_connect_provider_reapplies_live(monkeypatch, pg, user_row):
    """THE regression. Pre-fix, `_connect_provider` re-applied ONLY tournament
    routing, so a restart after Demo -> Live came back up on the demo account
    with no log line and no UI signal."""
    orch = _make_orchestrator(monkeypatch)
    orch.runtime.trading.account_mode = "live"
    orch.runtime.trading.is_demo = False

    await orch._connect_provider()

    assert orch.provider.calls == [("live", None)], (
        "_connect_provider did not re-apply the LIVE account mode"
    )
    assert orch.state.is_demo is False


@pytest.mark.asyncio
async def test_connect_provider_reapplies_demo(monkeypatch, pg, user_row):
    orch = _make_orchestrator(monkeypatch)
    orch.runtime.trading.account_mode = "demo"
    orch.runtime.trading.is_demo = True

    await orch._connect_provider()

    assert orch.provider.calls == [("demo", None)]


@pytest.mark.asyncio
async def test_connect_provider_reapplies_tournament(monkeypatch, pg, user_row):
    """The one case the old code DID handle -- must keep working."""
    orch = _make_orchestrator(monkeypatch)
    orch.runtime.trading.account_mode = "tournament"
    orch.runtime.trading.tournament_id = 99
    orch.runtime.trading.is_demo = True

    await orch._connect_provider()

    assert orch.provider.calls == [("tournament", 99)]


@pytest.mark.asyncio
async def test_connect_provider_pauses_when_live_cannot_be_verified(monkeypatch, pg, user_row):
    orch = _make_orchestrator(monkeypatch, provider_result=False)
    orch.runtime.trading.account_mode = "live"
    orch.runtime.trading.is_demo = False
    assert orch.risk.paused is False

    await orch._connect_provider()

    assert orch.risk.paused is True


@pytest.mark.asyncio
async def test_connect_provider_does_not_reapply_when_connect_fails(monkeypatch, pg, user_row):
    """A failed login must not push account-routing messages at a dead socket."""
    orch = _make_orchestrator(monkeypatch)
    orch.provider = _FakeProvider(connects=False)
    orch.runtime.trading.account_mode = "live"

    await orch._connect_provider()

    assert orch.provider.calls == []
