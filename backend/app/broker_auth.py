"""Broker authentication session manager for Quotex.

Single owner of the broker session lifecycle:

    load -> validate -> reuse if the broker confirms
                    -> otherwise establish a fresh session legitimately

Why this exists
---------------
Before this module the session lifecycle was spread across two files with no
single owner:

* ``market.py`` read/wrote ``<sessions_dir>/session.json`` itself, and
  ``_seed_session_via_curlcffi`` was the only login implementation --
  reachable only from ``connect()``, never from a mid-session rejection.
* ``orchestrator._recover_session`` decided *when* to re-authenticate but
  called the provider directly, so nothing else could reuse that logic.

This module owns the lifecycle. The orchestrator keeps deciding *when*; the
provider keeps implementing *how*. Nothing here re-implements a login.

Design rules enforced here
--------------------------
1. Authentication is isolated from trading execution. This module performs no
   trade, reads no market data and is never imported by the strategy, candle,
   risk or execution layers.
2. Validation must prove the session is actually usable. A JSON file on disk
   proves nothing; ``validate_session`` asks the broker (via the injected
   probe) and treats an inconclusive answer as "not validated" -- but NOT as
   "expired", so a transient network blip cannot throw away a good session.
3. Per-account independence. The manager is constructed with one ``user_id``
   and one ``session_dir``. It cannot read another account's session.
4. No bypass. Quotex may sit behind Cloudflare. Nothing here defeats it,
   solves a challenge, spoofs a fingerprint or extracts a challenge token.
   When the broker demands interactive verification the manager reports it
   (``AUTH_BROWSER_REQUIRED``) and stops, leaving the legitimate sign-in to a
   real user. Same for an OTP/PIN.
5. Secrets are never logged. ``SessionBundle`` has a secret-suppressing
   ``__repr__`` and every event payload carries lengths, not values.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("app.broker_auth")

# ── Structured event names ─────────────────────────────────────────────────
# Emitted through the injected `on_event` hook. Payloads NEVER contain an
# ssid, a cookie, a password, a token or an auth header.
AUTH_SESSION_LOAD = "AUTH_SESSION_LOAD"
AUTH_SESSION_VALID = "AUTH_SESSION_VALID"
AUTH_SESSION_EXPIRED = "AUTH_SESSION_EXPIRED"
AUTH_SESSION_INVALID = "AUTH_SESSION_INVALID"
AUTH_FRESH_SESSION_REQUIRED = "AUTH_FRESH_SESSION_REQUIRED"
AUTH_FRESH_SESSION_STARTED = "AUTH_FRESH_SESSION_STARTED"
AUTH_FRESH_SESSION_SUCCEEDED = "AUTH_FRESH_SESSION_SUCCEEDED"
AUTH_FRESH_SESSION_FAILED = "AUTH_FRESH_SESSION_FAILED"
AUTH_BROWSER_REQUIRED = "AUTH_BROWSER_REQUIRED"
AUTH_OTP_REQUIRED = "AUTH_OTP_REQUIRED"
AUTH_SESSION_SAVED = "AUTH_SESSION_SAVED"
AUTH_SESSION_INVALIDATED = "AUTH_SESSION_INVALIDATED"
AUTH_RECONNECT_SUCCESS = "AUTH_RECONNECT_SUCCESS"
AUTH_RECONNECT_FAILED = "AUTH_RECONNECT_FAILED"

#: The session filename the vendor writes into `session_dir`.
SESSION_FILE = "quotex_session.json"

#: Markers meaning Quotex wants a real browser to complete a security check.
#: These are DETECTED so the manager can stop honestly. They are never
#: bypassed, spoofed or solved.
BROWSER_REQUIRED_MARKERS = (
    "just a moment",
    "checking your browser",
    "cloudflare",
    "verify you are human",
    "verify you are a human",
    "enable javascript and cookies",
    "attention required",
    "cf-chl",
    "cf_chl",
    "cf-turnstile",
    "turnstile",
    "ray id",
    "cf-ray",
    "challenge-platform",
    "cf-browser-verification",
    "security check",
    "are you a robot",
    "captcha",
)


@dataclass
class SessionBundle:
    """The minimum the vendor client needs to resume an authenticated session.

    Holds secrets: it deliberately has a ``__repr__`` that prints lengths only
    and must never be handed to a logger.
    """

    ssid: str
    cookies: Optional[str] = None
    user_agent: Optional[str] = None
    #: Set when the broker has already told us this session is dead.
    expired: bool = False

    __repr__ = lambda self: (  # noqa: E731 - intentional secret suppression
        f"SessionBundle(ssid_len={len(self.ssid or '')}, "
        f"cookies_len={len(self.cookies or '')})"
    )

    def is_usable(self) -> bool:
        """True only when both halves the vendor needs are present."""
        return bool(self.ssid) and bool(self.cookies) and not self.expired

    def redacted(self) -> Dict[str, Any]:
        """Secret-free description, safe for events and logs."""
        return {
            "ssid_len": len(self.ssid or ""),
            "cookies_len": len(self.cookies or ""),
            "user_agent": self.user_agent,
            "expired": self.expired,
        }


@dataclass
class ValidationResult:
    """Outcome of asking the broker whether the session still works."""

    valid: bool = False
    #: The broker did not give a definite yes or no (network blip, mid-login).
    #: Deliberately NOT the same as expired.
    unknown: bool = False
    expired: bool = False
    rejected: bool = False
    browser_required: bool = False
    otp_required: bool = False
    reason: str = ""
    bundle: Optional[SessionBundle] = None

    def __post_init__(self) -> None:
        if not self.valid:
            # Never hand back a bundle holding live secrets alongside a
            # "this is not usable" verdict -- callers cannot accidentally
            # re-inject a session the broker refused.
            self.bundle = None

    def __repr__(self) -> str:  # pragma: no cover - diagnostic
        return (f"ValidationResult(valid={self.valid}, unknown={self.unknown}, "
                f"expired={self.expired}, rejected={self.rejected}, "
                f"browser_required={self.browser_required}, "
                f"otp_required={self.otp_required}, reason={self.reason!r})")


@dataclass
class AuthState:
    """What the manager currently believes about this account."""

    session_present: bool = False
    session_valid: bool = False
    last_validation: Optional[ValidationResult] = None
    browser_required: bool = False
    otp_required: bool = False
    auth_attempts: int = 0
    events: list = field(default_factory=list)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "session_present": self.session_present,
            "session_valid": self.session_valid,
            "browser_required": self.browser_required,
            "otp_required": self.otp_required,
            "auth_attempts": self.auth_attempts,
            "last_reason": self.last_validation.reason if self.last_validation else "",
        }


class QuotexAuthSessionManager:
    """Owns one user's broker session from load to invalidation.

    All broker-facing behaviour is injected, which keeps this module free of
    duplicated login logic and makes every branch testable without a network:

    ``persist(ssid, cookies, user_agent)``  write the session to durable store
    ``load() -> Optional[SessionBundle]``   read it back
    ``fresh_login() -> Awaitable[bool]``    run the broker's sign-in flow
    ``auth_probe() -> str``                 the broker's verdict on the session
    ``discard()``                           remove it from the durable store
    ``on_event(name, **payload)``           structured event sink

    The manager holds no credential of its own. It never sees a password.
    """

    def __init__(
        self,
        user_id: str,
        session_dir: Optional[Path] = None,
        persist: Optional[Callable[[str, Optional[str], Optional[str]], None]] = None,
        load: Optional[Callable[[], Optional[SessionBundle]]] = None,
        fresh_login: Optional[Callable[[], Awaitable[bool]]] = None,
        auth_probe: Optional[Callable[[], str]] = None,
        discard: Optional[Callable[[], None]] = None,
        on_event: Optional[Callable[..., None]] = None,
    ) -> None:
        self.user_id = user_id
        self.session_dir = Path(session_dir) if session_dir else None
        self._persist = persist
        self._load = load
        self._fresh_login = fresh_login
        self._auth_probe = auth_probe
        self._discard = discard
        self._on_event = on_event

        self.state = AuthState()
        self.last_bundle: Optional[SessionBundle] = None
        self._in_flight = False

    # ── event emission ──────────────────────────────────────────────────────

    def _emit(self, name: str, **payload: Any) -> None:
        """Record an event. Payloads carry lengths, never secret values."""
        payload.setdefault("user_id", self.user_id)
        self.state.events.append((name, payload))
        # Keep the in-memory ring bounded for a long-running process.
        if len(self.state.events) > 256:
            del self.state.events[:128]
        if self._on_event is None:
            return
        try:
            self._on_event(name, **payload)
        except Exception as exc:  # noqa: BLE001 - an event sink must not
            logger.debug("[auth] event sink failed: %s", type(exc).__name__)
            # break the auth flow

    # ── paths and permissions ───────────────────────────────────────────────

    @property
    def session_path(self) -> Optional[Path]:
        """This user's session file, or None when no directory was configured."""
        return self.session_dir / SESSION_FILE if self.session_dir else None

    def _write(self, bundle: SessionBundle) -> None:
        """Atomically write the session with owner-only permissions."""
        if self.session_dir is None:
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # Directory first: a world-readable directory would leak the filename
        # and allow an unprivileged local user to swap the file out.
        try:
            os.chmod(self.session_dir, 0o700)
        except OSError:
            pass
        tmp = self.session_dir / f".{SESSION_FILE}.tmp"
        payload = {
            "cookies": bundle.cookies,
            "ssid": bundle.ssid,
            "user_agent": bundle.user_agent,
        }
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.session_path)
        except OSError as exc:
            logger.warning("[auth] could not persist session for user=%s: %s",
                           self.user_id, type(exc).__name__)

    def _remove_file(self) -> None:
        if self.session_path is None:
            return
        try:
            self.session_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ── load_session() ──────────────────────────────────────────────────────

    def load_session(self) -> Optional[SessionBundle]:
        """Return the stored session for this user, or None.

        A missing, unreadable or corrupt file is treated as "no session" --
        never as a crash, and never as proof the session is valid.
        """
        bundle: Optional[SessionBundle] = None
        if self._load is not None:
            try:
                bundle = self._load()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[auth] session loader raised for user=%s: %s",
                               self.user_id, type(exc).__name__)
                bundle = None
        if bundle is None and self.session_path is not None:
            bundle = self._read_file()
        if bundle is not None and not bundle.is_usable():
            logger.debug("[auth] stored session for user=%s is incomplete; "
                         "treating as absent", self.user_id)
            bundle = None

        self.last_bundle = bundle
        self.state.session_present = bundle is not None
        self._emit(AUTH_SESSION_LOAD,
                   found=bundle is not None,
                   ssid_len=len(bundle.ssid) if bundle else 0,
                   cookies_len=len(bundle.cookies or "") if bundle else 0)
        return bundle

    def _read_file(self) -> Optional[SessionBundle]:
        path = self.session_path
        if path is None or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            logger.warning("[auth] session file present but unreadable for "
                           "user=%s; treating as absent", self.user_id)
            return None
        if not isinstance(raw, dict):
            return None
        ssid = raw.get("ssid")
        cookies = raw.get("cookies")
        if not ssid:
            return None
        return SessionBundle(ssid=ssid, cookies=cookies,
                             user_agent=raw.get("user_agent"))

    # ── save_session() ──────────────────────────────────────────────────────

    async def save_session(self, ssid: str, cookies: Optional[str],
                           user_agent: Optional[str] = None) -> bool:
        """Persist a freshly authenticated session."""
        bundle = SessionBundle(ssid=ssid, cookies=cookies, user_agent=user_agent)
        return await self._save_bundle(bundle)

    async def _save_bundle(self, bundle: SessionBundle) -> bool:
        if not bundle.ssid:
            logger.debug("[auth] refusing to save a session without an ssid")
            return False
        self._write(bundle)
        if self._persist is not None:
            try:
                self._persist(bundle.ssid, bundle.cookies, bundle.user_agent)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[auth] session store rejected the write for "
                               "user=%s: %s", self.user_id, type(exc).__name__)
                return False
        self.last_bundle = bundle
        self.state.session_present = True
        self._emit(AUTH_SESSION_SAVED, ssid_len=len(bundle.ssid),
                   cookies_len=len(bundle.cookies or ""),
                   has_user_agent=bool(bundle.user_agent))
        return True

    # ── validate_session() ──────────────────────────────────────────────────

    async def validate_session(self) -> ValidationResult:
        """Prove the session is actually usable.

        A file on disk is not evidence. The broker's own verdict is.

        Classification matters (item 7): only an explicit rejection or expiry
        counts as an auth failure. Network problems and in-progress logins are
        reported as ``unknown`` so the caller does not burn a login on a
        blip -- but ``unknown`` never grants trading either.
        """
        bundle = self.load_session()
        if bundle is None:
            result = ValidationResult(reason="no stored session")
            self.state.last_validation = result
            self.state.session_valid = False
            self._emit(AUTH_FRESH_SESSION_REQUIRED, reason="no stored session")
            return result

        if self._auth_probe is None:
            # No way to ask the broker, so nothing is proven either way.
            result = ValidationResult(unknown=True, bundle=bundle,
                                      reason="no auth probe available")
            self.state.last_validation = result
            self.state.session_valid = False
            return result

        try:
            verdict = str(self._auth_probe())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[auth] probe raised for user=%s: %s",
                           self.user_id, type(exc).__name__)
            verdict = "error"

        result = self._classify(verdict, bundle)
        self.state.last_validation = result
        self.state.session_valid = result.valid
        self.state.browser_required = result.browser_required
        self.state.otp_required = result.otp_required

        if result.valid:
            self._emit(AUTH_SESSION_VALID, reason=result.reason,
                       ssid_len=len(bundle.ssid))
        elif result.expired or result.rejected:
            self._emit(AUTH_SESSION_EXPIRED, reason=result.reason,
                       ssid_len=len(bundle.ssid))
        elif result.browser_required:
            self._emit(AUTH_BROWSER_REQUIRED, reason=result.reason)
        elif result.otp_required:
            self._emit(AUTH_OTP_REQUIRED, reason=result.reason)
        else:
            self._emit(AUTH_SESSION_INVALID, reason=result.reason,
                       unknown=result.unknown)
        return result

    #: Broker verdicts that mean "this session is dead".
    _EXPIRED_VERDICTS = frozenset({
        "expired", "failed", "rejected", "not_authenticated", "unauthorized",
        "authorization/reject", "auth_failed", "invalid_session",
    })
    #: Verdicts that are NOT auth failures.
    _TRANSIENT_VERDICTS = frozenset({
        "unknown", "error", "authenticating", "connecting", "reconnecting",
        "websocket_disconnected", "disconnected", "network", "timeout",
    })

    def _classify(self, verdict: str, bundle: SessionBundle) -> ValidationResult:
        v = verdict.strip().lower()

        if v in self._EXPIRED_VERDICTS:
            return ValidationResult(expired=True, rejected=True, bundle=bundle,
                                    reason=f"broker rejected the session ({v})")
        # "browser_required" is the explicit verdict the caller emits when a
        # login was answered with an interactive challenge. It is matched here
        # as a literal, because none of BROWSER_REQUIRED_MARKERS (which test
        # challenge *page content*) can match a short status token -- without
        # this the verdict would fall through to `unknown` and be mistaken for
        # a transient network blip.
        if v == "browser_required" or self.requires_browser(v):
            return ValidationResult(browser_required=True, bundle=bundle,
                                    reason="broker requires an interactive "
                                           "browser security check")
        if v in ("otp_required", "pin_required", "awaiting_pin", "reauth_required"):
            return ValidationResult(otp_required=True, bundle=bundle,
                                    reason="broker requires a one-time code")
        if v in self._TRANSIENT_VERDICTS:
            return ValidationResult(unknown=True, bundle=bundle,
                                    reason=f"broker did not confirm ({v})")
        if v in ("authenticated", "connected", "ok", "authorized", "valid",
                 "success", "logged_in"):
            return ValidationResult(valid=True, bundle=bundle,
                                    reason=f"broker confirmed ({v})")
        # Unrecognised verdict: not proof of validity.
        return ValidationResult(unknown=True, bundle=bundle,
                                reason=f"unrecognised verdict ({v})")

    # ── invalidate_session() ────────────────────────────────────────────────

    def invalidate_session(self) -> None:
        """Drop a session the broker no longer accepts.

        Only ever called after the broker has rejected it, so this cannot
        destroy a session that is still working.

        BOTH copies go: the on-disk file and the durable store. Clearing only
        the file would let a restart resurrect a session the broker already
        rejected, which is precisely the failure mode this manager exists to
        prevent.
        """
        self._remove_file()
        if self._discard is not None:
            try:
                self._discard()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[auth] could not clear the stored session for "
                               "user=%s: %s", self.user_id, type(exc).__name__)
        self.last_bundle = None
        self.state.session_present = False
        self.state.session_valid = False
        self._emit(AUTH_SESSION_INVALIDATED, reason="session rejected or expired")

    # ── create_fresh_session() ──────────────────────────────────────────────

    async def create_fresh_session(self) -> bool:
        """Establish a new session through the broker's own sign-in flow.

        Concurrent attempts are refused: two logins racing for one account is
        how a working session gets clobbered.

        Nothing here bypasses a control. If Quotex demands a PIN or an
        interactive browser challenge, the injected login reports failure and
        this returns False without inventing a way around it.
        """
        if self._in_flight:
            logger.info("[auth] authentication already in progress for user=%s; "
                        "refusing a concurrent attempt", self.user_id)
            return False
        if self._fresh_login is None:
            logger.warning("[auth] no login implementation configured for user=%s",
                           self.user_id)
            return False

        self._in_flight = True
        self.state.auth_attempts += 1
        self._emit(AUTH_FRESH_SESSION_STARTED, attempt=self.state.auth_attempts)
        try:
            try:
                ok = bool(await self._fresh_login())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[auth] login raised for user=%s: %s",
                               self.user_id, type(exc).__name__)
                ok = False

            if not ok:
                self.state.last_validation = ValidationResult(
                    reason="fresh authentication did not complete")
                self._emit(AUTH_FRESH_SESSION_FAILED,
                           attempt=self.state.auth_attempts,
                           browser_required=self.state.browser_required,
                           otp_required=self.state.otp_required)
                return False

            # The login produced something; now prove the broker accepts it.
            bundle = self.load_session()
            if bundle is None:
                self._emit(AUTH_FRESH_SESSION_FAILED,
                           attempt=self.state.auth_attempts,
                           reason="login reported success but produced no session")
                return False
            await self._save_bundle(bundle)

            verdict = "unknown"
            if self._auth_probe is not None:
                try:
                    verdict = str(self._auth_probe())
                except Exception:  # noqa: BLE001
                    verdict = "error"
            result = self._classify(verdict, bundle)
            self.state.last_validation = result
            self.state.session_valid = result.valid

            if not result.valid:
                # Never report a session as usable that the broker has not
                # confirmed. `unknown` here means "we could not confirm".
                self._emit(AUTH_FRESH_SESSION_FAILED,
                           attempt=self.state.auth_attempts,
                           reason=result.reason, unknown=result.unknown)
                return False

            self._emit(AUTH_FRESH_SESSION_SUCCEEDED,
                       attempt=self.state.auth_attempts,
                       ssid_len=len(bundle.ssid))
            self._emit(AUTH_SESSION_VALID, reason=result.reason,
                       ssid_len=len(bundle.ssid))
            return True
        finally:
            self._in_flight = False

    # ── reconnect_session() ─────────────────────────────────────────────────

    async def reconnect_session(self) -> bool:
        """Get back to a usable session by the shortest legitimate route.

        Validate first and reuse when the broker confirms: a dropped websocket
        is a network problem and must not cost the account a new login. Only a
        genuine auth failure invalidates the session and triggers fresh auth.
        """
        result = await self.validate_session()
        if result.valid:
            self._emit(AUTH_RECONNECT_SUCCESS, reason="existing session still valid")
            return True

        if result.browser_required:
            # Stop and say so. No bypass.
            self._emit(AUTH_RECONNECT_FAILED, reason="interactive browser "
                                                     "verification required")
            return False
        if result.otp_required:
            self._emit(AUTH_RECONNECT_FAILED,
                       reason="a one-time code is required from the user")
            return False

        if result.expired or result.rejected:
            # The broker said no. Now -- and only now -- drop it.
            self.invalidate_session()
        elif result.unknown or result.bundle is not None:
            # The broker did not say no -- it did not say anything definite.
            # A dropped websocket, a mid-flight login or a transient network
            # error is NOT session expiry, so it must not cost the account a
            # fresh login: only an auth failure triggers fresh authentication.
            # We still refuse to report the session as usable.
            self._emit(AUTH_RECONNECT_FAILED,
                       reason="broker did not confirm the session; no fresh "
                              "authentication triggered for an inconclusive "
                              "verdict")
            return False

        ok = await self.create_fresh_session()
        if ok:
            self._emit(AUTH_RECONNECT_SUCCESS, reason="fresh session established")
            return True

        self._emit(AUTH_RECONNECT_FAILED,
                   reason=self.state.last_validation.reason
                   if self.state.last_validation else "unknown")
        return False

    # ── Cloudflare / interactive-challenge detection ────────────────────────

    @staticmethod
    def requires_browser(text: Optional[str]) -> bool:
        """True when Quotex is asking for a real browser to complete a check.

        Detection only. This module contains no bypass, no challenge solving,
        no fingerprint spoofing and no token extraction -- when the broker
        wants an interactive verification, a human completes it in a real
        browser.
        """
        if not text:
            return False
        low = text.lower()
        return any(marker in low for marker in BROWSER_REQUIRED_MARKERS)

    # ── convenience ─────────────────────────────────────────────────────────

    @property
    def can_trade(self) -> bool:
        """Trading is allowed only on a session the broker has confirmed."""
        return bool(self.state.session_valid)

    @property
    def in_progress(self) -> bool:
        return self._in_flight

    def snapshot(self) -> Dict[str, Any]:
        """Secret-free view of the account's auth state."""
        snap = self.state.snapshot()
        snap["in_progress"] = self._in_flight
        return snap
