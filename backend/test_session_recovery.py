"""Fresh session recovery: auth lifecycle vs transport, gates, budgets.

The behaviours that matter most here are the negative ones -- that a dropped
socket is NOT mistaken for session expiry, that a revoked session does not get
retried forever, that trading never happens on unconfirmed auth, and that two
authentications can never run at once.
"""

import pytest

from app.engine.session_recovery import (
    RecoveryPolicy,
    SessionEvent,
    SessionRecoveryStateMachine,
    SessionSnapshot,
    SessionState,
    TRADING_ALLOWED_STATES,
)


class FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sm(clock):
    return SessionRecoveryStateMachine(
        policy=RecoveryPolicy(max_auth_attempts=2), clock=clock
    )


def _authenticate(sm):
    """Bring a machine to a confirmed, tradeable CONNECTED."""
    sm.handle(SessionEvent.AUTH_CONFIRMED, "broker confirmed")


# ────────────────────────────────────────────────────────────────────────────
# transport path vs authentication path
# ────────────────────────────────────────────────────────────────────────────

def test_starts_stopped_and_never_tradeable(sm):
    assert sm.state is SessionState.STOPPED
    assert sm.trading_allowed is False


def test_websocket_connect_alone_does_not_grant_trading(sm):
    """The core defect this module exists to fix: a socket is not a session."""
    sm.handle(SessionEvent.WS_CONNECTED, "socket up")
    assert sm.state is SessionState.WS_DISCONNECTED
    assert sm.trading_allowed is False


def test_auth_confirmation_is_what_grants_trading(sm):
    sm.handle(SessionEvent.WS_CONNECTED, "socket up")
    sm.handle(SessionEvent.AUTH_CONFIRMED, "s_authorization")
    assert sm.state is SessionState.CONNECTED
    assert sm.trading_allowed is True


def test_ws_drop_is_not_treated_as_session_expiry(sm):
    """Requirement 2: a WebSocket disconnect must NOT mean the session died."""
    _authenticate(sm)
    sm.handle(SessionEvent.WS_DISCONNECTED, "network blip")

    assert sm.state is SessionState.WS_DISCONNECTED
    assert sm.auth_confirmed is True, "the confirmation must survive a socket drop"
    assert sm.state is not SessionState.SESSION_EXPIRED


def test_transport_cycle_returns_to_connected(sm):
    _authenticate(sm)
    sm.handle(SessionEvent.WS_DISCONNECTED, "blip")
    sm.handle(SessionEvent.RECONNECT_STARTED, "retry")
    assert sm.state is SessionState.RECONNECTING
    sm.handle(SessionEvent.RECONNECT_SUCCEEDED, "back up")
    assert sm.state is SessionState.CONNECTED
    assert sm.trading_allowed is True


def test_reconnect_failed_falls_back_to_disconnected(sm):
    _authenticate(sm)
    sm.handle(SessionEvent.RECONNECT_STARTED, "retry")
    sm.handle(SessionEvent.RECONNECT_FAILED, "nope")
    assert sm.state is SessionState.WS_DISCONNECTED


# ────────────────────────────────────────────────────────────────────────────
# session expiry path
# ────────────────────────────────────────────────────────────────────────────

def test_explicit_rejection_enters_the_auth_path(sm):
    _authenticate(sm)
    st = sm.handle(SessionEvent.AUTH_REJECTED, "authorization/reject")
    assert st is SessionState.FRESH_SESSION_REQUIRED
    assert sm.needs_fresh_session is True
    assert sm.auth_confirmed is False
    assert sm.trading_allowed is False


def test_rejection_stops_trading_immediately(sm):
    _authenticate(sm)
    assert sm.trading_allowed is True
    sm.handle(SessionEvent.AUTH_REJECTED, "revoked")
    assert sm.trading_allowed is False


def test_repeat_rejection_is_idempotent(sm):
    """The broker can emit authorization/reject more than once for one session.
    Re-running the transition each time would churn history and spam logs."""
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "reject")
    before = len(sm.snapshot().history)

    sm.handle(SessionEvent.AUTH_REJECTED, "reject again")
    sm.handle(SessionEvent.AUTH_REJECTED, "and again")

    assert sm.state is SessionState.FRESH_SESSION_REQUIRED
    assert len(sm.snapshot().history) == before


def test_ws_noise_cannot_drag_the_auth_path_back_to_reconnecting(sm):
    """Once on the auth path, transport events must not hijack the lifecycle."""
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")
    sm.handle(SessionEvent.WS_DISCONNECTED, "socket closed too")
    assert sm.state is SessionState.FRESH_SESSION_REQUIRED

    sm.handle(SessionEvent.RECONNECT_STARTED, "stale reconnect")
    assert sm.state is SessionState.FRESH_SESSION_REQUIRED


def test_stale_auth_confirmation_stops_trading(sm, clock):
    """Confirmed once at startup, silently revoked hours later: the
    re-confirmation window must catch that."""
    _authenticate(sm)
    assert sm.trading_allowed is True

    clock.advance(400)          # past auth_reconfirm_after_seconds (300)
    assert sm.trading_allowed is False


def test_reconfirming_restores_trading(sm, clock):
    _authenticate(sm)
    clock.advance(400)
    assert sm.trading_allowed is False
    sm.handle(SessionEvent.AUTH_CONFIRMED, "re-confirmed")
    assert sm.trading_allowed is True


# ────────────────────────────────────────────────────────────────────────────
# Session A -> Session B lifecycle
# ────────────────────────────────────────────────────────────────────────────

def test_new_session_is_not_trusted_until_the_broker_confirms_it(sm):
    """Requirement: resume normal operation only after auth is POSITIVELY
    confirmed. Obtaining a token is not the same as being authorized."""
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")
    assert sm.begin_auth_attempt() is True
    sm.handle(SessionEvent.FRESH_SESSION_STARTED, "login")
    assert sm.state is SessionState.AUTHENTICATING
    assert sm.trading_allowed is False

    sm.handle(SessionEvent.FRESH_SESSION_SUCCEEDED, "new ssid obtained")
    assert sm.state is SessionState.NEW_SESSION_CREATED
    assert sm.trading_allowed is False, "Session B is not trusted yet"

    sm.handle(SessionEvent.AUTH_CONFIRMED, "s_authorization on Session B")
    assert sm.state is SessionState.CONNECTED
    assert sm.trading_allowed is True


def test_successful_recovery_resets_the_attempt_budget(sm):
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")
    sm.begin_auth_attempt()
    sm.handle(SessionEvent.FRESH_SESSION_STARTED, "login")
    sm.handle(SessionEvent.FRESH_SESSION_SUCCEEDED, "ok")
    sm.handle(SessionEvent.AUTH_CONFIRMED, "confirmed")
    assert sm.auth_attempts == 0


def test_otp_satisfied_does_not_grant_trading(sm):
    """A human entered a code; the broker has not yet accepted it."""
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")
    sm.handle(SessionEvent.OTP_REQUIRED, "PIN required")
    assert sm.state is SessionState.REAUTH_REQUIRED
    assert sm.otp_required is True
    assert sm.trading_allowed is False

    sm.handle(SessionEvent.OTP_SATISFIED, "human entered code")
    assert sm.state is SessionState.AUTHENTICATING
    assert sm.trading_allowed is False

    sm.handle(SessionEvent.AUTH_CONFIRMED, "broker accepted")
    assert sm.trading_allowed is True


# ────────────────────────────────────────────────────────────────────────────
# dangerous-behaviour guards
# ────────────────────────────────────────────────────────────────────────────

def test_concurrent_authentication_is_refused(sm):
    """No concurrent auth attempts, no duplicate sessions fighting."""
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")

    assert sm.begin_auth_attempt() is True
    assert sm.begin_auth_attempt() is False, "second concurrent login must be refused"
    assert sm.auth_in_flight is True


def test_budget_is_enforced_over_realistic_attempts(sm):
    """No infinite login loops: a bounded number of genuine attempts, then stop."""
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")

    # attempt 1
    assert sm.begin_auth_attempt() is True
    sm.handle(SessionEvent.FRESH_SESSION_STARTED, "login")
    sm.handle(SessionEvent.FRESH_SESSION_FAILED, "failed")
    assert sm.state is SessionState.FRESH_SESSION_REQUIRED
    assert sm.auth_attempts == 1

    # attempt 2 (the cap)
    assert sm.begin_auth_attempt() is True
    sm.handle(SessionEvent.FRESH_SESSION_STARTED, "login")
    sm.handle(SessionEvent.FRESH_SESSION_FAILED, "failed")
    assert sm.state is SessionState.AUTH_EXHAUSTED
    assert sm.is_exhausted is True

    # no further logins, and trading stays off
    assert sm.begin_auth_attempt() is False
    assert sm.trading_allowed is False


def test_exhausted_state_does_not_trade(sm):
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")
    for _ in range(2):
        sm.begin_auth_attempt()
        sm.handle(SessionEvent.FRESH_SESSION_STARTED, "l")
        sm.handle(SessionEvent.FRESH_SESSION_FAILED, "f")
    assert sm.state is SessionState.AUTH_EXHAUSTED
    assert sm.trading_allowed is False


def test_reset_auth_budget_after_human_intervention(sm):
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")
    for _ in range(2):
        sm.begin_auth_attempt()
        sm.handle(SessionEvent.FRESH_SESSION_STARTED, "l")
        sm.handle(SessionEvent.FRESH_SESSION_FAILED, "f")
    assert sm.state is SessionState.AUTH_EXHAUSTED

    sm.reset_auth_budget("human signed in again")
    assert sm.auth_attempts == 0
    assert sm.state is SessionState.WS_DISCONNECTED
    assert sm.begin_auth_attempt() is True


def test_backoff_is_exponential_and_capped(clock):
    sm = SessionRecoveryStateMachine(
        policy=RecoveryPolicy(
            max_auth_attempts=10,
            auth_backoff_base_seconds=5.0,
            auth_backoff_max_seconds=40.0,
        ),
        clock=clock,
    )
    seq = []
    for _ in range(5):
        sm.begin_auth_attempt()
        seq.append(sm.auth_backoff_seconds())
        sm._auth_attempt_in_flight = False
    assert seq == [5.0, 10.0, 20.0, 40.0, 40.0], seq


def test_stop_event_clears_in_flight_flags(sm):
    _authenticate(sm)
    sm.handle(SessionEvent.AUTH_REJECTED, "expired")
    sm.begin_auth_attempt()
    sm.handle(SessionEvent.FRESH_SESSION_STARTED, "login")

    sm.handle(SessionEvent.STOP, "shutdown")
    assert sm.state is SessionState.STOPPED
    assert sm.auth_in_flight is False
    assert sm.reconnect_in_flight is False
    assert sm.trading_allowed is False


def test_only_connected_is_ever_tradeable():
    assert TRADING_ALLOWED_STATES == frozenset({SessionState.CONNECTED})


# ────────────────────────────────────────────────────────────────────────────
# robustness / observability
# ────────────────────────────────────────────────────────────────────────────

def test_transition_callback_failure_cannot_break_a_transition(clock):
    def boom(old, new, reason):
        raise RuntimeError("observer down")

    sm = SessionRecoveryStateMachine(clock=clock, on_transition=boom)
    sm.handle(SessionEvent.AUTH_CONFIRMED, "ok")     # must not raise
    assert sm.state is SessionState.CONNECTED


def test_history_is_bounded(clock):
    sm = SessionRecoveryStateMachine(clock=clock, max_history=5)
    for i in range(50):
        sm.handle(SessionEvent.AUTH_CONFIRMED, f"c{i}")
        sm.handle(SessionEvent.WS_DISCONNECTED, f"d{i}")
    assert len(sm.snapshot().history) <= 5


def test_snapshot_is_json_serializable(sm):
    import json
    _authenticate(sm)
    payload = sm.snapshot().as_dict()
    json.dumps(payload)
    assert payload["state"] == "connected"
    assert payload["trading_allowed"] is True
    assert payload["auth_confirmed"] is True


def test_snapshot_reports_auth_age(sm, clock):
    _authenticate(sm)
    clock.advance(12.5)
    assert sm.snapshot().auth_age_seconds == pytest.approx(12.5)


# ────────────────────────────────────────────────────────────────────────────
# provider auth introspection
# ────────────────────────────────────────────────────────────────────────────

class _FakeState:
    def __init__(self, a): self.auth_status = a


class _FakeApi:
    def __init__(self, a): self.state = _FakeState(a)


class _FakeClient:
    def __init__(self, a=None, has_api=True):
        self.api = _FakeApi(a) if has_api else None


def _provider():
    from app.services.market import PyQuotexProvider
    return PyQuotexProvider.__new__(PyQuotexProvider)


def test_provider_maps_every_vendor_auth_status():
    from pyquotex.global_value import AuthStatus

    p = _provider()
    expected = {
        AuthStatus.AUTHENTICATED: "authenticated",
        AuthStatus.AUTHENTICATING: "authenticating",
        AuthStatus.FAILED: "failed",
        AuthStatus.NOT_AUTHENTICATED: "not_authenticated",
    }
    for status, want in expected.items():
        p._client = _FakeClient(status)
        assert p.get_auth_status() == want


def test_only_positive_confirmation_counts_as_authenticated():
    from pyquotex.global_value import AuthStatus

    p = _provider()
    p._client = _FakeClient(AuthStatus.AUTHENTICATED)
    assert p.is_authenticated() is True
    assert p.session_expired() is False

    p._client = _FakeClient(AuthStatus.FAILED)
    assert p.is_authenticated() is False
    assert p.session_expired() is True

    p._client = _FakeClient(AuthStatus.AUTHENTICATING)
    assert p.is_authenticated() is False
    assert p.session_expired() is False


def test_unknown_is_never_treated_as_healthy():
    """When introspection cannot tell, trading must be considered unsafe."""
    p = _provider()
    p._client = _FakeClient(has_api=False)
    assert p.get_auth_status() == "unknown"
    assert p.is_authenticated() is False
    assert p.session_expired() is False


def test_missing_auth_status_attribute_is_unknown():
    class _NoStatus:
        pass

    class _Api:
        state = _NoStatus()

    class _Client:
        api = _Api()

    p = _provider()
    p._client = _Client()
    assert p.get_auth_status() == "unknown"


def test_introspection_exception_is_not_read_as_healthy():
    class _Boom:
        @property
        def api(self):
            raise RuntimeError("boom")

    p = _provider()
    p._client = _Boom()
    assert p.get_auth_status() == "unknown"
    assert p.is_authenticated() is False


def test_refresh_session_is_async():
    import inspect
    assert inspect.iscoroutinefunction(_provider().refresh_session)


# ────────────────────────────────────────────────────────────────────────────
# published state
# ────────────────────────────────────────────────────────────────────────────

def test_engine_state_exposes_session_state_with_a_safe_default():
    from app.schemas import EngineState

    e = EngineState(provider="quotex")
    assert e.session_state == "stopped"
    e.session_state = SessionState.SESSION_EXPIRED.value
    assert e.session_state == "session_expired"


# ────────────────────────────────────────────────────────────────────────────
# orchestrator observation: the REAL _observe_session_state, stubbed inputs
# ────────────────────────────────────────────────────────────────────────────
# These call the shipped method directly rather than re-implementing its
# branch. They exist because the first version of the auth gate silently
# blocked ALL trading on providers that expose no auth introspection
# (Binance/Bybit) -- the full suite did not catch it.

from types import SimpleNamespace


class _NoAuthProvider:
    """A provider with no get_auth_status (e.g. Binance/Bybit)."""
    connected = True
    name = "binance"


class _IntrospectingProvider:
    connected = True
    name = "quotex"

    def __init__(self, status: str) -> None:
        self._status = status

    def get_auth_status(self) -> str:
        return self._status


def _observe(provider):
    from app.orchestrator import Orchestrator

    fake = SimpleNamespace(
        provider=provider,
        session_recovery=SessionRecoveryStateMachine(policy=RecoveryPolicy()),
    )
    Orchestrator._observe_session_state(fake)
    return fake.session_recovery


def test_provider_without_auth_introspection_is_not_blocked():
    """Regression: connectivity must map through, or every non-Quotex
    provider would have all trading permanently disabled."""
    sm = _observe(_NoAuthProvider())
    assert sm.state is SessionState.CONNECTED
    assert sm.trading_allowed is True


def test_provider_without_introspection_disconnected_is_not_tradeable():
    sm = _observe(SimpleNamespace(connected=False, name="binance"))
    assert sm.state is SessionState.WS_DISCONNECTED
    assert sm.trading_allowed is False


def test_quotex_unknown_status_does_not_grant_trading():
    """'unknown' means we cannot tell -- that is not a green light."""
    sm = _observe(_IntrospectingProvider("unknown"))
    assert sm.trading_allowed is False


def test_quotex_authenticated_grants_trading():
    sm = _observe(_IntrospectingProvider("authenticated"))
    assert sm.state is SessionState.CONNECTED
    assert sm.trading_allowed is True


def test_quotex_failed_enters_the_auth_path_and_blocks_trading():
    sm = _observe(_IntrospectingProvider("failed"))
    assert sm.state is SessionState.FRESH_SESSION_REQUIRED
    assert sm.trading_allowed is False


def test_observation_never_raises_on_a_broken_provider():
    class _Boom:
        connected = True
        name = "broken"

        def get_auth_status(self):
            raise RuntimeError("introspection blew up")

    sm = _observe(_Boom())          # must not raise
    assert sm.trading_allowed is False
