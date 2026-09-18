"""Background task supervision: restart-on-failure for long-running loops.

WHY THIS EXISTS
----------------
`asyncio.create_task(some_loop())` is fire-and-forget: if `some_loop()`
raises an exception nobody is awaiting that task, so the exception is
never seen anywhere. The task object sits in memory in a "done, with an
exception" state forever, and nothing about the running process looks
any different -- no crash, no log line, no restart. If that loop was the
one piece of code responsible for e.g. reconnecting a dropped broker
connection, the bot is now permanently stuck in whatever state it was in
the instant the exception was raised, with zero visible symptoms besides
"it stopped working." A process supervisor (systemd `Restart=always`)
cannot detect or fix this class of failure, because the *process* never
exits -- only one internal asyncio Task does.

This module is the fix: every long-running loop is started through
`TaskSupervisor.supervise(...)` instead of a raw `create_task(...)`. The
supervisor:
  - Runs the loop.
  - On ANY exit -- clean return or exception -- notices immediately.
  - A clean return is treated as an intentional stop (the loop's own
    `while self._running:` condition went false) and is NOT restarted.
  - An exception is logged with its full traceback, then the loop is
    restarted automatically after an exponential backoff (with jitter,
    reusing the same `ExponentialBackoff` primitive as the rest of the
    error-recovery code for consistency).
  - Restart counts, last error, and liveness timestamps are tracked per
    loop and exposed via `.snapshot()` so a loop that is silently
    crash-looping is visible in `/api/status` instead of only in logs.
  - Caps restarts: a loop that crashes the SAME way over and over is not
    something blind restarting fixes, so past `max_restarts` failures
    inside `restart_window` seconds the supervisor stops restarting it,
    marks it `stuck`, and fires `on_stuck(name)`. The owner uses that to
    surface a manual-restart prompt (state.stage="stuck" ->
    /api/pipeline/restart) instead of spinning forever in a hot crash
    loop. This half was merged in from the pipeline-fix change set's
    Orchestrator._run_supervised(); there is deliberately only ONE
    supervision mechanism in this codebase, and it is this class.

This is a safety net *underneath* the per-loop try/except blocks that
already exist elsewhere (those should catch the expected failure modes
already) -- it exists so that a gap in one of those try/except blocks,
or a genuinely unanticipated exception, still cannot permanently kill a
critical loop without at least being logged and retried.

Nothing in this file touches trading strategy, signal generation, or
execution logic -- it only supervises whichever coroutine it is handed.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional

from .error_recovery import ExponentialBackoff

logger = logging.getLogger(__name__)


@dataclass
class SupervisedTaskStats:
    """Point-in-time health snapshot for one supervised loop."""

    name: str
    started_at: float = field(default_factory=time.time)
    last_alive_at: float = field(default_factory=time.time)
    restart_count: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[float] = None
    running: bool = False
    # True once the loop has blown its restart budget and the supervisor
    # has given up on auto-recovery -- needs a manual restart.
    stuck: bool = False
    # Timestamps of restarts inside the current sliding window, used to
    # decide when the budget is blown.
    restart_times: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "running": self.running,
            "stuck": self.stuck,
            "restart_count": self.restart_count,
            "restarts_recent": len(self.restart_times),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "last_alive_age_s": round(time.time() - self.last_alive_at, 1),
        }


class TaskSupervisor:
    """Owns zero or more supervised background loops for one owner
    (one Orchestrator instance, or the process itself for process-wide
    monitors). Not shared across owners.
    """

    #: Defaults for the restart budget (see class docstring). Overridable
    #: per-supervise() call.
    DEFAULT_MAX_RESTARTS = 8
    DEFAULT_RESTART_WINDOW = 300.0

    def __init__(
        self,
        owner_label: str,
        on_stuck: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.owner_label = owner_label
        # Called (synchronously, best-effort) with the loop name the first
        # time that loop blows its restart budget. The Orchestrator uses
        # this to flip state.stage -> "stuck" and prompt a manual restart.
        self._on_stuck = on_stuck
        self._stats: Dict[str, SupervisedTaskStats] = {}
        self._stopping = False

    def supervise(
        self,
        name: str,
        coro_factory: Callable[[], Awaitable[None]],
        *,
        min_uptime_for_reset: float = 30.0,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        max_restarts: Optional[int] = None,
        restart_window: Optional[float] = None,
        is_active: Optional[Callable[[], bool]] = None,
    ) -> asyncio.Task:
        """Start supervision of a named loop and return the supervisor
        task. `coro_factory` must be a zero-arg callable returning a
        FRESH coroutine each call (a Task/coroutine can only be run
        once, so restarting means calling the factory again -- passing
        an already-created coroutine object here is a bug, not a
        restart).

        Callers cancel the returned task (same as they previously
        cancelled the raw task) to stop the loop for good: cancellation
        propagates into whichever attempt of `coro_factory()` is
        currently running, the supervisor sees its own CancelledError
        and does not restart.

        `is_active` (optional) makes a CLEAN return conditional rather
        than always-trusted. Every loop here is normally
        `while owner._running:`, so returning while the owner still
        considers itself running is not a graceful stop -- it's a bug
        (an early `return` down some path), and the loop should be
        restarted rather than quietly disappearing. Pass a predicate
        like `lambda: orchestrator._running`; when it returns True on a
        clean exit, that exit is treated as a failure. Merged in from
        the pipeline-fix change set's own supervisor, which had this
        check and would otherwise have been lost.
        """
        stats = self._stats.setdefault(name, SupervisedTaskStats(name=name))
        stats.running = True
        stats.stuck = False
        budget = self.DEFAULT_MAX_RESTARTS if max_restarts is None else max_restarts
        window = self.DEFAULT_RESTART_WINDOW if restart_window is None else restart_window

        async def _run_supervised() -> None:
            backoff = ExponentialBackoff(base_delay=base_delay, max_delay=max_delay)
            while not self._stopping:
                attempt_started = time.time()
                stats.last_alive_at = attempt_started
                try:
                    await coro_factory()
                    if is_active is not None and is_active():
                        # Returned while the owner still considers itself
                        # running: not a graceful stop. Fall through to the
                        # crash path so it gets restarted and logged.
                        raise RuntimeError(
                            f"{name} loop exited unexpectedly while still running"
                        )
                    # Clean return -- the loop's own `while self._running:`
                    # went false, i.e. stop() was called deliberately.
                    # Not a failure: don't restart, don't log as an error.
                    logger.info(
                        "[supervisor:%s] '%s' exited cleanly -- not restarting",
                        self.owner_label, name,
                    )
                    stats.running = False
                    return
                except asyncio.CancelledError:
                    stats.running = False
                    raise
                except Exception as exc:
                    now = time.time()
                    ran_for = now - attempt_started
                    if ran_for >= min_uptime_for_reset:
                        # Ran a reasonable while before dying this time --
                        # treat as a fresh failure rather than a
                        # continuation of some crash loop from hours ago,
                        # so backoff doesn't stay maxed out forever after
                        # one bad patch.
                        backoff.reset()
                        stats.consecutive_failures = 0
                    stats.consecutive_failures += 1
                    stats.restart_count += 1
                    stats.last_error = f"{type(exc).__name__}: {exc}"
                    stats.last_error_at = now
                    stats.restart_times = [t for t in stats.restart_times if now - t < window]
                    stats.restart_times.append(now)
                    logger.error(
                        "[supervisor:%s] '%s' crashed after %.1fs (restart #%d, "
                        "%d in the last %.0fs) -- restarting automatically:\n%s",
                        self.owner_label, name, ran_for, stats.restart_count,
                        len(stats.restart_times), window,
                        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                    )
                    if self._stopping:
                        stats.running = False
                        return
                    if len(stats.restart_times) > budget:
                        # Same failure over and over: restarting again just
                        # burns CPU and floods the log. Give up, mark stuck,
                        # and let the owner surface a manual restart.
                        stats.stuck = True
                        stats.running = False
                        logger.error(
                            "[supervisor:%s] '%s' crashed %d times in %.0fs -- giving up on "
                            "auto-restart; needs a manual restart (/api/pipeline/restart)",
                            self.owner_label, name, len(stats.restart_times), window,
                        )
                        if self._on_stuck is not None:
                            try:
                                self._on_stuck(name)
                            except Exception:
                                logger.debug(
                                    "[supervisor:%s] on_stuck callback for '%s' failed",
                                    self.owner_label, name, exc_info=True,
                                )
                        return
                    await asyncio.sleep(backoff.next_delay())

        task = asyncio.create_task(_run_supervised(), name=f"supervisor:{self.owner_label}:{name}")
        return task

    def stop(self) -> None:
        """Signal every supervised loop not to restart again. Does not
        cancel any task itself -- the caller still cancels each
        supervisor task exactly as it always cancelled the raw task, so
        an in-flight `await` inside the current attempt gets a prompt
        CancelledError instead of waiting for it to notice `_stopping`.
        """
        self._stopping = True
        for stats in self._stats.values():
            stats.running = False

    def snapshot(self) -> Dict[str, dict]:
        return {name: stats.to_dict() for name, stats in self._stats.items()}

    def all_healthy(self) -> bool:
        return all(s.running for s in self._stats.values())

    def any_stuck(self) -> bool:
        """True if any supervised loop blew its restart budget and is
        waiting on a manual restart."""
        return any(s.stuck for s in self._stats.values())

    def stuck_names(self) -> list:
        return [name for name, s in self._stats.items() if s.stuck]


# --------------------------------------------------------------------------- #
# Process-wide monitors (one instance for the whole app, not per-user --
# these observe the single shared event loop, so running one per user would
# just be N copies measuring the same thing).
# --------------------------------------------------------------------------- #

async def event_loop_lag_monitor(
    *, interval: float = 5.0, warn_threshold: float = 1.0, should_run: Callable[[], bool] = lambda: True,
) -> None:
    """Detects the event loop being blocked by synchronous/CPU-bound code
    anywhere in the process. A blocked loop delays every `await` in every
    task -- including watchdogs and scan loops -- but doesn't raise an
    exception anywhere, so no try/except in this codebase would ever
    catch it. This is the only way to actually see it happening.

    Schedules a sleep for `interval` seconds and compares the ACTUAL
    elapsed time against the requested one; the difference is scheduling
    lag caused by something else hogging the loop.
    """
    loop = asyncio.get_event_loop()
    last = loop.time()
    while should_run():
        await asyncio.sleep(interval)
        now = loop.time()
        lag = (now - last) - interval
        if lag > warn_threshold:
            logger.warning(
                "[event-loop-lag] Scheduling delay of %.2fs on a %.1fs tick -- "
                "the event loop was blocked by synchronous/CPU-bound code "
                "somewhere in this process during that window.",
                lag, interval,
            )
        last = now


async def asyncio_task_count_monitor(
    *, interval: float = 60.0, warn_threshold: int = 500,
    warn_growth: int = 200, should_run: Callable[[], bool] = lambda: True,
) -> None:
    """Lightweight async-task leak detector. Orphaned tasks (created and
    never awaited, cancelled, or allowed to finish -- e.g. a duplicate
    reconnect attempt that never gets cleaned up) don't crash anything;
    they just quietly accumulate until memory/scheduler overhead becomes
    a problem, with no single moment that looks like a failure. Tracking
    the total live-task count over time turns that invisible slow leak
    into an early, visible warning.
    """
    baseline: Optional[int] = None
    while should_run():
        await asyncio.sleep(interval)
        count = len(asyncio.all_tasks())
        if baseline is None:
            baseline = count
            continue
        growth = count - baseline
        if count > warn_threshold or growth > warn_growth:
            logger.warning(
                "[task-count] %d live asyncio tasks (baseline %d, +%d) -- "
                "possible task leak (a background task being created "
                "repeatedly without the old one ever finishing).",
                count, baseline, growth,
            )
