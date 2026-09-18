"""Owns one Orchestrator per logged-in user. This replaces the old
module-level singleton — every request/WS connection resolves its
orchestrator through here, keyed by user_id, so concurrent users never touch
each other's provider connection, risk state, or trade history."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Tuple

from .config import RuntimeSettings
from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Minimum time a per-user Orchestrator must have been (re)built before it can
# be manually restarted again. Guards against a double-tap on the Restart
# Pipeline button tearing down an instance that's still mid-boot (still
# authenticating, still restoring state) before it ever got a chance to
# connect.
MIN_RESTART_COOLDOWN_SECONDS = 10.0


class OrchestratorRegistry:
    def __init__(self) -> None:
        self._instances: Dict[str, Orchestrator] = {}
        self._lock = asyncio.Lock()
        self._last_restart: Dict[str, float] = {}

    async def _build(self, user_id: str) -> Orchestrator:
        """Single construction path shared by get_or_create() and restart(),
        so the hardening below applies to BOTH -- a manual Restart Pipeline
        must not silently skip the logging/off-loop-load that a cold start
        gets."""
        try:
            # RuntimeSettings.load() is a synchronous file read; run it off
            # the event loop so one user's cold start can't stall every
            # other user's concurrent get_or_create() call, which is waiting
            # on this same lock (this matters most right after a backend
            # restart, when several users' first requests land close
            # together).
            runtime = await asyncio.to_thread(RuntimeSettings.load, user_id)
            orch = Orchestrator(user_id, runtime)
            await orch.start()
            return orch
        except Exception:
            # Without this, a bad per-user settings/session file (or any
            # other construction/start failure) fails with no log line
            # anywhere -- worst case when the caller is the fire-and-forget
            # startup-restore task in main.py's lifespan, which previously
            # had nothing watching for it.
            logger.exception(
                "Failed to create/start orchestrator for user %s -- "
                "this user's bot will not run until this is resolved "
                "and get_or_create() is retried.", user_id,
            )
            raise

    async def get_or_create(self, user_id: str) -> Orchestrator:
        existing = self._instances.get(user_id)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._instances.get(user_id)
            if existing is not None:
                return existing
            orch = await self._build(user_id)
            self._instances[user_id] = orch
            return orch

    def get(self, user_id: str) -> "Orchestrator | None":
        return self._instances.get(user_id)

    def all(self):
        return list(self._instances.values())

    async def stop(self, user_id: str) -> None:
        orch = self._instances.pop(user_id, None)
        if orch:
            try:
                await orch.stop()
            except Exception:
                # BUG FIX: Log orchestrator stop failures
                logger.exception("Failed to stop orchestrator for user %s", user_id)

    async def stop_all(self) -> None:
        for user_id in list(self._instances.keys()):
            try:
                await self.stop(user_id)
            except Exception:
                logger.exception("Error stopping orchestrator %s in stop_all", user_id)

    async def restart(self, user_id: str) -> Tuple[Orchestrator, bool]:
        """Manual recovery path for a stuck pipeline: tears down and rebuilds
        just this ONE user's Orchestrator -- every other logged-in user's
        instance is untouched, since each lives under its own key in
        _instances and this only ever pops/rebuilds user_id. This is the
        exact same construction path (_build -> RuntimeSettings.load ->
        Orchestrator(...) -> start()) that already runs for every user on a
        full backend restart, just triggered on demand for one user instead
        of forced on everyone by a systemd restart.

        Returns (orchestrator, did_restart). did_restart is False when a
        restart happened too recently (MIN_RESTART_COOLDOWN_SECONDS) and the
        existing, already-fresh instance was returned instead -- so a
        double-tap on the button can't tear a brand-new instance down before
        it's finished connecting.
        """
        async with self._lock:
            existing = self._instances.get(user_id)
            last = self._last_restart.get(user_id, 0.0)
            if existing is not None and (time.time() - last) < MIN_RESTART_COOLDOWN_SECONDS:
                return existing, False
            old = self._instances.pop(user_id, None)
            if old is not None:
                try:
                    await old.stop()
                except Exception:
                    logger.exception("Failed to stop orchestrator for user %s during restart", user_id)
            orch = await self._build(user_id)
            self._instances[user_id] = orch
            self._last_restart[user_id] = time.time()
            return orch, True


registry = OrchestratorRegistry()
