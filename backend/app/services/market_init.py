"""Market Initialization Manager — bounded, deterministic asset warm-up.

PROBLEM THIS REPLACES
----------------------
Previously, the *first* time any asset entered the scan loop's top-12
payout-sorted candidate window, `_evaluate_asset_signal()` called
`provider.get_candles()` for it directly, with zero coordination across
assets. At startup — or any time the candidate window rotated in a new
symbol as payouts moved — several "cold" assets could hit this at once.
Each one triggers a real historical-candle fetch inside
`PyQuotexProvider.get_candles()` (`_client.get_historical_candles(...)`)
over the *same* underlying Quotex connection. A concurrent burst of these
is what caused request congestion/timeouts.

Worse: `PyQuotexProvider.get_candles()` swallows its own history-fetch
exceptions internally (`except Exception: pass`) and returns whatever's
in its local cache instead of raising — so the *existing*
`error_recovery.execute_with_retry(..., "get_candles", ...)` wrapper
around it never even saw a failure to retry. The asset just silently
stayed under `IndicatorCache.get_enriched`'s `warm` bar (>=55 bars)
forever, rejected every single cycle with `"indicators not warm"` and no
path back to health. That's the "assets permanently skipped" failure
mode this module exists to close off.

WHAT THIS MODULE OWNS
----------------------
This manager owns *when* an asset's first candle-history load happens
and *how many* happen concurrently. It does not change `get_candles()`,
`get_enriched()`, strategies, risk, or execution in any way — it only
decides which assets `Orchestrator._candidate_assets()` is allowed to
hand to the scan loop. Trading strategy, signal generation, confidence
scoring, risk management, execution, position sizing, adaptive learning,
and strategy weighting are all untouched.

One instance per Orchestrator (i.e. per logged-in user) — constructed
and owned exactly like every other per-user component in
`Orchestrator.__init__`, so N users never share init state, workers,
queues, or asset records.

NOTE ON RETRY COUNT
--------------------
The spec this was built from reads: "Retry three times. Attempt 1 -> 1s,
Attempt 2 -> 2s, Attempt 3 -> 4s." That's read here as 3 *total* attempts
per asset (not 3 retries *after* an initial try, i.e. not 4 total) — the
backoff after attempt N only applies if attempt N+1 is going to happen,
so there's no wait after the final (3rd) attempt fails; it goes straight
to FAILED. `market_init.max_retries` in RuntimeSettings is the total
attempt count, documented as such at the field.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from .market import TIMEFRAMES
from ..engine.incremental import IndicatorCache
from ..engine.task_supervisor import TaskSupervisor
from ..logging_setup import log_event
from ..schemas import Candle

logger = logging.getLogger("market_init")

# Registered as its own RetryConfig/circuit-breaker in Orchestrator.__init__,
# deliberately separate from "get_candles" — see the comment at that
# registration site for why (background warm-up traffic must never trip
# the breaker the live scan loop's own candle fetches depend on, or
# vice versa).
MARKET_INIT_SERVICE_NAME = "market_init_get_candles"


class AssetInitState(str, Enum):
    UNKNOWN = "unknown"
    LOADING = "loading"
    READY = "ready"
    RETRYING = "retrying"
    FAILED = "failed"


@dataclass
class AssetInitRecord:
    symbol: str
    state: AssetInitState = AssetInitState.UNKNOWN
    history_requested: bool = False
    history_loaded: bool = False
    history_request_time: Optional[float] = None
    history_latency_ms: Optional[float] = None
    candle_count: int = 0
    expected_candle_count: int = 0
    warm: bool = False
    retries: int = 0
    last_retry: Optional[float] = None
    last_history_timestamp: Optional[float] = None
    ready_timestamp: Optional[float] = None
    initialization_duration_ms: Optional[float] = None
    worker_id: Optional[int] = None
    recovery_count: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "history_requested": self.history_requested,
            "history_loaded": self.history_loaded,
            "history_request_time": self.history_request_time,
            "history_latency_ms": self.history_latency_ms,
            "candle_count": self.candle_count,
            "expected_candle_count": self.expected_candle_count,
            "warm": self.warm,
            "retries": self.retries,
            "last_retry": self.last_retry,
            "last_history_timestamp": self.last_history_timestamp,
            "ready_timestamp": self.ready_timestamp,
            "initialization_duration_ms": self.initialization_duration_ms,
            "worker_id": self.worker_id,
            "recovery_count": self.recovery_count,
            "last_error": self.last_error,
        }


def _validate_candles(candles: List[Candle], timeframe: str, gap_multiplier: float,
                      gap_tolerance: int = 0) -> Tuple[bool, str]:
    """Same gap-detection convention as the admin Candle Dashboard: a gap
    bigger than `gap_multiplier`x the expected bar interval counts as a
    missing bar. Also rejects non-increasing/duplicate timestamps.

    RCA F12: a gap over the threshold used to fail the ENTIRE pull on the
    first occurrence. That is far too strict for this broker -- pyquotex
    serves history in chunks and never pads the holes a dropped chunk leaves,
    so a single lost chunk meant 119 of 120 perfectly good bars were thrown
    away, the asset went FAILED, and `_recovery_loop` re-attempted it every
    `recovery_interval_seconds` (60s) for as long as the hole stayed in the
    served window. `gap_tolerance` bounds how many such holes one pull may
    contain before it is genuinely unusable; 0 restores the old behaviour.

    Duplicate and non-increasing timestamps stay a HARD reject regardless of
    tolerance: those are not missing data, they are corrupt data, and no
    indicator downstream can be trusted on them.
    """
    if not candles:
        return False, "no candles returned"
    interval = TIMEFRAMES.get(timeframe, 60)
    prev_ts: Optional[float] = None
    gaps = 0
    worst_gap = 0.0
    for c in sorted(candles, key=lambda c: c.timestamp):
        if prev_ts is not None:
            delta = c.timestamp - prev_ts
            if delta <= 0:
                return False, f"non-increasing/duplicate timestamp at {c.timestamp}"
            if delta > interval * gap_multiplier:
                gaps += 1
                worst_gap = max(worst_gap, delta)
        prev_ts = c.timestamp
    if gaps > max(0, int(gap_tolerance)):
        return False, (
            f"{gaps} gap(s) exceed {gap_multiplier}x expected interval ({interval}s), "
            f"largest {worst_gap:.0f}s -- tolerance is {max(0, int(gap_tolerance))}"
        )
    return True, ""


def effective_min_bars(configured: int, observed: int,
                       absolute_floor: int = 55) -> int:
    """The bar count an asset should actually be certified against. RCA F11.

    `trading.min_candles_for_signal` defaults to 120, which is exactly this
    broker's history ceiling -- the loader in config.py even clamps anything
    higher back to 120 with the comment "which no asset can reach". So the
    READY gate sat precisely on the edge of what the data source can serve:
    an asset returning 119 bars never became READY, and `_candidate_assets()`
    filters READY-only, so the scan reported SCAN_CYCLE_NO_CANDIDATES forever
    with no error to explain it.

    This does NOT lower a quality bar on a whim. It only relaxes the gate to
    what the broker demonstrably served, and never below the absolute floor
    at which the indicator maths stops returning NaN. If the broker serves
    enough, the configured value is used unchanged.
    """
    configured = int(configured)
    observed = int(observed)
    floor = int(absolute_floor)
    if observed >= configured:
        return configured
    return max(floor, min(configured, observed))


def _top_reasons(reasons: List[str], limit: int = 3) -> List[str]:
    """Most common failure reasons, so the status panel can say WHY assets
    are failing instead of only how many."""
    counts: Dict[str, int] = {}
    for r in reasons:
        key = str(r)[:120]
        counts[key] = counts.get(key, 0) + 1
    return [f"{k} (x{v})" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


class MarketInitManager:
    def __init__(
        self,
        user_id: str,
        get_provider: Callable[[], object],   # returns the CURRENT services.market.MarketProvider
        indicators: IndicatorCache,
        candle_store,                  # services.candle_store.CandleStore
        error_recovery,                # engine.error_recovery.ErrorRecovery
        get_timeframe: Callable[[], str],
        get_settings: Callable[[], "MarketInitSettings"],  # noqa: F821 — forward ref, see config.py
        get_min_bars: Optional[Callable[[], int]] = None,  # trading.min_candles_for_signal
    ) -> None:
        self.user_id = user_id
        # MERGE FIX: this used to hold the MarketProvider *instance* handed
        # in at construction time. Orchestrator.switch_provider() replaces
        # self.provider with a brand-new object, so that reference went
        # stale on every provider swap -- this manager would then poll a
        # disconnected provider's `.connected` forever (never leaving Phase
        # 1) and fetch history from the discarded client, so no asset would
        # ever reach READY again and _candidate_assets() would return an
        # empty list permanently. Holding a getter instead means every read
        # sees whichever provider is current right now.
        self._get_provider = get_provider
        self._indicators = indicators
        self._candle_store = candle_store
        self._error_recovery = error_recovery
        self._get_timeframe = get_timeframe
        self._get_settings = get_settings
        # Warm-up must certify against the same bar count the scanner demands,
        # otherwise READY means "enough bars to compute" while the scan loop
        # means "enough bars to trust" and assets bounce between the two.
        self._get_min_bars = get_min_bars or (lambda: 200)

        self._records: Dict[str, AssetInitRecord] = {}
        self._active: Set[str] = set()   # queued or in-flight — duplicate protection
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._lock = asyncio.Lock()

        # MERGE ADDITION: this manager's own background tasks (worker pool,
        # recovery sweep, universe refresh) now run under the SAME
        # TaskSupervisor the Orchestrator's five core loops use. Before this
        # they were bare create_task()s: if one died, nothing restarted it
        # and nothing logged it -- a worker pool that silently shrank to
        # zero, or a recovery loop that stopped sweeping FAILED assets,
        # looks identical from the outside to "history never loads", which
        # is the exact failure this whole module exists to fix. Own
        # supervisor instance (not the Orchestrator's) so market-init crash
        # counts stay distinguishable from scan/watchdog crash counts in
        # /api/status.
        self._supervisor = TaskSupervisor(owner_label=f"market_init:{user_id}")
        self._workers: List[asyncio.Task] = []
        self._recovery_task: Optional[asyncio.Task] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        # Tracked so stop() cancels it too -- it was previously a bare
        # create_task that nothing held a reference to.
        self._startup_watch_task: Optional[asyncio.Task] = None
        self._stopping = False

        self._get_enabled: Optional[Callable[[], Awaitable[List[str]]]] = None
        self._initial_batch: Set[str] = set()
        self._startup_completed_at: Optional[float] = None

    # -- public API used by Orchestrator ------------------------------------ #
    def is_ready(self, symbol: str) -> bool:
        rec = self._records.get(symbol)
        return bool(rec and rec.state == AssetInitState.READY)

    def state_of(self, symbol: str) -> AssetInitState:
        rec = self._records.get(symbol)
        return rec.state if rec else AssetInitState.UNKNOWN

    def snapshot(self) -> dict:
        """Everything the ADMIN METRICS spec asks to track, per asset, plus
        a fleet-level summary — this is what the admin API endpoint reads."""
        by_state: Dict[str, int] = {}
        for rec in self._records.values():
            by_state[rec.state.value] = by_state.get(rec.state.value, 0) + 1
        return {
            "queue_length": self._queue.qsize(),
            "workers": len(self._workers),
            "startup_completed_at": self._startup_completed_at,
            "supervision": self._supervisor.snapshot(),
            "any_task_stuck": self._supervisor.any_stuck(),
            "by_state": by_state,
            # What the broker is ACTUALLY delivering, and what we're asking
            # for. Without these two numbers side by side, "47 failed" gives
            # no way to tell a dead connection from a threshold set higher
            # than the broker can supply -- which are opposite problems with
            # opposite fixes.
            "required_candles": int(self._get_min_bars()),
            "best_candle_count": max((rec.candle_count for rec in self._records.values()), default=0),
            "median_candle_count": (
                sorted(rec.candle_count for rec in self._records.values())[len(self._records) // 2]
                if self._records else 0
            ),
            "top_failure_reasons": _top_reasons(
                [rec.last_error for rec in self._records.values() if rec.last_error]
            ),
            "assets": {sym: rec.to_dict() for sym, rec in self._records.items()},
        }

    async def start(self, get_enabled_assets: Callable[[], Awaitable[List[str]]]) -> None:
        """Fire-and-forget from Orchestrator.start() — never blocks startup,
        matching this codebase's existing "connect in the background"
        pattern for the broker connection itself."""
        if self._supervisor_task is not None:
            return  # idempotent — calling start() twice must not spawn a second worker pool
        # Cleared here so a stop() -> start() cycle on the SAME instance
        # actually restarts. (Today registry.restart() builds a whole new
        # Orchestrator and therefore a new manager, so this is defensive --
        # but a manager that silently refuses to run is exactly the kind of
        # permanent-stuck failure this merge is meant to rule out.)
        self._stopping = False
        self._supervisor._stopping = False
        self._get_enabled = get_enabled_assets
        self._supervisor_task = self._supervisor.supervise(
            "market_init_run", self._run, is_active=lambda: not self._stopping,
        )

    def supervision_snapshot(self) -> dict:
        """Per-task health for this manager's own background tasks, same
        shape as the Orchestrator's supervisor snapshot -- surfaced via
        /api/market-init so a dead worker pool is visible, not inferred."""
        return self._supervisor.snapshot()

    async def stop(self) -> None:
        self._stopping = True
        self._supervisor.stop()
        tasks = list(self._workers)
        if self._recovery_task:
            tasks.append(self._recovery_task)
        if self._supervisor_task:
            tasks.append(self._supervisor_task)
        if self._startup_watch_task:
            tasks.append(self._startup_watch_task)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers = []
        self._recovery_task = None
        self._supervisor_task = None
        self._startup_watch_task = None

    async def ensure_enqueued(self, symbols: List[str]) -> None:
        """Idempotent — safe to call repeatedly with an overlapping or
        growing symbol list (e.g. every time the eligible-asset universe
        refreshes). Only ever schedules a symbol that has *never* been
        seen before (state UNKNOWN). A FAILED asset is deliberately NOT
        re-queued here — recovering it is the background recovery loop's
        job, on its own timer, so a symbol never has two independent code
        paths racing to schedule it (DUPLICATE PROTECTION)."""
        async with self._lock:
            newly_queued = 0
            for symbol in symbols:
                if symbol not in self._records:
                    self._records[symbol] = AssetInitRecord(symbol=symbol)
                rec = self._records[symbol]
                if symbol in self._active or rec.state != AssetInitState.UNKNOWN:
                    continue
                rec.state = AssetInitState.LOADING
                self._active.add(symbol)
                self._queue.put_nowait(symbol)
                newly_queued += 1
            if newly_queued:
                log_event(logger, logging.INFO, "QueueLength", user_id=self.user_id,
                          queue_length=self._queue.qsize(), newly_queued=newly_queued)

    async def handle_reconnect(self) -> None:
        """Called by Orchestrator after its own `_reconnect_with_backoff`
        succeeds. `PyQuotexProvider.disconnect()` drops every stream
        subscription (`_streamed`/`_stream_tick_cursor`/`_last_resync`) but
        keeps the in-memory candle cache, so previously-READY assets don't
        need a full cold re-fetch — just their subscription re-established
        and one verification pass, routed through the same bounded queue
        instead of all reconnecting at once (this is the "reload only
        stale assets, don't restart the entire pipeline" requirement)."""
        async with self._lock:
            stale = [s for s, rec in self._records.items()
                     if rec.state == AssetInitState.READY and s not in self._active]
            if not stale:
                return
            log_event(logger, logging.INFO, "RecoveryStarted", user_id=self.user_id,
                      reason="websocket_reconnect", asset_count=len(stale))
            for symbol in stale:
                rec = self._records[symbol]
                rec.state = AssetInitState.RETRYING
                rec.recovery_count += 1
                self._active.add(symbol)
                self._queue.put_nowait(symbol)

    # -- internals ------------------------------------------------------------ #
    async def _run(self) -> None:
        # PHASE 1 — no history requests before the connection is confirmed.
        while not self._stopping and not self._get_provider().connected:
            await asyncio.sleep(0.5)
        if self._stopping:
            return

        settings = self._get_settings()
        self._workers = [
            self._supervisor.supervise(
                f"worker_{i}", lambda i=i: self._worker(i),
                is_active=lambda: not self._stopping,
            )
            for i in range(max(1, settings.workers))
        ]
        for i in range(len(self._workers)):
            log_event(logger, logging.INFO, "WorkerStarted", user_id=self.user_id, worker_id=i)
        self._recovery_task = self._supervisor.supervise(
            "recovery", self._recovery_loop, is_active=lambda: not self._stopping,
        )

        # RELIABILITY FIX (found during the merge): the initial universe
        # fetch below used to be unguarded. If _get_enabled() raised here --
        # provider.get_assets() throwing on a flaky just-established
        # connection is the realistic case -- _run() died right there, the
        # refresh loop underneath never started, and no asset was ever
        # enqueued again. Workers stayed alive on an empty queue, nothing
        # crashed, nothing logged: permanently stuck with zero READY assets.
        # Retried instead, and a failure here now just falls through to the
        # periodic refresh loop, which retries every 10s anyway.
        for attempt in range(1, 4):
            try:
                assets = await self._get_enabled()
                self._initial_batch = set(assets)
                await self.ensure_enqueued(assets)
                self._startup_watch_task = asyncio.create_task(self._watch_startup_completion())
                break
            except Exception:
                logger.warning(
                    "[market_init] initial enabled-asset fetch failed (attempt %d/3) -- "
                    "the periodic refresh below will keep retrying", attempt, exc_info=True,
                )
                if self._stopping:
                    return
                await asyncio.sleep(2.0 * attempt)

        # Keep the eligible-asset universe fresh — a symbol that becomes
        # eligible after startup (payout crosses min_payout, market opens,
        # whitelist changes) gets the same bounded, deduplicated path in,
        # instead of falling back to the old direct/unbounded behavior the
        # first time the scan loop's top-12 window happens to rotate it in.
        while not self._stopping:
            await asyncio.sleep(10.0)
            try:
                assets = await self._get_enabled()
                await self.ensure_enqueued(assets)
            except Exception:
                logger.debug("[market_init] refreshing enabled-asset universe failed", exc_info=True)

    async def _watch_startup_completion(self) -> None:
        """STARTUP COMPLETION: fires once every asset in the *initial*
        batch has left LOADING/RETRYING/UNKNOWN (reached READY or FAILED).
        A one-time milestone — assets discovered later via rotation don't
        re-trigger it."""
        while not self._stopping:
            pending = [s for s in self._initial_batch
                       if self.state_of(s) in (AssetInitState.LOADING, AssetInitState.RETRYING, AssetInitState.UNKNOWN)]
            if not pending:
                break
            await asyncio.sleep(0.5)
        if self._stopping:
            return
        ready = sum(1 for s in self._initial_batch if self.is_ready(s))
        failed = sum(1 for s in self._initial_batch if self.state_of(s) == AssetInitState.FAILED)
        self._startup_completed_at = time.time()
        log_event(logger, logging.INFO, "InitializationCompleted", user_id=self.user_id,
                  total=len(self._initial_batch), ready=ready, failed=failed)

    async def _worker(self, worker_id: int) -> None:
        try:
            while not self._stopping:
                try:
                    symbol = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                try:
                    await self._init_one(symbol, worker_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # _init_one already catches everything internally; this
                    # is the belt-and-braces layer so a worker can never be
                    # taken out of the pool by one bad symbol. A pool that
                    # silently shrinks to zero looks exactly like "history
                    # never loads" from the outside.
                    logger.exception("[market_init] worker %d: unexpected error on %s",
                                     worker_id, symbol)
                finally:
                    self._queue.task_done()
        finally:
            log_event(logger, logging.INFO, "WorkerFinished", user_id=self.user_id, worker_id=worker_id)

    async def _retry_or_fail(self, rec: AssetInitRecord, symbol: str, attempt: int,
                              settings: "MarketInitSettings", reason: str) -> bool:  # noqa: F821
        """Returns True if the caller should attempt again, False if the
        asset is now FAILED and the caller should stop. Sets rec.last_error
        unconditionally so every failure path (timeout, exception,
        validation, not-warm) reports a reason, rather than relying on
        each call site to remember to set it individually."""
        rec.retries += 1
        rec.last_retry = time.time()
        rec.last_error = reason
        if attempt < max(1, settings.max_retries):
            backoff = settings.retry_backoff_base_seconds * (2 ** (attempt - 1))
            rec.state = AssetInitState.RETRYING
            log_event(logger, logging.INFO, "HistoryRetry", user_id=self.user_id, asset=symbol,
                      attempt=attempt, backoff_seconds=backoff, reason=reason)
            await asyncio.sleep(backoff)
            return True
        rec.state = AssetInitState.FAILED
        log_event(logger, logging.WARNING, "AssetFailed", user_id=self.user_id, asset=symbol,
                  retries=rec.retries, reason=reason)
        self._active.discard(symbol)
        return False

    async def _init_one(self, symbol: str, worker_id: int) -> None:
        settings = self._get_settings()
        rec = self._records[symbol]
        rec.worker_id = worker_id
        start = time.monotonic()
        timeframe = self._get_timeframe()

        try:
            for attempt in range(1, max(1, settings.max_retries) + 1):
                rec.history_requested = True
                rec.history_request_time = time.time()
                req_start = time.monotonic()
                log_event(logger, logging.INFO, "HistoryRequestStarted", user_id=self.user_id,
                          asset=symbol, attempt=attempt, worker_id=worker_id)
                try:
                    candles = await asyncio.wait_for(
                        self._error_recovery.execute_with_retry(
                            self._get_provider().get_candles, MARKET_INIT_SERVICE_NAME, symbol, timeframe,
                            settings.history_count,
                        ),
                        timeout=settings.history_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    log_event(logger, logging.WARNING, "HistoryRequestTimeout", user_id=self.user_id,
                              asset=symbol, attempt=attempt, worker_id=worker_id,
                              timeout_seconds=settings.history_timeout_seconds)
                    if not await self._retry_or_fail(rec, symbol, attempt, settings, "timeout"):
                        return
                    continue
                except Exception as exc:
                    logger.warning("[market_init] get_candles failed for %s (attempt %d): %s",
                                   symbol, attempt, exc)
                    reason = f"{type(exc).__name__}: {exc}"
                    if not await self._retry_or_fail(rec, symbol, attempt, settings, reason):
                        return
                    continue

                rec.history_latency_ms = (time.monotonic() - req_start) * 1000
                rec.candle_count = len(candles)
                rec.expected_candle_count = settings.history_count
                rec.last_history_timestamp = max((c.timestamp for c in candles), default=None)

                valid, reason = _validate_candles(
                    candles, timeframe, settings.gap_multiplier,
                    gap_tolerance=int(getattr(settings, "gap_tolerance", 0)),
                )
                if not valid:
                    log_event(logger, logging.WARNING, "HistoryValidationFailed", user_id=self.user_id,
                              asset=symbol, attempt=attempt, worker_id=worker_id,
                              candle_count=rec.candle_count, reason=reason)
                    if not await self._retry_or_fail(rec, symbol, attempt, settings, reason):
                        return
                    continue

                # Reuse the *real* warm check the scan loop itself uses for
                # every live evaluation — never a second, separately
                # -maintained threshold that could silently drift from it.
                # Certify against the SAME threshold the scan loop enforces.
                # Previously this used get_enriched's built-in 55-bar floor
                # while the scanner had no floor at all, so an asset could be
                # marked READY on 55 bars and then be evaluated on those same
                # 55 half-converged bars for the rest of the session.
                min_bars = max(55, int(self._get_min_bars()))
                _edf, warm = self._indicators.get_enriched(
                    f"{symbol}|{timeframe}", candles, min_bars=min_bars)
                if not warm:
                    # RCA F11: the configured gate sits exactly on the broker's
                    # history ceiling, so an asset serving one bar less never
                    # became READY and _candidate_assets() -- READY-only --
                    # reported SCAN_CYCLE_NO_CANDIDATES forever. Retry against
                    # what the broker actually served, never below the
                    # absolute floor at which the indicator maths works.
                    relaxed = effective_min_bars(min_bars, len(candles))
                    if relaxed < min_bars:
                        _edf, warm = self._indicators.get_enriched(
                            f"{symbol}|{timeframe}", candles, min_bars=relaxed)
                        if warm:
                            log_event(
                                logger, logging.WARNING, "HistoryGateRelaxed",
                                user_id=self.user_id, asset=symbol,
                                candle_count=len(candles),
                                configured_min_bars=min_bars, effective_min_bars=relaxed,
                                reason="broker served less history than the configured "
                                       "minimum -- certified against the observed depth",
                            )
                rec.warm = bool(warm)
                if not warm:
                    log_event(logger, logging.WARNING, "HistoryValidationFailed", user_id=self.user_id,
                              asset=symbol, attempt=attempt, worker_id=worker_id,
                              candle_count=rec.candle_count,
                              reason=f"indicators not warm: {rec.candle_count} bars < {min_bars} required")
                    if not await self._retry_or_fail(rec, symbol, attempt, settings, "indicators not warm"):
                        return
                    continue

                rec.history_loaded = True
                log_event(logger, logging.INFO, "HistoryRequestCompleted", user_id=self.user_id,
                          asset=symbol, attempt=attempt, worker_id=worker_id, candle_count=rec.candle_count)
                log_event(logger, logging.INFO, "AssetWarm", user_id=self.user_id,
                          asset=symbol, candle_count=rec.candle_count)

                # Best-effort live-stream confirmation only — PyQuotexProvider
                # tracks its own (asset, period) subscriptions in `_streamed`;
                # if that attribute isn't there (paper provider, or any future
                # provider), this can't observe the signal either way and does
                # NOT block readiness on something it can't actually see. See
                # the report's Risk Analysis section for why this is a soft
                # check rather than a hard gate.
                streamed_flag = getattr(self._get_provider(), "_streamed", None)
                if streamed_flag is not None:
                    period = TIMEFRAMES.get(timeframe, 60)
                    if (symbol, period) not in streamed_flag:
                        logger.debug("[market_init] %s not yet showing in provider._streamed after warm-up", symbol)

                rec.state = AssetInitState.READY
                rec.ready_timestamp = time.time()
                rec.initialization_duration_ms = (time.monotonic() - start) * 1000
                log_event(logger, logging.INFO, "AssetReady", user_id=self.user_id, asset=symbol,
                          worker_id=worker_id, duration_ms=round(rec.initialization_duration_ms, 1),
                          retries=rec.retries)
                try:
                    self._candle_store.upsert_many(symbol, timeframe, candles)
                except Exception:
                    pass  # candle persistence is best-effort here too, matching _evaluate_asset_signal
                self._active.discard(symbol)
                return
        except Exception as exc:  # never let a bug in this manager wedge a worker forever
            rec.state = AssetInitState.FAILED
            rec.last_error = f"internal: {type(exc).__name__}: {exc}"
            logger.exception("[market_init] unexpected error initializing %s", symbol)
            log_event(logger, logging.ERROR, "AssetFailed", user_id=self.user_id, asset=symbol,
                      retries=rec.retries, reason="internal_error")
            self._active.discard(symbol)

    async def _recovery_loop(self) -> None:
        while not self._stopping:
            settings = self._get_settings()
            await asyncio.sleep(max(1.0, settings.recovery_interval_seconds))
            if self._stopping:
                return
            try:
                async with self._lock:
                    failed = [s for s, rec in self._records.items()
                              if rec.state == AssetInitState.FAILED and s not in self._active]
                    if not failed:
                        continue
                    log_event(logger, logging.INFO, "RecoveryStarted", user_id=self.user_id,
                              reason="failed_asset_sweep", asset_count=len(failed))
                    for symbol in failed:
                        rec = self._records[symbol]
                        rec.state = AssetInitState.LOADING
                        rec.recovery_count += 1
                        self._active.add(symbol)
                        self._queue.put_nowait(symbol)
                log_event(logger, logging.INFO, "RecoveryCompleted", user_id=self.user_id, asset_count=len(failed))
            except Exception:
                logger.exception("[market_init] recovery sweep failed")
