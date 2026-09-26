"""Session-lifecycle state machine: transport failures vs real session expiry.

Why this exists
---------------
The broker connection has TWO independent failure modes that the previous code
conflated:

  * the WebSocket dropped, but the authenticated session is still valid
    -> a plain reconnect is correct and cheap;
  * the broker explicitly rejected the session (`authorization/reject`)
    -> reconnecting is useless, and retrying the same dead credential
       forever is actively harmful. It produces an endless reconnect loop
       that re-sends a token the broker has already revoked, while the
       system still reports itself "connected".

The vendor already distinguishes these -- `pyquotex/api.py:432-441` sets
`state.auth_status = AuthStatus.FAILED` on `authorization/reject` -- but the
backend never consulted it for any decision. This module is the missing
decision layer.

It is deliberately pure logic: no I/O, no asyncio, no broker calls. The
orchestrator feeds it observations and asks it what to do. That keeps the
dangerous invariants (never trade on uncertain auth, never run two
authentications at once, never retry forever) in one place that can be unit
tested without a network.

What this does NOT do
---------------------
It does not replace the session manager, the reconnect loop, the trading
engine or any strategy. It does not bypass, defeat or automate around OTP:
when the broker demands a PIN, the only honest states are REAUTH_REQUIRED /
OTP_REQUIRED, and trading stays paused until a human completes it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    """The lifecycle states, kept distinct on purpose.

    Transport path:
        CONNECTED -> WS_DISCONNECTED -> RECONNECTING -> CONNECTED
    Authentication path:
        CONNECTED -> SESSION_EXPIRED -> FRESH_SESSION_REQUIRED
                  -> AUTHENTICATING -> NEW_SESSION_CREATED -> CONNECTED
    Dead ends that must pause trading rather than loop:
        REAUTH_REQUIRED (broker wants a PIN)  /  AUTH_EXHAUSTED (retries used up)
    """

    CONNECTED = "connected"
    WS_DISCONNECTED = "ws_disconnected"
    RECONNECTING = "reconnecting"
    SESSION_EXPIRED = "session_expired"
    FRESH_SESSION_REQUIRED = "fresh_session_required"
    AUTHENTICATING = "authenticating"
    NEW_SESSION_CREATED = "new_session_created"
    REAUTH_REQUIRED = "reauth_required"
    AUTH_EXHAUSTED = "auth_exhausted"
    STOPPED = "stopped"


class SessionEvent(str, Enum):
    WS_CONNECTED = "ws_connected"
    WS_DISCONNECTED = "ws_disconnected"
    RECONNECT_STARTED = "reconnect_started"
    RECONNECT_SUCCEEDED = "reconnect_succeeded"
    RECONNECT_FAILED = "reconnect_failed"
    #: The broker positively confirmed authentication (vendor `s_authorization`).
    AUTH_CONFIRMED = "auth_confirmed"
    #: The broker explicitly rejected the session (`authorization/reject`).
    AUTH_REJECTED = "auth_rejected"
    FRESH_SESSION_STARTED = "fresh_session_started"
    FRESH_SESSION_SUCCEEDED = "fresh_session_succeeded"
    FRESH_SESSION_FAILED = "fresh_session_failed"
    OTP_REQUIRED = "otp_required"
    OTP_SATISFIED = "otp_satisfied"
    STOP = "stop"


#: The ONLY state in which placing a trade is permitted. Everything else means
#: authentication is unconfirmed, in flight, or known-bad.
TRADING_ALLOWED_STATES = frozenset({SessionState.CONNECTED})


@dataclass
class RecoveryPolicy:
    """Bounds on the authentication-recovery attempt loop.

    Separate from the transport reconnect backoff on purpose: a reconnect is
    cheap and worth retrying for a long time, while a fresh login hits the
    broker's sign-in endpoint and must be capped hard.
    """

    max_auth_attempts: int = 3
    auth_backoff_base_seconds: float = 5.0
    auth_backoff_max_seconds: float = 300.0
    #: How long a positive auth confirmation stays trustworthy before we want
    #: to see it again. Guards against "confirmed once at startup, silently
    #: revoked hours later, nobody noticed".
    auth_reconfirm_after_seconds: float = 300.0


@dataclass
class SessionSnapshot:
    """Serializable view for logs, /api endpoints and the UI."""

    state: str
    auth_confirmed: bool
    auth_age_seconds: Optional[float]
    auth_attempts: int
    otp_required: bool
    trading_allowed: bool
    last_transition_at: float
    last_reason: str
    history: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "auth_confirmed": self.auth_confirmed,
            "auth_age_seconds": self.auth_age_seconds,
            "auth_attempts": self.auth_attempts,
            "otp_required": self.otp_required,
            "trading_allowed": self.trading_allowed,
            "last_transition_at": self.last_transition_at,
            "last_reason": self.last_reason,
            "history": list(self.history),
        }


class SessionRecoveryStateMachine:
    """Tracks authentication separately from WebSocket connectivity.

    One instance per orchestrator (per user) -- no shared global state, so two
    users' sessions can never influence each other.
    """

    def __init__(
        self,
        *,
        policy: Optional[RecoveryPolicy] = None,
        clock: Optional[Callable[[], float]] = None,
        on_transition: Optional[Callable[[SessionState, SessionState, str], None]] = None,
        max_history: int = 50,
    ) -> None:
        self.policy = policy or RecoveryPolicy()
        self._clock = clock or time.time
        self._on_transition = on_transition
        self._max_history = int(max_history)

        self.state: SessionState = SessionState.STOPPED
        self._auth_confirmed: bool = False
        self._auth_confirmed_at: Optional[float] = None
        self._auth_attempts: int = 0
        self._auth_attempt_in_flight: bool = False
        self._reconnect_in_flight: bool = False
        self._otp_required: bool = False
        self._last_reason: str = ""
        self._last_transition_at: float = self._clock()
        self._history: List[Dict[str, Any]] = []

    # ── transitions ────────────────────────────────────────────────────────
    def _go(self, new_state: SessionState, reason: str) -> None:
        if new_state is self.state:
            return
        old = self.state
        self.state = new_state
        self._last_reason = reason
        self._last_transition_at = self._clock()
        self._history.append({
            "at": self._last_transition_at,
            "from": old.value, "to": new_state.value, "reason": reason,
        })
        if len(self._history) > self._max_history:
            del self._history[:len(self._history) - self._max_history]
        logger.info("session_state: %s -> %s (%s)", old.value, new_state.value, reason)
        if self._on_transition is not None:
            try:
                self._on_transition(old, new_state, reason)
            except Exception:
                logger.debug("session_state: on_transition failed", exc_info=True)

    def handle(self, event: SessionEvent, reason: str = "") -> SessionState:
        """Apply one observation and return the resulting state.

        Never raises: a lifecycle bookkeeping failure must not be able to take
        down the orchestrator's watchdog.
        """
        try:
            return self._handle(event, reason or event.value)
        except Exception:
            logger.exception("session_state: failed handling %s", event.value)
            return self.state

    def _handle(self, event: SessionEvent, reason: str) -> SessionState:
        # ── explicit stop always wins ──
        if event is SessionEvent.STOP:
            self._auth_attempt_in_flight = False
            self._reconnect_in_flight = False
            self._go(SessionState.STOPPED, reason)
            return self.state

        # ── authentication observations ──
        if event is SessionEvent.AUTH_CONFIRMED:
            self._auth_confirmed = True
            self._auth_confirmed_at = self._clock()
            # A positive confirmation is the only thing that retires the
            # previous session: reset the attempt budget so a later, unrelated
            # expiry gets a full allowance again.
            self._auth_attempts = 0
            self._auth_attempt_in_flight = False
            self._otp_required = False
            self._go(SessionState.CONNECTED, reason)
            return self.state

        if event is SessionEvent.AUTH_REJECTED:
            # The broker has told us this session is dead. Retrying it is
            # pointless, so leave the transport path entirely.
            self._auth_confirmed = False
            self._auth_attempt_in_flight = False
            # Idempotent: the broker can emit `authorization/reject` more than
            # once for the same dead session (one per socket, or repeatedly on
            # a half-open one). Re-running the transition each time would
            # churn the history and spam the log without changing anything,
            # so a repeat while already on the auth path is a no-op.
            if self.state in (SessionState.SESSION_EXPIRED,
                              SessionState.FRESH_SESSION_REQUIRED):
                return self.state
            self._go(SessionState.SESSION_EXPIRED, reason)
            # Immediately decide what happens next, so the caller does not have
            # to poll: either we may attempt a fresh login, or we must stop.
            return self._after_expiry(reason)

        # ── OTP / re-auth ──
        if event is SessionEvent.OTP_REQUIRED:
            self._otp_required = True
            self._auth_attempt_in_flight = False
            self._go(SessionState.REAUTH_REQUIRED, reason)
            return self.state

        if event is SessionEvent.OTP_SATISFIED:
            # The human supplied a code. We are NOT authenticated yet -- the
            # broker still has to confirm. Stay out of CONNECTED until it does.
            self._otp_required = False
            self._go(SessionState.AUTHENTICATING, reason)
            return self.state

        # ── fresh-session (re-authentication) path ──
        if event is SessionEvent.FRESH_SESSION_STARTED:
            self._auth_attempt_in_flight = True
            self._go(SessionState.AUTHENTICATING, reason)
            return self.state

        if event is SessionEvent.FRESH_SESSION_SUCCEEDED:
            # A new session was obtained. It is NOT yet trusted: the WebSocket
            # still has to authorize with it, which is what AUTH_CONFIRMED
            # reports. Until then, no trading.
            self._auth_attempt_in_flight = False
            self._go(SessionState.NEW_SESSION_CREATED, reason)
            return self.state

        if event is SessionEvent.FRESH_SESSION_FAILED:
            self._auth_attempt_in_flight = False
            return self._after_expiry(reason)

        # ── transport path ──
        if event is SessionEvent.WS_CONNECTED:
            # The socket is up. Whether we may trade depends on auth, not on
            # the socket -- so only report CONNECTED if auth is confirmed and
            # still fresh.
            if self._auth_confirmed and not self._auth_is_stale():
                self._reconnect_in_flight = False
                self._go(SessionState.CONNECTED, reason)
            else:
                self._go(SessionState.WS_DISCONNECTED, reason)
            return self.state

        if event is SessionEvent.WS_DISCONNECTED:
            self._reconnect_in_flight = False
            # A dropped socket is NOT evidence of session expiry. Keep the auth
            # confirmation as-is so a plain reconnect can resume.
            if self.state in (SessionState.SESSION_EXPIRED,
                              SessionState.FRESH_SESSION_REQUIRED,
                              SessionState.AUTHENTICATING,
                              SessionState.NEW_SESSION_CREATED,
                              SessionState.REAUTH_REQUIRED,
                              SessionState.AUTH_EXHAUSTED):
                return self.state          # auth path owns the lifecycle now
            self._go(SessionState.WS_DISCONNECTED, reason)
            return self.state

        if event is SessionEvent.RECONNECT_STARTED:
            if self.state in (SessionState.SESSION_EXPIRED,
                              SessionState.FRESH_SESSION_REQUIRED,
                              SessionState.AUTHENTICATING,
                              SessionState.NEW_SESSION_CREATED,
                              SessionState.REAUTH_REQUIRED,
                              SessionState.AUTH_EXHAUSTED):
                return self.state
            self._reconnect_in_flight = True
            self._go(SessionState.RECONNECTING, reason)
            return self.state

        if event is SessionEvent.RECONNECT_SUCCEEDED:
            self._reconnect_in_flight = False
            if self._auth_confirmed and not self._auth_is_stale():
                self._go(SessionState.CONNECTED, reason)
            return self.state

        if event is SessionEvent.RECONNECT_FAILED:
            self._reconnect_in_flight = False
            if self.state is SessionState.RECONNECTING:
                self._go(SessionState.WS_DISCONNECTED, reason)
            return self.state

        return self.state

    def _after_expiry(self, reason: str) -> SessionState:
        """Decide between one more fresh-auth attempt and giving up."""
        if self._auth_attempts >= self.policy.max_auth_attempts:
            self._go(SessionState.AUTH_EXHAUSTED,
                     f"{reason}; {self._auth_attempts} auth attempts exhausted")
            return self.state
        self._go(SessionState.FRESH_SESSION_REQUIRED, reason)
        return self.state

    # ── queries ────────────────────────────────────────────────────────────
    def _auth_is_stale(self) -> bool:
        if self._auth_confirmed_at is None:
            return True
        return (self._clock() - self._auth_confirmed_at) > \
            self.policy.auth_reconfirm_after_seconds

    @property
    def trading_allowed(self) -> bool:
        """True ONLY when authentication is positively confirmed and fresh.

        This is the invariant that stops the previous behaviour of trading on a
        socket the broker had already deauthorized.
        """
        if self.state not in TRADING_ALLOWED_STATES:
            return False
        if self._otp_required:
            return False
        return self._auth_confirmed and not self._auth_is_stale()

    @property
    def auth_confirmed(self) -> bool:
        return self._auth_confirmed

    @property
    def otp_required(self) -> bool:
        return self._otp_required

    @property
    def auth_attempts(self) -> int:
        return self._auth_attempts

    @property
    def auth_in_flight(self) -> bool:
        return self._auth_attempt_in_flight

    @property
    def reconnect_in_flight(self) -> bool:
        return self._reconnect_in_flight

    @property
    def needs_fresh_session(self) -> bool:
        return self.state is SessionState.FRESH_SESSION_REQUIRED

    @property
    def is_exhausted(self) -> bool:
        return self.state is SessionState.AUTH_EXHAUSTED

    def begin_auth_attempt(self) -> bool:
        """Claim the single authentication slot.

        Returns False if an attempt is already in flight, or the budget is
        spent. This is the guard against concurrent logins and against
        duplicate sessions fighting each other.
        """
        if self._auth_attempt_in_flight:
            logger.warning("session_state: auth attempt already in flight; refusing concurrent login")
            return False
        if self._auth_attempts >= self.policy.max_auth_attempts:
            logger.warning("session_state: auth budget exhausted (%d/%d)",
                           self._auth_attempts, self.policy.max_auth_attempts)
            self._go(SessionState.AUTH_EXHAUSTED, "auth budget exhausted")
            return False
        self._auth_attempts += 1
        self._auth_attempt_in_flight = True
        return True

    def auth_backoff_seconds(self) -> float:
        """Exponential backoff for authentication attempts, capped."""
        attempt = max(0, self._auth_attempts - 1)
        delay = self.policy.auth_backoff_base_seconds * (2 ** attempt)
        return min(delay, self.policy.auth_backoff_max_seconds)

    def reset_auth_budget(self, reason: str = "manual reset") -> None:
        """Clear the attempt counter (e.g. after a human completes OTP)."""
        self._auth_attempts = 0
        self._auth_attempt_in_flight = False
        self._otp_required = False
        if self.state in (SessionState.AUTH_EXHAUSTED, SessionState.REAUTH_REQUIRED):
            self._go(SessionState.WS_DISCONNECTED, reason)

    def snapshot(self) -> SessionSnapshot:
        age = None
        if self._auth_confirmed_at is not None:
            age = round(self._clock() - self._auth_confirmed_at, 3)
        return SessionSnapshot(
            state=self.state.value,
            auth_confirmed=self._auth_confirmed,
            auth_age_seconds=age,
            auth_attempts=self._auth_attempts,
            otp_required=self._otp_required,
            trading_allowed=self.trading_allowed,
            last_transition_at=self._last_transition_at,
            last_reason=self._last_reason,
            history=list(self._history[-10:]),
        )
