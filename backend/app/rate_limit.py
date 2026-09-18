"""Simple in-memory sliding-window rate limiter, used to protect the login
endpoint from brute-force password guessing.

No external dependency (e.g. slowapi/Redis) required -- this app runs as a
single process per deployment, so an in-memory counter is sufficient. If
this were ever scaled to multiple processes behind a load balancer, this
would need a shared store instead, since each process would otherwise track
its own separate counters and the limit could be bypassed by hitting
different processes.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: float, lockout_seconds: float) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._attempts: Dict[str, List[float]] = {}
        self._locked_until: Dict[str, float] = {}

    def check(self, key: str) -> Optional[float]:
        """Returns seconds remaining if `key` is currently locked out, else
        None (request allowed to proceed)."""
        now = time.time()
        locked = self._locked_until.get(key)
        if locked is not None:
            if now < locked:
                return locked - now
            # Lockout expired -- clear it and this key's attempt history so
            # it gets a clean slate rather than an immediately-re-triggered lock.
            self._locked_until.pop(key, None)
            self._attempts.pop(key, None)
        return None

    def record_failure(self, key: str) -> None:
        now = time.time()
        window_start = now - self.window_seconds
        attempts = [t for t in self._attempts.get(key, []) if t >= window_start]
        attempts.append(now)
        self._attempts[key] = attempts
        if len(attempts) >= self.max_attempts:
            self._locked_until[key] = now + self.lockout_seconds

    def record_success(self, key: str) -> None:
        """Clears a key's failure history entirely on a successful login --
        a legitimate user who mistypes their password a couple of times and
        then gets it right shouldn't stay flagged."""
        self._attempts.pop(key, None)
        self._locked_until.pop(key, None)
