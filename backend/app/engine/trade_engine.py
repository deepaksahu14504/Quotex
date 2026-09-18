"""Per-user trade execution engine.

Problem this solves: `execute_signal` used to be "check status -> await
broker call -> mark executed", called directly both from the auto-scan loop
and from the manual `/api/signals/{id}/execute` REST endpoint. Those two
call sites run as separate asyncio tasks, so nothing stopped them (or two
rapid manual taps, or two signals on the same asset) from both passing the
"not already executed" / "no active trade on this asset" checks before
either one finished placing its order -- a classic check-then-act race that
can double-order a real account.

The fix is a single-consumer queue per user: every execution request
(whatever triggered it) goes through `submit()`, which enqueues it and waits
for the result. Exactly one coroutine (`_run`) ever calls the actual
order-placing executor at a time, for a given user -- so the checks inside
that executor (signal still valid? asset already has an open trade?) are
race-free by construction, no separate lock object needed. Duplicate
submissions for the same signal while it's already queued/in-flight are
coalesced (second caller gets `None`) instead of being queued twice.

--------------------------------------------------------------------------
Bug fix (2026-08) -- the scan loop hung behind a wedged broker order.

Both awaits on the execution path were unbounded:

    async def submit(...):  return await fut                 # no timeout
    async def _run(...):    await self._executor(signal_id)   # no timeout

A pyquotex order call that hangs (rather than raising) therefore froze
`_run`, which never resolved `fut`, which blocked `submit()`, which blocked
`Orchestrator.execute_signal()`, which blocked `_scan_once()`, which stopped
`_scan_loop` from ever updating `health["last_scan_at"]`. The task object was
still alive -- awaiting, not dead -- so TaskSupervisor correctly reported
"all background loops running, no restarts" while the scanner had not
completed a cycle in minutes. That combination is exactly what production
showed.

How the timeout is handled matters more than the timeout itself:

  * The in-flight order task is NEVER cancelled. `asyncio.wait_for` would
    cancel it, and cancelling mid-`buy()` is precisely the state where you
    cannot tell whether the broker accepted the order. Instead the task is
    left running with a done-callback, so if it does eventually return, the
    trade is still booked correctly.
  * There is NO automatic retry. A timed-out order is recorded as
    UNRECONCILED, not retried -- retrying an order that may already be live
    is how one signal becomes two positions.
  * While an order is unreconciled the engine REFUSES new executions and
    reports `blocked`. Trading stops until the stuck call resolves or the
    operator intervenes. Refusing to trade is the safe failure direction;
    continuing to fire orders past a call whose outcome is unknown is not.
  * `submit_background()` lets the scan loop hand a signal over without
    awaiting the broker at all. Serialization still happens inside `_run`,
    so nothing about ordering or dedup changes -- the scan cycle simply
    stops being coupled to broker latency.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Set

logger = logging.getLogger(__name__)

# A broker order that has not returned in this long is treated as
# outcome-unknown. Generous on purpose: this is a safety net for a wedged
# connection, not a latency SLA.
#
# IMPORTANT — this MUST stay above the provider's own internal budget.
# pyquotex's buy() now spends at most 3 x PREPARE_TIMEOUT (10s) before the
# order is transmitted, plus BUY_CONFIRM_TIMEOUT (30s) waiting for the
# acknowledgement: ~60s worst case, after which it returns a DEFINITE answer
# ("Timeout", or "no order was sent"). If this outer guard fires first it
# converts that definite answer into an "outcome unknown" that blocks trading
# and needs manual reconciliation -- strictly worse information. 75s leaves
# headroom for the inner budget to resolve and report first.
ORDER_TIMEOUT_SECONDS = 90.0

# How long a caller of submit() (i.e. the manual Enter Trade endpoint) waits
# for its result before giving up.
#
# BUG FIX (2026-08): this was 60s while ORDER_TIMEOUT_SECONDS is 75s and the
# orchestrator's own place_order bound is 70s -- so the CALLER gave up first,
# 10-15s before the layer underneath had reached a verdict. Pressing Enter
# Trade therefore reported the vague "the execution queue refused it" while the
# order was still perfectly alive and being placed. The rule is the same one
# that governs the rest of this ladder: an outer wait must outlast every inner
# one, or it converts a definite answer into a guess.
SUBMIT_WAIT_SECONDS = 105.0


class OrderUnreconciled(RuntimeError):
    """The broker call did not return in time. Whether the order was
    accepted is UNKNOWN -- it must not be retried automatically."""


@dataclass
class _Request:
    signal_id: str
    future: "asyncio.Future"


class TradeEngine:
    def __init__(
        self,
        executor: Callable[[str], Awaitable[Optional[object]]],
        *,
        order_timeout: float = ORDER_TIMEOUT_SECONDS,
        on_unreconciled: Optional[Callable[[str], None]] = None,
        allow_duplicates: Optional[Callable[[], bool]] = None,
        block_on_unknown: Optional[Callable[[], bool]] = None,
    ) -> None:
        """`executor(signal_id)` performs the actual broker call + booking;
        supplied by the Orchestrator so this class has no broker/risk
        knowledge of its own -- just serialization, dedup, and the
        outcome-unknown guard.

        `on_unreconciled(signal_id)` is an optional synchronous hook the
        Orchestrator uses to pause trading and raise an alert."""
        self._executor = executor
        self._order_timeout = float(order_timeout)
        self._on_unreconciled = on_unreconciled
        # risk.allow_duplicate_orders -- read live so the toggle takes effect
        # without a restart.
        self._allow_duplicates = allow_duplicates or (lambda: False)
        # risk.block_trading_on_unknown_order. Read live so the toggle works
        # without a restart, and so turning it OFF immediately releases an
        # order that is already holding the queue.
        self._block_on_unknown = block_on_unknown or (lambda: True)
        self._queue: "asyncio.Queue[_Request]" = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._inflight: Set[str] = set()
        self._dup_counts: dict = {}

        # Outcome-unknown state. While set, no new order may be placed.
        self.unreconciled_signal_id: Optional[str] = None
        self.unreconciled_since: Optional[float] = None
        self.last_error: Optional[str] = None
        self.executed_count = 0
        self.timeout_count = 0
        # Why the most recent submission was refused. "Refused" and "tried and
        # failed" are different events with different fixes, and the endpoint
        # could not tell them apart from a bare None.
        self.last_refusal: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="trade-engine")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        while not self._queue.empty():
            req = self._queue.get_nowait()
            if not req.future.done():
                req.future.set_result(None)
        self._inflight.clear()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def blocked(self) -> bool:
        """True while an order's outcome is unknown AND the operator has asked
        for that to stop trading. With risk.block_trading_on_unknown_order off,
        the unknown order is still recorded and still visible in System Status
        -- it simply no longer gates anything."""
        return self.unreconciled_signal_id is not None and self._block_on_unknown()

    def status(self) -> dict:
        return {
            "running": bool(self._task and not self._task.done()),
            "queued": self._queue.qsize(),
            "inflight": sorted(self._inflight),
            "blocked": self.blocked,
            "blocking_enabled": self._block_on_unknown(),
            "unreconciled_signal_id": self.unreconciled_signal_id,
            "unreconciled_age_seconds": (
                round(time.time() - self.unreconciled_since, 1)
                if self.unreconciled_since else None
            ),
            "executed_count": self.executed_count,
            "timeout_count": self.timeout_count,
            "order_timeout_seconds": self._order_timeout,
            "last_error": self.last_error,
        }

    def clear_unreconciled(self, reason: str = "cleared by operator") -> bool:
        """Explicit operator acknowledgement that the stuck order has been
        checked against the broker. Deliberately manual -- the engine will not
        clear this on its own for an order it never saw resolve."""
        if not self.blocked:
            return False
        logger.warning(
            "[trade-engine] unreconciled order %s cleared: %s",
            self.unreconciled_signal_id, reason,
        )
        self.unreconciled_signal_id = None
        self.unreconciled_since = None
        return True

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #
    def _reject_reason(self, signal_id: str) -> Optional[str]:
        if self.blocked:
            return (f"execution blocked: order {self.unreconciled_signal_id} has an unknown "
                    f"outcome and must be checked against the broker before trading resumes")
        if signal_id in self._inflight and not self._allow_duplicates():
            return ("duplicate request (already queued or in flight) — enable "
                    "risk.allow_duplicate_orders to permit this")
        return None

    def _enqueue(self, signal_id: str) -> Optional["asyncio.Future"]:
        reason = self._reject_reason(signal_id)
        if reason:
            self.last_refusal = reason
            logger.info("[trade-engine] execute request for %s refused -- %s", signal_id, reason)
            return None
        self.last_refusal = None
        if self._task is None or self._task.done():
            self.start()
        # A set cannot represent "two in flight for this id", so with
        # duplicates enabled the id is tracked with a counter instead --
        # otherwise the first completion would clear the slot while a second
        # order was still running.
        if signal_id in self._inflight:
            self._dup_counts[signal_id] = self._dup_counts.get(signal_id, 1) + 1
        self._inflight.add(signal_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Request(signal_id, fut))
        return fut

    async def submit(self, signal_id: str, *, wait: float = SUBMIT_WAIT_SECONDS) -> Optional[object]:
        """Enqueue and wait for the outcome. Used by the manual REST path,
        where the caller genuinely wants the resulting trade back.

        BUG FIX: this used to be a bare `await fut` -- an unbounded wait that
        turned a wedged broker call into a wedged caller (and, via the scan
        loop, a wedged scanner)."""
        fut = self._enqueue(signal_id)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=wait)
        except asyncio.TimeoutError:
            # The queue worker keeps ownership of the request; we just stop
            # waiting on it. The underlying order is never cancelled.
            self.last_refusal = (
                f"still in flight after {wait:.0f}s — the order was NOT cancelled and may still "
                f"be placed. Check Order execution in System Status before retrying."
            )
            logger.warning("[trade-engine] caller stopped waiting on %s after %.0fs", signal_id, wait)
            return None
        finally:
            # Only release the dedup slot if the request has actually finished.
            # Discarding it unconditionally (as this used to) meant a caller
            # that timed out immediately re-opened the door to a SECOND
            # submission for a signal whose first order was still in flight.
            if fut.done():
                self._release(signal_id)

    def submit_background(self, signal_id: str) -> bool:
        """Enqueue WITHOUT waiting. Used by the auto-scan loop, which never
        used the return value anyway -- awaiting it only coupled the scan
        cycle to broker latency. Serialization and dedup are unchanged; they
        live in `_run`, not in the caller."""
        fut = self._enqueue(signal_id)
        if fut is None:
            return False
        fut.add_done_callback(lambda f, sid=signal_id: self._background_done(f, sid))
        return True

    def _release(self, signal_id: str) -> None:
        """Release one in-flight slot for this signal, keeping the rest."""
        n = self._dup_counts.get(signal_id, 1)
        if n > 1:
            self._dup_counts[signal_id] = n - 1
            return
        self._dup_counts.pop(signal_id, None)
        self._inflight.discard(signal_id)

    def _background_done(self, fut: "asyncio.Future", signal_id: str) -> None:
        """Nobody awaits a background submission, so its exception must be
        consumed here -- otherwise asyncio logs 'Future exception was never
        retrieved' for every timed-out order, burying the real error."""
        self._release(signal_id)
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.warning("[trade-engine] background execution of %s ended: %s", signal_id, exc)

    # ------------------------------------------------------------------ #
    # Single consumer
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        while True:
            req = await self._queue.get()
            try:
                if self.blocked:
                    if not req.future.done():
                        req.future.set_result(None)
                    continue
                await self._execute_one(req)
            except asyncio.CancelledError:
                if not req.future.done():
                    req.future.set_result(None)
                raise
            except Exception as exc:  # never let one bad order kill the queue
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Trade execution failed for signal %s", req.signal_id)
                if not req.future.done():
                    req.future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _execute_one(self, req: _Request) -> None:
        order = asyncio.ensure_future(self._executor(req.signal_id))
        done, _pending = await asyncio.wait({order}, timeout=self._order_timeout)

        if order in done:
            exc = order.exception()
            if exc is not None:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if not req.future.done():
                    req.future.set_exception(exc)
                return
            self.executed_count += 1
            if not req.future.done():
                req.future.set_result(order.result())
            return

        # --- Timed out. The order is STILL RUNNING and is NOT cancelled. ---
        self.timeout_count += 1
        self.unreconciled_signal_id = req.signal_id
        self.unreconciled_since = time.time()
        self.last_error = f"order {req.signal_id} did not return in {self._order_timeout:.0f}s"
        logger.error(
            "[trade-engine] ORDER OUTCOME UNKNOWN: signal=%s did not return within %.0fs. "
            "The call was NOT cancelled and will NOT be retried -- it may already be live at the "
            "broker. New executions are blocked until this resolves or is cleared. "
            "Check the account manually.",
            req.signal_id, self._order_timeout,
        )
        order.add_done_callback(lambda t, sid=req.signal_id: self._late_order_finished(t, sid))
        if self._on_unreconciled:
            try:
                self._on_unreconciled(req.signal_id)
            except Exception:
                logger.exception("[trade-engine] on_unreconciled hook failed")
        if not req.future.done():
            req.future.set_exception(OrderUnreconciled(self.last_error))

    def _late_order_finished(self, task: "asyncio.Task", signal_id: str) -> None:
        """The slow order eventually came back. It was allowed to finish, so
        whatever it booked is correct -- record the outcome and unblock."""
        if task.cancelled():
            logger.error("[trade-engine] unreconciled order %s was cancelled -- outcome still UNKNOWN",
                         signal_id)
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("[trade-engine] unreconciled order %s finally failed: %s -- "
                           "no order was placed, resuming", signal_id, exc)
        else:
            self.executed_count += 1
            logger.warning("[trade-engine] unreconciled order %s finally completed late: %r -- "
                           "the trade is booked, resuming", signal_id, task.result())
        if self.unreconciled_signal_id == signal_id:
            self.unreconciled_signal_id = None
            self.unreconciled_since = None
