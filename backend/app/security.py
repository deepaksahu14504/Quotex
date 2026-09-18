"""Auth primitives: password hashing, JWT session tokens, and symmetric
encryption for broker secrets stored at rest.

No new heavy auth framework — this is deliberately self-contained:
  - Password hashing: PBKDF2-HMAC-SHA256 (stdlib `hashlib`), 260k iterations.
  - JWT: minimal HS256 implementation (stdlib `hmac`/`hashlib`/`base64`/`json`).
  - Encryption at rest: Fernet (AES128-CBC + HMAC) via the `cryptography`
    package — this is the one new dependency, and it's the industry-standard
    choice for "encrypt this secret and store it" use cases.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA_DIR, ENV_FILE, read_env_file, update_env_file

# --------------------------------------------------------------------------- #
# Secret keys — generated once and persisted to .env so restarts don't
# invalidate every session / make stored secrets unreadable.
# --------------------------------------------------------------------------- #
_env = read_env_file()


def _get_or_create_secret(env_key: str, generator) -> str:
    val = os.environ.get(env_key) or _env.get(env_key)
    if val:
        return val
    val = generator()
    update_env_file({env_key: val})
    os.environ[env_key] = val
    return val


JWT_SECRET = _get_or_create_secret("JWT_SECRET", lambda: base64.urlsafe_b64encode(os.urandom(32)).decode())
ENCRYPTION_KEY = _get_or_create_secret("SESSION_ENCRYPTION_KEY", lambda: Fernet.generate_key().decode())

_fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

JWT_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iter_s, salt_b64, digest_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError) as e:
        # BUG FIX: Log password verification failures for security auditing
        import logging
        logging.getLogger(__name__).warning("Password verification failed: %s", type(e).__name__)
        return False


# --------------------------------------------------------------------------- #
# JWT (HS256) — minimal, dependency-free implementation
# --------------------------------------------------------------------------- #
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_token(user_id: str, extra: Optional[Dict[str, Any]] = None) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"sub": user_id, "iat": now, "exp": now + JWT_TTL_SECONDS, **(extra or {})}
    signing_input = f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode())}." \
                     f"{_b64url_encode(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = hmac.new(JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


class TokenError(Exception):
    pass


def decode_token(token: str) -> Dict[str, Any]:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise TokenError("Malformed token")
    signing_input = f"{header_b64}.{payload_b64}"
    expected_sig = hmac.new(JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, _b64url_decode(sig_b64)):
        raise TokenError("Bad signature")
    payload = json.loads(_b64url_decode(payload_b64))
    if payload.get("exp", 0) < time.time():
        raise TokenError("Token expired")
    return payload


# --------------------------------------------------------------------------- #
# Symmetric encryption for broker secrets (email/password/ssid/cookies)
# --------------------------------------------------------------------------- #
def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
