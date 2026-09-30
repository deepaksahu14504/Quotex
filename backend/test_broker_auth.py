"""Tests for QuotexAuthSessionManager -- broker authentication lifecycle.

Each test drives the REAL manager. No login logic is re-implemented here:
storage, login and validation are injected callables, so a test proves the
manager's decisions rather than a stand-in.

Nothing here talks to the network and nothing here bypasses a broker control.
"""

from __future__ import annotations

import asyncio
import json
import stat
from types import SimpleNamespace

import pytest

from app.broker_auth import (
    AUTH_BROWSER_REQUIRED,
    AUTH_FRESH_SESSION_REQUIRED,
    AUTH_FRESH_SESSION_STARTED,
    AUTH_OTP_REQUIRED,
    AUTH_RECONNECT_FAILED,
    AUTH_RECONNECT_SUCCESS,
    AUTH_SESSION_EXPIRED,
    AUTH_SESSION_LOAD,
    AUTH_SESSION_SAVED,
    AUTH_SESSION_VALID,
    QuotexAuthSessionManager,
    SessionBundle,
    ValidationResult,
)


class FakeStorage:
    """Stands in for the encrypted per-user session store."""

    def __init__(self):
        self.rows = {}

    def persist(self, user_id, ssid, cookies, user_agent):
        self.rows[user_id] = {"ssid": ssid, "cookies": cookies, "ua": user_agent}

    def discard(self, user_id):
        self.rows.pop(user_id, None)

    def load(self, user_id):
        row = self.rows.get(user_id)
        if not row:
            return None
        return SessionBundle(ssid=row["ssid"], cookies=row["cookies"],
                             user_agent=row.get("ua"))


def make_probe(status):
    """Build an auth probe from either one verdict or a scripted sequence.

    A sequence is consumed one call at a time and then CLAMPS on its final
    value: a broker's verdict stays put until it actually changes, and an
    exhausted list must not turn into a spurious IndexError.
    """
    if isinstance(status, str):
        return lambda: status
    seq = list(status)
    assert seq, "an empty verdict sequence proves nothing"

    def probe():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return probe


def make_manager(tmp_path, user_id, status="unknown", storage=None,
                 login_result=True, login_side_effect=None, on_event=None,
                 load_override=None):
    """Build a real manager with injected storage/login/probe."""
    store = storage if storage is not None else FakeStorage()
    calls = {"login": 0}

    async def fresh_login():
        calls["login"] += 1
        if login_side_effect is not None:
            login_side_effect(calls["login"])
        if not login_result:
            return False
        # sync store: must NOT be awaited
        store.persist(user_id, f"ssid-{calls['login']}", "cookie=1", "UA/1.0")
        return True

    manager = QuotexAuthSessionManager(
        user_id,
        session_dir=tmp_path / user_id,
        persist=lambda ssid, cookies, ua: store.persist(user_id, ssid, cookies, ua),
        load=(load_override if load_override is not None
              else (lambda: store.load(user_id))),
        fresh_login=fresh_login,
        auth_probe=make_probe(status),
        discard=lambda: store.discard(user_id),
        on_event=on_event,
    )
    return manager, store, calls


# ── 1. session reuse when valid ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_session_is_reused_without_logging_in(tmp_path):
    events = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status="authenticated",
        on_event=lambda e, **kw: events.append((e, kw)),
    )
    store.persist("u1", "existing-ssid", "cookie=1", "UA/1.0")

    result = await manager.validate_session()

    assert result.valid is True
    assert result.bundle.ssid == "existing-ssid"
    assert calls["login"] == 0, "a valid session must not trigger a login"
    names = [n for n, _ in events]
    assert AUTH_SESSION_LOAD in names
    assert AUTH_SESSION_VALID in names
    assert manager.can_trade


@pytest.mark.asyncio
async def test_reconnect_with_valid_session_does_not_log_in(tmp_path):
    manager, store, calls = make_manager(tmp_path, "u1", status="authenticated")
    store.persist("u1", "existing", "cookie=1", "UA/1.0")

    assert await manager.reconnect_session() is True
    assert calls["login"] == 0


# ── 2. session missing ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_session_is_detected_and_a_fresh_one_is_created(tmp_path):
    events = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status="authenticated",
        on_event=lambda e, **kw: events.append(e),
    )

    result = await manager.validate_session()
    assert result.valid is False
    assert result.reason == "no stored session"
    assert calls["login"] == 0, "validation alone must never log in"

    assert await manager.reconnect_session() is True
    assert calls["login"] == 1
    assert AUTH_FRESH_SESSION_REQUIRED in events
    assert AUTH_RECONNECT_SUCCESS in events


# ── 3. session expired ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_expired_session_is_not_reused(tmp_path):
    events = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status="expired",
        on_event=lambda e, **kw: events.append((e, kw)),
    )
    store.persist("u1", "stale-ssid", "cookie=1", "UA/1.0")

    result = await manager.validate_session()

    assert result.valid is False
    assert AUTH_SESSION_EXPIRED in [n for n, _ in events]
    assert manager.can_trade is False


@pytest.mark.asyncio
async def test_expired_session_is_replaced_not_retried(tmp_path):
    manager, store, calls = make_manager(
        tmp_path, "u1", status=["expired", "authenticated"],
    )
    store.persist("u1", "stale-ssid", "cookie=1", "UA/1.0")

    assert await manager.reconnect_session() is True
    assert calls["login"] == 1
    assert store.rows["u1"]["ssid"] == "ssid-1", "the stale session was replaced"


# ── 4. auth rejection handling ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_rejection_invalidates_and_establishes_a_new_session(tmp_path):
    seen = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status=["failed", "authenticated"],
        on_event=lambda e, **kw: seen.append(e),
    )
    store.persist("u1", "rejected-ssid", "cookie=1", "UA/1.0")

    ok = await manager.reconnect_session()

    assert ok is True
    assert calls["login"] == 1
    assert store.rows["u1"]["ssid"] == "ssid-1"
    assert AUTH_SESSION_EXPIRED in seen
    assert AUTH_SESSION_SAVED in seen
    assert AUTH_RECONNECT_SUCCESS in seen


@pytest.mark.asyncio
async def test_rejected_session_is_never_blindly_retried(tmp_path):
    """The same expired session must not be re-sent forever."""
    statuses = ["failed", "failed", "failed"]
    manager, store, calls = make_manager(tmp_path, "u1", status=statuses)
    store.persist("u1", "dead-ssid", "cookie=1", "UA/1.0")

    ok = await manager.reconnect_session()

    assert ok is False
    assert calls["login"] == 1, "reconnect attempts login once, then reports failure"
    assert manager.can_trade is False


# ── 5. fresh session creation ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fresh_session_is_saved_and_validated(tmp_path):
    events = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status="authenticated",
        on_event=lambda e, **kw: events.append(e),
    )

    ok = await manager.create_fresh_session()

    assert ok is True
    assert calls["login"] == 1
    assert AUTH_SESSION_SAVED in events
    assert manager.session_path.exists()
    assert manager.load_session().ssid == "ssid-1"


@pytest.mark.asyncio
async def test_fresh_session_not_confirmed_is_never_reported_usable(tmp_path):
    manager, store, calls = make_manager(tmp_path, "u1", status="unknown")

    ok = await manager.create_fresh_session()

    assert ok is False
    assert manager.can_trade is False


@pytest.mark.asyncio
async def test_login_that_raises_does_not_propagate(tmp_path):
    def boom(_n):
        raise RuntimeError("login exploded")

    manager, store, calls = make_manager(
        tmp_path, "u1", status="unknown", login_side_effect=boom,
    )
    assert await manager.create_fresh_session() is False
    assert manager.can_trade is False


@pytest.mark.asyncio
async def test_login_returning_false_is_reported_as_failure(tmp_path):
    manager, store, calls = make_manager(tmp_path, "u1", login_result=False)
    assert await manager.create_fresh_session() is False
    assert calls["login"] == 1


@pytest.mark.asyncio
async def test_concurrent_fresh_session_is_refused(tmp_path):
    gate = asyncio.Event()

    async def slow_login():
        await gate.wait()
        return True

    manager = QuotexAuthSessionManager("u1", session_dir=tmp_path / "u1",
                                       fresh_login=slow_login)
    first = asyncio.create_task(manager.create_fresh_session())
    await asyncio.sleep(0.05)

    assert await manager.create_fresh_session() is False, "no second concurrent login"

    gate.set()
    await first


# ── 6. reconnect after WS disconnect ────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_disconnect_reuses_the_still_valid_session(tmp_path):
    """A dropped websocket is a network problem, not an expired session."""
    events = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status="authenticated",
        on_event=lambda e, **kw: events.append(e),
    )
    store.persist("u1", "good-ssid", "cookie=1", "UA/1.0")

    assert await manager.reconnect_session() is True

    assert calls["login"] == 0, "a WS drop must not force a new login"
    assert store.rows["u1"]["ssid"] == "good-ssid"
    assert AUTH_RECONNECT_SUCCESS in events


@pytest.mark.asyncio
async def test_inconclusive_validation_never_discards_the_session(tmp_path):
    """'unknown' must not be treated as expired -- that would throw away a
    perfectly good session on a transient network blip."""
    manager, store, _calls = make_manager(tmp_path, "u1", status="unknown")
    await manager.save_session("good-ssid", "cookie=1", "UA/1.0")
    assert manager.session_path.exists(), "precondition: session is on disk"

    result = await manager.validate_session()

    assert result.valid is False
    assert result.unknown is True
    assert manager.session_path.exists(), "session file must survive"
    assert store.rows["u1"]["ssid"] == "good-ssid"
    assert manager.can_trade is False, "but trading still stays off"


# ── 7. session recovery after restart ───────────────────────────────────────

@pytest.mark.asyncio
async def test_restart_reuses_the_session_written_by_the_previous_run(tmp_path):
    """Crash recovery: load -> validate -> reuse. The file alone proves nothing."""
    # One durable store shared across both instances: a real restart loses the
    # manager object but NOT the encrypted broker_sessions row.
    store = FakeStorage()
    manager_a, _s1, _ = make_manager(tmp_path, "u1", status="authenticated",
                                     storage=store)
    await manager_a.save_session("persisted-ssid", "cookie=1", "UA/1.0")

    # Simulate a process restart: a brand new manager instance, same store.
    manager_b, _store2, calls = make_manager(tmp_path, "u1", status="authenticated",
                                             storage=store)

    result = await manager_b.validate_session()

    assert result.valid is True
    assert result.bundle.ssid == "persisted-ssid"
    assert calls["login"] == 0


@pytest.mark.asyncio
async def test_restart_does_not_trust_the_file_when_the_broker_rejects_it(tmp_path):
    store = FakeStorage()
    manager_a, _s1, _ = make_manager(tmp_path, "u1", status="authenticated",
                                     storage=store)
    await manager_a.save_session("persisted-ssid", "cookie=1", "UA/1.0")

    # Restart, but the broker has since killed the session.
    manager_b, _s2, calls = make_manager(tmp_path, "u1",
                                         status=["failed", "failed",
                                                 "authenticated"],
                                         storage=store)
    result = await manager_b.validate_session()
    assert result.valid is False

    assert await manager_b.reconnect_session() is True
    assert calls["login"] == 1
    assert store.rows["u1"]["ssid"] == "ssid-1", "the dead session was replaced"
    # and the on-disk file (shared, per-user) was replaced too
    assert manager_b.load_session().ssid == "ssid-1"


# ── 8. multiple independent users ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_sessions_are_isolated_per_user(tmp_path):
    store = FakeStorage()
    events = {}
    a, _s, calls_a = make_manager(tmp_path, "alice", status="authenticated",
                                  storage=store,
                                  on_event=lambda e, **kw: events.setdefault("a", []).append(e))
    b, _s2, calls_b = make_manager(tmp_path, "bob", status="authenticated",
                                   storage=store,
                                   on_event=lambda e, **kw: events.setdefault("b", []).append(e))

    await a.save_session("alice-ssid", "cookie=a", "UA/1.0")

    assert a.load_session().ssid == "alice-ssid"
    assert b.load_session() is None, "bob must not see alice's session"
    assert a.session_path != b.session_path


@pytest.mark.asyncio
async def test_one_user_expiring_does_not_disturb_another(tmp_path):
    store = FakeStorage()
    alice, _s, calls_a = make_manager(tmp_path, "alice", status="authenticated",
                                      storage=store)
    bob, _s2, calls_b = make_manager(tmp_path, "bob",
                                     status=["failed", "authenticated"],
                                     storage=store)

    await alice.save_session("alice-ssid", "cookie=a", "UA/1.0")
    await bob.save_session("bob-ssid", "cookie=b", "UA/1.0")

    # Bob's session dies and he re-authenticates.
    assert await bob.reconnect_session() is True
    assert calls_b["login"] == 1

    # Alice is untouched.
    assert alice.load_session().ssid == "alice-ssid"
    assert await alice.validate_session() is not None
    assert (await alice.validate_session()).valid is True
    assert calls_a["login"] == 0, "alice never had to log in"


# ── 9. corrupt session file ────────────────────────────────────────────────

def test_corrupt_session_file_is_treated_as_absent(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "quotex_session.json").write_text("{ this is not json", encoding="utf-8")

    manager = QuotexAuthSessionManager("u1", session_dir=d)

    assert manager.load_session() is None


def test_truncated_json_session_file_is_treated_as_absent(tmp_path):
    d = tmp_path / "trunc"
    d.mkdir()
    (d / "quotex_session.json").write_text('{"ssid": "abc"', encoding="utf-8")

    manager = QuotexAuthSessionManager("u1", session_dir=d)

    assert manager.load_session() is None


def test_valid_json_without_an_ssid_is_treated_as_absent(tmp_path):
    d = tmp_path / "nossid"
    d.mkdir()
    (d / "quotex_session.json").write_text('{"cookies": "x"}', encoding="utf-8")

    manager = QuotexAuthSessionManager("u1", session_dir=d)

    assert manager.load_session() is None


@pytest.mark.asyncio
async def test_corrupt_file_triggers_fresh_auth_rather_than_a_crash(tmp_path):
    d = tmp_path / "broken2"
    d.mkdir()
    (d / "quotex_session.json").write_text("garbage", encoding="utf-8")

    calls = {"login": 0}
    produced = {}

    async def login():
        # A real sign-in produces a session; returning True while storing
        # nothing is not something the manager may accept as success.
        calls["login"] += 1
        produced["ssid"] = "fresh-ssid"
        return True

    manager = QuotexAuthSessionManager(
        "u1", session_dir=d, fresh_login=login,
        load=lambda: (SessionBundle(ssid=produced["ssid"], cookies="c=1")
                      if produced else None),
        auth_probe=lambda: "authenticated",
    )
    assert manager.load_session() is None

    assert await manager.reconnect_session() is True
    assert calls["login"] == 1


# ── 10. auth failure without infinite retry ────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_auth_failures_do_not_loop_forever(tmp_path):
    """Each reconnect does exactly one login attempt; the caller's budget --
    not this manager -- decides how many times to ask."""
    manager, store, calls = make_manager(tmp_path, "u1", status="failed",
                                         login_result=True)

    for _ in range(5):
        assert await manager.reconnect_session() is False

    assert calls["login"] == 5, "one login per reconnect call, never an inner loop"
    assert manager.can_trade is False


@pytest.mark.asyncio
async def test_invalidate_clears_both_copies(tmp_path):
    manager, store, _ = make_manager(tmp_path, "u1", status="authenticated")
    await manager.save_session("ssid", "cookie=1", "UA/1.0")
    assert manager.session_path.exists()

    manager.invalidate_session()

    assert not manager.session_path.exists()
    assert manager.load_session() is None
    assert manager.can_trade is False


# ── storage hygiene and secrets ────────────────────────────────────────────

def test_session_file_permissions_are_owner_only(tmp_path):
    d = tmp_path / "perms"
    manager = QuotexAuthSessionManager("u1", session_dir=d)
    manager.save_session_sync = None  # not used; keeps the intent explicit

    asyncio.get_event_loop_policy()  # no-op; sync write below
    # save_session is async; drive it directly
    async def run():
        await manager.save_session("ssid", "cookie=1", "UA/1.0")
    asyncio.run(run())

    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "quotex_session.json").stat().st_mode) == 0o600


def test_secret_values_never_reach_the_repr():
    bundle = SessionBundle(ssid="SUPER-SECRET-SSID", cookies="ssid=SUPER-SECRET")
    text = repr(bundle)
    assert "SUPER-SECRET" not in text
    assert "ssid_len=17" in text


def test_event_payloads_carry_lengths_not_secrets(tmp_path):
    captured = {}
    d = tmp_path / "evt"

    async def run():
        manager = QuotexAuthSessionManager(
            "u1", session_dir=d,
            on_event=lambda e, **kw: captured.setdefault(e, []).append(kw),
        )
        await manager.save_session("SUPER-SECRET-SSID", "ssid=SUPER-SECRET", "UA/1.0")

    asyncio.run(run())

    payload = captured[AUTH_SESSION_SAVED][0]
    assert payload["ssid_len"] == len("SUPER-SECRET-SSID")
    assert "SUPER-SECRET" not in json.dumps(payload)


# ── browser-required detection ────────────────────────────────────────────

def test_browser_required_is_detected_and_not_worked_around():
    manager = QuotexAuthSessionManager("u1")

    for page in ("Just a moment...", "Checking your browser before accessing",
                 "Cloudflare challenge", "Enable JavaScript and cookies",
                 "Ray ID: 8f3a", "Verify you are human"):
        assert manager.requires_browser(page) is True, page

    for page in ("websocket connection rejected", "socket disconnected",
                 "timeout"):
        assert manager.requires_browser(page) is False, page


# ── failure classification (item 7) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_network_failure_does_not_trigger_fresh_auth(tmp_path):
    # A blip that persists: the broker never says the session is dead.
    manager, store, calls = make_manager(tmp_path, "u1",
                                         status="websocket_disconnected")
    await manager.save_session("good-ssid", "cookie=1", "UA/1.0")

    result = await manager.validate_session()
    assert result.valid is False
    assert result.unknown is True
    assert result.expired is False, "a network blip is NOT session expiry"

    # Not confirmed -> not a successful reconnect. But equally: no login.
    assert await manager.reconnect_session() is False
    assert calls["login"] == 0, "a network blip must not burn a login"
    assert manager.can_trade is False
    assert store.rows["u1"]["ssid"] == "good-ssid", "session must survive"


@pytest.mark.asyncio
async def test_blip_that_resolves_reuses_the_session_without_a_login(tmp_path):
    """When the broker confirms again, reconnect succeeds with no new login."""
    manager, store, calls = make_manager(
        tmp_path, "u1", status=["websocket_disconnected", "authenticated"],
    )
    await manager.save_session("good-ssid", "cookie=1", "UA/1.0")

    assert (await manager.validate_session()).unknown is True
    assert await manager.reconnect_session() is True
    assert calls["login"] == 0


@pytest.mark.asyncio
async def test_only_auth_failure_triggers_fresh_auth(tmp_path):
    for non_auth in ("unknown", "websocket_disconnected", "authenticating"):
        manager, store, calls = make_manager(tmp_path, "u1", status=non_auth)
        store.persist("u1", "good-ssid", "cookie=1", "UA/1.0")
        # inconclusive != success, but it must never cost a login either
        assert await manager.reconnect_session() is False
        assert calls["login"] == 0, f"{non_auth} wrongly triggered a login"
        assert store.rows["u1"]["ssid"] == "good-ssid", "session must survive"

    manager, store, calls = make_manager(tmp_path, "u1",
                                         status=["failed", "authenticated"])
    store.persist("u1", "dead-ssid", "cookie=1", "UA/1.0")
    assert await manager.reconnect_session() is True
    assert calls["login"] == 1, "an auth rejection must trigger fresh auth"


# ── ValidationResult semantics ────────────────────────────────────────────

def test_validation_result_never_exposes_a_bundle_when_invalid():
    r = ValidationResult(valid=False, reason="expired",
                         bundle=SessionBundle(ssid="x", cookies="y"))
    assert r.bundle is None
    assert r.expired is False
    assert r.rejected is False
    assert r.browser_required is False


def test_bundle_usability_requires_ssid_and_cookies():
    assert SessionBundle(ssid="a", cookies="b").is_usable() is True
    assert SessionBundle(ssid="a", cookies=None).is_usable() is False
    assert SessionBundle(ssid=None, cookies="b").is_usable() is False
    assert SessionBundle(ssid="a", cookies="b", expired=True).is_usable() is False


# ── OTP / emailed PIN is observable (item 6/11) ───────────────────────────
#
# The broker's own `get_auth_status()` returns only
# authenticated | authenticating | not_authenticated | failed | unknown. It can
# never say "a PIN is pending", because OTP is a blocking interaction inside the
# login call, not a websocket state. So the orchestrator's probe reports OTP
# from `state.otp_required`. Without that, a login parked on a PIN would look
# like an ordinary auth failure and invite another login on top of one already
# waiting for the user.

@pytest.mark.asyncio
async def test_pending_pin_is_reported_and_blocks_further_logins(tmp_path):
    events = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status="otp_required",
        on_event=lambda e, **kw: events.append(e),
    )
    await manager.save_session("pending-ssid", "cookie=1", "UA/1.0")

    result = await manager.validate_session()

    assert result.otp_required is True
    assert result.valid is False
    assert result.expired is False, "a pending PIN is NOT session expiry"
    assert AUTH_OTP_REQUIRED in events

    # reconnect must stop, not start a second login over the pending one
    assert await manager.reconnect_session() is False
    assert calls["login"] == 0, "must not log in over a pending PIN"
    assert AUTH_FRESH_SESSION_STARTED not in events
    assert manager.can_trade is False


@pytest.mark.asyncio
async def test_pending_pin_does_not_discard_the_session(tmp_path):
    manager, store, calls = make_manager(tmp_path, "u1", status="otp_required")
    await manager.save_session("pending-ssid", "cookie=1", "UA/1.0")

    assert await manager.reconnect_session() is False
    assert store.rows["u1"]["ssid"] == "pending-ssid", "session must survive"
    assert manager.session_path.exists()


def test_orchestrator_probe_reports_otp_from_state_not_the_broker():
    """The REAL orchestrator probe, with a negative control.

    `get_auth_status()` cannot express OTP, so the probe must read the flag.
    Stubbing only the inputs the method touches -- the branch under test is the
    real one.
    """
    import app.orchestrator as orch

    def probe(otp_required, auth_status):
        stub = SimpleNamespace(
            state=SimpleNamespace(otp_required=otp_required),
            provider=SimpleNamespace(get_auth_status=lambda: auth_status),
        )
        return orch.Orchestrator._probe_auth_status(stub)

    assert probe(True, "authenticating") == "otp_required"
    assert probe(False, "failed") == "failed"
    assert probe(False, "authenticated") == "authenticated"

    # a provider with no auth introspection at all must not look healthy
    bare = SimpleNamespace(state=SimpleNamespace(otp_required=False),
                           provider=SimpleNamespace())
    assert orch.Orchestrator._probe_auth_status(bare) == "unknown"


# ── interactive challenge is reported, never worked around (items 8/11) ────
#
# Two dead paths were found here and both are now live:
#
#  1. `_classify` only tested challenge *page content* via requires_browser(),
#     but the probe reports a short status token. No marker can match the
#     literal "browser_required", so it fell through to `unknown` and was
#     mistaken for a transient network blip.
#  2. Nothing produced that token at all: market.py never inspected a failed
#     login response, so an HTTP 200 challenge page was indistinguishable from
#     a wrong password.
#
# Detection only. No test here asserts that a challenge is bypassed.

@pytest.mark.asyncio
async def test_interactive_challenge_stops_instead_of_retrying(tmp_path):
    events = []
    manager, store, calls = make_manager(
        tmp_path, "u1", status="browser_required",
        on_event=lambda e, **kw: events.append(e),
    )
    await manager.save_session("blocked-ssid", "cookie=1", "UA/1.0")

    result = await manager.validate_session()

    assert result.browser_required is True
    assert result.valid is False
    assert result.unknown is False, "a challenge is a definite verdict, not a blip"
    assert result.expired is False, "a challenge is NOT session expiry"
    assert AUTH_BROWSER_REQUIRED in events

    assert await manager.reconnect_session() is False
    assert calls["login"] == 0, "retrying cannot succeed; do not burn attempts"
    assert AUTH_FRESH_SESSION_STARTED not in events


@pytest.mark.asyncio
async def test_interactive_challenge_preserves_the_session(tmp_path):
    manager, store, calls = make_manager(tmp_path, "u1", status="browser_required")
    await manager.save_session("blocked-ssid", "cookie=1", "UA/1.0")

    assert await manager.reconnect_session() is False
    assert store.rows["u1"]["ssid"] == "blocked-ssid", "session must survive"
    assert manager.session_path.exists()
    assert manager.can_trade is False


def test_classify_recognises_the_literal_browser_required_token():
    """The regression: a status token is not challenge page content."""
    from app.broker_auth import QuotexAuthSessionManager as M

    assert M.requires_browser("browser_required") is False, \
        "no marker matches the token -- this is why the literal is needed"
    bundle = SessionBundle(ssid="s", cookies="c")
    result = M("u1")._classify("browser_required", bundle)
    assert result.browser_required is True
    assert result.unknown is False

    # page-content detection must keep working too
    assert M("u1")._classify("Just a moment...", bundle).browser_required is True
    assert M("u1")._classify("websocket disconnected", bundle).unknown is True


def test_provider_block_reason_reaches_the_probe():
    """The REAL orchestrator probe, with a negative control."""
    import app.orchestrator as orch

    def probe(block_reason, auth_status="failed", otp=False):
        stub = SimpleNamespace(
            state=SimpleNamespace(otp_required=otp),
            provider=SimpleNamespace(
                get_auth_status=lambda: auth_status,
                get_login_block_reason=lambda: block_reason,
            ),
        )
        return orch.Orchestrator._probe_auth_status(stub)

    assert probe("browser_required") == "browser_required"
    assert probe(None) == "failed"
    # a pending PIN still outranks a stale block reason
    assert probe("browser_required", auth_status="failed", otp=True) == "otp_required"

    # negative control: without the block hook a challenge reads as a plain
    # auth failure and the manager would keep retrying logins
    stub = SimpleNamespace(state=SimpleNamespace(otp_required=False),
                           provider=SimpleNamespace(get_auth_status=lambda: "failed"))
    assert orch.Orchestrator._probe_auth_status(stub) == "failed"


# ── structured events actually leave the manager (item 11) ─────────────────
#
# The manager emitted 14 AUTH_* events but the orchestrator passed no on_event,
# so every one of them died in an in-memory ring: nothing reached the log and
# nothing reached the UI. These tests call the REAL handler.

def _auth_stub(monkeypatch, orch, logged, sent):
    """A stub carrying exactly what _on_auth_event touches."""
    class Spy:
        def __getattr__(self, lvl):
            return lambda msg, *a, **k: logged.append(
                (lvl, msg % a if a else msg))

    class Hub:
        async def broadcast(self, uid, ev, data):
            sent.append((uid, ev, data))

    monkeypatch.setattr(orch, "logger", Spy())
    monkeypatch.setattr(orch, "hub", Hub())
    stub = SimpleNamespace(
        user_id="u1", logger=orch.logger,
        _AUTH_SECRET_KEYS=orch.Orchestrator._AUTH_SECRET_KEYS,
        _AUTH_UI_EVENTS=orch.Orchestrator._AUTH_UI_EVENTS,
        _AUTH_FAILURE_EVENTS=orch.Orchestrator._AUTH_FAILURE_EVENTS,
    )
    stub._scrub_auth_payload = (
        lambda payload: orch.Orchestrator._scrub_auth_payload(stub, payload))
    return stub


def test_secret_keys_are_replaced_by_lengths():
    import app.orchestrator as orch
    import json as _json

    logged, sent = [], []
    # no monkeypatch fixture needed: the scrub touches no module globals
    class _NoLog:
        def __getattr__(self, _): return lambda *a, **k: None
    stub = SimpleNamespace(
        user_id="u1", logger=_NoLog(),
        _AUTH_SECRET_KEYS=orch.Orchestrator._AUTH_SECRET_KEYS,
        _AUTH_UI_EVENTS=orch.Orchestrator._AUTH_UI_EVENTS,
        _AUTH_FAILURE_EVENTS=orch.Orchestrator._AUTH_FAILURE_EVENTS,
    )
    stub._scrub_auth_payload = (
        lambda payload: orch.Orchestrator._scrub_auth_payload(stub, payload))

    clean = orch.Orchestrator._scrub_auth_payload(stub, {
        "ssid": "SUPER-SECRET-SSID", "cookies": "ssid=SUPERSECRET",
        "token": "T0KEN", "password": "hunter2", "reason": "ok",
    })

    blob = _json.dumps(clean, default=str)
    for leak in ("SUPER-SECRET-SSID", "SUPERSECRET", "T0KEN", "hunter2"):
        assert leak not in blob, f"secret leaked: {leak}"
    assert clean["ssid_len"] == len("SUPER-SECRET-SSID")
    assert clean["reason"] == "ok", "non-secret fields must survive"


@pytest.mark.asyncio
async def test_events_are_logged_and_broadcast(monkeypatch):
    import app.orchestrator as orch

    logged, sent = [], []
    stub = _auth_stub(monkeypatch, orch, logged, sent)

    orch.Orchestrator._on_auth_event(stub, "AUTH_SESSION_VALID",
                                     reason="broker confirmed", ssid_len=15)
    orch.Orchestrator._on_auth_event(stub, "AUTH_BROWSER_REQUIRED",
                                     reason="interactive challenge")
    orch.Orchestrator._on_auth_event(stub, "AUTH_SESSION_LOAD", found=True)
    for _ in range(3):
        await asyncio.sleep(0)

    assert any("AUTH_SESSION_VALID" in m for _, m in logged)
    assert any("AUTH_BROWSER_REQUIRED" in m for _, m in logged)

    levels = {m.split()[1]: lvl for lvl, m in logged
              if m.startswith("[auth] AUTH_")}
    assert levels["AUTH_SESSION_VALID"] == "info"
    assert levels["AUTH_BROWSER_REQUIRED"] == "warning", \
        "a failure-class event must log at warning"
    assert levels["AUTH_SESSION_LOAD"] == "debug", \
        "the high-frequency event must stay quiet"

    pushed = {d["event"] for _, _, d in sent}
    assert pushed == {"AUTH_SESSION_VALID", "AUTH_BROWSER_REQUIRED"}
    assert "AUTH_SESSION_LOAD" not in pushed, \
        "it fires on every validation; it must not reach the UI"


def test_event_handler_is_safe_with_no_running_loop(monkeypatch):
    import app.orchestrator as orch

    logged, sent = [], []
    stub = _auth_stub(monkeypatch, orch, logged, sent)

    # called outside any event loop: must log, must not raise
    orch.Orchestrator._on_auth_event(stub, "AUTH_RECONNECT_FAILED", reason="x")

    assert len(logged) == 1
    assert sent == []


def test_orchestrator_actually_wires_on_event():
    """Guard against the regression: a manager with no event sink."""
    import inspect
    import app.orchestrator as orch

    src = inspect.getsource(orch.Orchestrator.__init__)
    assert "on_event=self._on_auth_event" in src, \
        "events would be trapped inside the manager again"


# ── a cleared store must not be overridden by a leftover file (item 10) ────
#
# load_session used to fall back to the on-disk file whenever the durable store
# returned nothing. Updating credentials in the UI calls clear_session_token(),
# which NULLs the stored ssid/cookies precisely so that new credentials mean a
# genuinely fresh login -- but the file survived, so the previous session came
# straight back.

@pytest.mark.asyncio
async def test_cleared_store_is_not_overridden_by_a_leftover_file(tmp_path):
    store = FakeStorage()
    d = tmp_path / "u1"
    manager = QuotexAuthSessionManager(
        "u1", session_dir=d,
        persist=lambda ssid, ck, ua: store.persist("u1", ssid, ck, ua),
        load=lambda: store.load("u1"),
        discard=lambda: store.discard("u1"),
        auth_probe=lambda: "authenticated",
    )
    await manager.save_session("old-ssid", "cookie=1", "UA/1.0")
    assert manager.session_path.exists(), "precondition: both copies exist"

    # The user changes credentials; the store forgets the session on purpose.
    store.discard("u1")

    assert manager.load_session() is None, "a cleared store must mean no session"
    assert not manager.session_path.exists(), "the stale file must be removed"
    assert manager.can_trade is False


@pytest.mark.asyncio
async def test_file_is_the_storage_when_no_store_is_configured(tmp_path):
    """Without a durable store the file IS the storage -- the corrupt-file and
    restart tests depend on that, and this fix must not break it."""
    d = tmp_path / "fileonly"
    manager = QuotexAuthSessionManager("u1", session_dir=d)
    await manager.save_session("file-ssid", "cookie=1", "UA/1.0")

    # a brand new instance must still find it
    reopened = QuotexAuthSessionManager("u1", session_dir=d)
    bundle = reopened.load_session()
    assert bundle is not None and bundle.ssid == "file-ssid"


@pytest.mark.asyncio
async def test_store_still_wins_over_the_file_when_both_exist(tmp_path):
    store = FakeStorage()
    d = tmp_path / "both"
    manager = QuotexAuthSessionManager(
        "u1", session_dir=d,
        persist=lambda ssid, ck, ua: store.persist("u1", ssid, ck, ua),
        load=lambda: store.load("u1"),
        auth_probe=lambda: "authenticated",
    )
    await manager.save_session("store-ssid", "cookie=1", "UA/1.0")

    # a stale file must never shadow the store's answer
    (d / "quotex_session.json").write_text(
        json.dumps({"ssid": "stale-file-ssid", "cookies": "c=1"}), encoding="utf-8")

    bundle = manager.load_session()
    assert bundle is not None
    assert bundle.ssid == "store-ssid", "the store is authoritative"
