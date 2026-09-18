"""Per-user Quotex broker session persistence.

Stores email/password/ssid/cookies encrypted at rest in PostgreSQL so that:
  - a backend restart or VPS reboot does not force the user to log in again
    (as long as the stored session is still valid), and
  - one user's broker credentials/session are never visible to another user.

This does NOT talk to Quotex directly — it only stores/retrieves what the
market provider (services/market.py) needs to reconnect. Expiry detection is
best-effort: we record `expires_at` when known and always record `login_at`,
and treat a session as "possibly stale" after SESSION_SOFT_TTL seconds so the
orchestrator knows to re-authenticate proactively rather than wait for a
connect failure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from . import db
from .security import decrypt, encrypt

# If we don't have a hard expiry from the broker, treat sessions older than
# this as due for a proactive re-login attempt (soft TTL, not a hard cutoff).
SESSION_SOFT_TTL = 60 * 60 * 20  # 20 hours


@dataclass
class BrokerCredentials:
    """Duck-typed to match the subset of `config.Settings` that
    `services.market.build_provider` reads (quotex_email, quotex_password,
    quotex_is_demo, quotex_ssid, quotex_user_agent, quotex_cookies)."""
    quotex_email: Optional[str] = None
    quotex_password: Optional[str] = None
    quotex_is_demo: bool = True
    quotex_ssid: Optional[str] = None
    quotex_user_agent: Optional[str] = None
    quotex_cookies: Optional[str] = None


@dataclass
class BrokerSessionInfo:
    provider: str
    account_type: str
    login_at: Optional[float]
    expires_at: Optional[float]
    has_ssid: bool

    @property
    def is_stale(self) -> bool:
        if self.expires_at:
            return time.time() >= self.expires_at
        if self.login_at:
            return time.time() - self.login_at >= SESSION_SOFT_TTL
        return True


class SessionManager:
    """All methods are synchronous and open-and-close their own pooled
    PostgreSQL connection per call (via db.tx()/db.read_tx()) — no state is
    held on `self`, so one SessionManager instance is safe to share across
    every request/user/background task. Call from async code via
    `asyncio.to_thread` if it ever shows up as a bottleneck — it won't at
    this scale."""

    def save_credentials(self, user_id: str, email: str, password: str,
                          is_demo: bool = True, user_agent: Optional[str] = None) -> None:
        now = time.time()
        with db.tx() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO broker_sessions (user_id, provider, email_enc, password_enc,
                                                  user_agent, account_type, updated_at)
                    VALUES (:user_id, 'pyquotex', :email_enc, :password_enc, :user_agent, :account_type, :now)
                    ON CONFLICT (user_id) DO UPDATE SET
                        email_enc = excluded.email_enc,
                        password_enc = excluded.password_enc,
                        user_agent = COALESCE(excluded.user_agent, broker_sessions.user_agent),
                        account_type = excluded.account_type,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "user_id": user_id,
                    "email_enc": encrypt(email),
                    "password_enc": encrypt(password),
                    "user_agent": user_agent,
                    "account_type": "PRACTICE" if is_demo else "REAL",
                    "now": now,
                },
            )

    def save_session_token(self, user_id: str, ssid: Optional[str], cookies: Optional[str] = None,
                            expires_at: Optional[float] = None) -> None:
        """Called after a successful broker connect to persist the fresh
        session token so the next restart can reuse it without a full login."""
        now = time.time()
        with db.tx() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO broker_sessions (user_id, provider, ssid_enc, cookies_enc,
                                                  login_at, expires_at, updated_at)
                    VALUES (:user_id, 'pyquotex', :ssid_enc, :cookies_enc, :login_at, :expires_at, :now)
                    ON CONFLICT (user_id) DO UPDATE SET
                        ssid_enc = excluded.ssid_enc,
                        cookies_enc = COALESCE(excluded.cookies_enc, broker_sessions.cookies_enc),
                        login_at = excluded.login_at,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "user_id": user_id,
                    "ssid_enc": encrypt(ssid),
                    "cookies_enc": encrypt(cookies),
                    "login_at": now,
                    "expires_at": expires_at,
                    "now": now,
                },
            )

    def get_credentials(self, user_id: str) -> Optional[BrokerCredentials]:
        with db.read_tx() as conn:
            row = conn.execute(
                text("SELECT * FROM broker_sessions WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).mappings().fetchone()
        if not row:
            return None
        return BrokerCredentials(
            quotex_email=decrypt(row["email_enc"]),
            quotex_password=decrypt(row["password_enc"]),
            quotex_is_demo=(row["account_type"] != "REAL"),
            quotex_ssid=decrypt(row["ssid_enc"]),
            quotex_user_agent=row["user_agent"],
            quotex_cookies=decrypt(row["cookies_enc"]),
        )

    def get_session_info(self, user_id: str) -> Optional[BrokerSessionInfo]:
        with db.read_tx() as conn:
            row = conn.execute(
                text(
                    "SELECT provider, account_type, login_at, expires_at, ssid_enc "
                    "FROM broker_sessions WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            ).mappings().fetchone()
        if not row:
            return None
        return BrokerSessionInfo(
            provider=row["provider"],
            account_type=row["account_type"],
            login_at=row["login_at"],
            expires_at=row["expires_at"],
            has_ssid=bool(row["ssid_enc"]),
        )

    def has_restorable_session(self, user_id: str) -> bool:
        """True if we have *something* to reconnect with — either a fresh-ish
        ssid or a stored email/password to fully re-authenticate."""
        info = self.get_session_info(user_id)
        creds = self.get_credentials(user_id)
        if not creds:
            return False
        if creds.quotex_ssid and info and not info.is_stale:
            return True
        return bool(creds.quotex_email and creds.quotex_password)

    def all_user_ids_with_sessions(self) -> list[str]:
        with db.read_tx() as conn:
            rows = conn.execute(text("SELECT user_id FROM broker_sessions")).mappings().fetchall()
        return [r["user_id"] for r in rows]

    def clear_session_token(self, user_id: str) -> None:
        """Wipe just the stored ssid/cookies/login timestamps, keeping the
        saved email/password. Called whenever credentials are (re)saved via
        the UI, so a stale/invalidated token from an earlier attempt is never
        silently reused — new credentials always mean a genuinely fresh
        login, never a resume attempt with old, possibly-dead session data."""
        with db.tx() as conn:
            conn.execute(
                text(
                    "UPDATE broker_sessions SET ssid_enc = NULL, cookies_enc = NULL, "
                    "login_at = NULL, expires_at = NULL, updated_at = :now WHERE user_id = :user_id"
                ),
                {"now": time.time(), "user_id": user_id},
            )

    def clear(self, user_id: str) -> None:
        with db.tx() as conn:
            conn.execute(
                text("DELETE FROM broker_sessions WHERE user_id = :user_id"),
                {"user_id": user_id},
            )


session_manager = SessionManager()
