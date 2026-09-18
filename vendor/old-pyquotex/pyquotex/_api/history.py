"""History/candle methods for Quotex.

This mixin is composed into Quotex via multiple inheritance. It uses
self.api, self.codes_asset, etc. -- all set up in Quotex.__init__ inside
pyquotex/stable_api.py.

PIPELINE
--------
    history/load request -> WebSocket
      -> api.py _on_message routes the response
      -> EventRegistry sets candles_ready_<asset> and, when the broker
         echoes an index, candles_ready_<asset>_<index>
      -> this module waits, correlates, validates, dedupes, merges
      -> prepare_candles() -> calculate_candles / process_candles_v2 /
         merge_candles (UNCHANGED downstream contract)

WHAT THIS MODULE GUARANTEES
---------------------------
  * Real broker data only. Nothing here interpolates, back-fills, or
    synthesises a candle. A gap is reported, never filled.
  * Bounded everything: workers, retries, chunk size, request rate,
    cache, metrics, and the event registry.
  * Request-scoped event cleanup in `finally`, including on timeout,
    exception and cancellation -- no orphan events, no registry leak.
  * Instance-scoped cache and metrics -- no cross-account contamination.
  * Trading logic untouched.

CORRELATION, AND THE ONE THING THAT COULD NOT BE VERIFIED OFFLINE
------------------------------------------------------------------
The producer in `pyquotex/api.py` fires the indexed event only inside
`if data.get("index") is not None:`. Whether Quotex actually echoes
`index` back on a `history/load` response could NOT be verified here --
that needs a live socket. The previous implementation waited *only* on
the indexed event, so if the broker does not echo the index, every batch
fetch times out and deep history silently returns nothing.

Rather than guess, `_fetch_historical_batch` now waits on the indexed
event AND the per-asset event concurrently, and validates correlation on
whatever arrives (see `validate_envelope`). If the index is echoed we get
exact correlation; if it is not, we still get the data and fall back to
asset correlation. A response that demonstrably belongs to a different
request is discarded and the wait continues within the remaining budget.
See "Remaining risks" in the accompanying report.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import Any, Callable

from pyquotex import expiration
from pyquotex._api._constants import DEFAULT_TIMEOUT, _request_counter
from pyquotex._api.history_config import (
    DEFAULT_HISTORY_CONFIG,
    HistoryConfig,
)
from pyquotex._api.history_validation import (
    CandleQualityReport,
    CircuitOpenError,
    HistoryCircuitBreaker,
    HistoryMetrics,
    PermanentHistoryError,
    TransientHistoryError,
    validate_and_clean,
    validate_envelope,
)
from pyquotex.utils import json_utils as json
from pyquotex.utils.cache import TTLCache
from pyquotex.utils.processor import (
    calculate_candles,
    merge_candles,
    process_candles_v2,
)

logger = logging.getLogger(__name__)

_EVENT_PREFIX = "candles_ready_"


class HistoryMixin:
    """Methods related to candle data, historical queries, and trade history."""

    # ------------------------------------------------------------------ #
    # Instance-scoped state. Created lazily so stable_api.Quotex.__init__
    # does not have to change -- important for any subclass overriding it.
    # ------------------------------------------------------------------ #
    @property
    def history_config(self) -> HistoryConfig:
        cfg = getattr(self, "_history_config", None)
        if cfg is None:
            cfg = DEFAULT_HISTORY_CONFIG
            self._history_config = cfg
        return cfg

    @history_config.setter
    def history_config(self, cfg: HistoryConfig) -> None:
        cfg.validate()
        self._history_config = cfg
        # Rebuild the instance state that depends on config.
        self._history_cache = None
        self._history_breaker = None
        self._history_throttle_s = cfg.throttle_seconds

    @property
    def history_metrics(self) -> HistoryMetrics:
        m = getattr(self, "_history_metrics", None)
        if m is None:
            m = HistoryMetrics(maxlen=self.history_config.metrics_latency_samples)
            self._history_metrics = m
        return m

    @property
    def _candle_cache(self) -> TTLCache:
        """INSTANCE-scoped, not module-scoped.

        This used to be a module-level ``_CANDLE_CACHE``, so two Quotex
        instances -- two accounts, or a demo and a live client in one
        process -- shared a single cache keyed only by
        (asset, period, offset, bucket). Account A's candles could be
        served to account B. Per-instance removes that entirely.
        """
        cache = getattr(self, "_history_cache", None)
        if cache is None:
            cfg = self.history_config
            cache = TTLCache(maxsize=cfg.cache_maxsize, ttl=cfg.cache_ttl)
            self._history_cache = cache
        return cache

    @property
    def _breaker(self) -> HistoryCircuitBreaker:
        b = getattr(self, "_history_breaker", None)
        if b is None:
            cfg = self.history_config
            b = HistoryCircuitBreaker(
                threshold=cfg.breaker_failure_threshold,
                cooldown=cfg.breaker_cooldown_seconds,
                enabled=cfg.breaker_enabled,
            )
            self._history_breaker = b
        return b

    @property
    def _history_throttle(self) -> float:
        """Current inter-request delay. Starts at the configured healthy
        value and only grows after failures."""
        val = getattr(self, "_history_throttle_s", None)
        if val is None:
            val = self.history_config.throttle_seconds
            self._history_throttle_s = val
        return val

    def _throttle_on_failure(self) -> None:
        cfg = self.history_config
        self._history_throttle_s = min(
            cfg.throttle_max_seconds,
            max(cfg.throttle_seconds, self._history_throttle * cfg.throttle_growth),
        )

    def _throttle_on_success(self) -> None:
        """Decay back toward the healthy value. Healthy traffic is never
        slowed beyond `throttle_seconds`."""
        cfg = self.history_config
        self._history_throttle_s = max(
            cfg.throttle_seconds, self._history_throttle * cfg.throttle_decay
        )

    def history_snapshot(self) -> dict[str, Any]:
        """Observability entry point. Read-only; safe to call any time."""
        live_events = None
        if self.api is not None:
            registry = getattr(self.api, "event_registry", None)
            if registry is not None and hasattr(registry, "event_count"):
                live_events = registry.event_count()
        return {
            "metrics": self.history_metrics.snapshot(),
            "circuit_breaker": self._breaker.state(),
            "throttle_seconds": round(self._history_throttle, 3),
            "cache_entries": len(self._candle_cache),
            "live_events": live_events,
        }

    def reset_history_metrics(self) -> None:
        self.history_metrics.reset()

    # ------------------------------------------------------------------ #
    # get_candles -- signature unchanged
    # ------------------------------------------------------------------ #
    async def get_candles(
            self,
            asset: str,
            end_from_time: float | None,
            offset: int,
            period: int,
            progressive: bool = False,
            timeout: int = DEFAULT_TIMEOUT,
            use_cache: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Retrieve candles for a specific asset.

        Parameters
        ----------
        use_cache:
            When ``True``, identical ``(asset, period, offset, candle_bucket)``
            requests within the cache TTL are served from THIS INSTANCE's
            memory. ``candle_bucket`` floors ``end_from_time`` to the candle
            period so live data is never served stale.
        """
        if self.api is None:
            return None

        if end_from_time is None:
            end_from_time = time.time()

        metrics = self.history_metrics

        cache_key: tuple[str, int, int, int] | None = None
        if use_cache and not progressive and period > 0:
            bucket = int(end_from_time // period)
            cache_key = (asset, period, offset, bucket)
            cached = self._candle_cache.get(cache_key)
            if cached is not None:
                metrics.incr("cache_hits")
                return cached
            metrics.incr("cache_misses")

        index = expiration.get_timestamp()
        event_name = f"{_EVENT_PREFIX}{asset}"

        # Shared-state reset kept for downstream compatibility:
        # prepare_candles() falls back to api.candles.candles_data when no
        # history is passed explicitly, and get_candle_v2 uses that path.
        self.api.candles.candles_data = None

        metrics.incr("requests_total")
        started = time.monotonic()

        if not self._breaker.allows():
            metrics.incr("circuit_rejections")
            logger.warning(
                "[history] circuit open -- skipping get_candles for %s (breaker=%s)",
                asset, self._breaker.state(),
            )
            return None

        history_data: Any = None
        try:
            # CLEAR -> REQUEST -> WAIT -> CONSUME -> CLEANUP.
            # The clear is what stops a *previous* response for this asset
            # (AsyncEvent keeps `_has_fired` set until reset) from being
            # returned instantly as if it answered this request.
            await self.api.event_registry.clear_event(event_name)

            await self.start_candles_stream(asset, period)
            await self.api.get_candles(asset, index, end_from_time, offset, period)

            try:
                history_data = await self.api.event_registry.wait_event(
                    event_name, timeout=timeout
                )
            except (TimeoutError, asyncio.TimeoutError):
                metrics.incr("timeouts")
                metrics.incr("requests_failed")
                self._breaker.record_failure()
                self._throttle_on_failure()
                logger.error(
                    "Timeout waiting for candles for %s after %ds", asset, timeout
                )
                logger.warning(
                    "[CANDLE_TRACE] wait_event TIMEOUT: asset=%s waited=%ds "
                    "final temp_status=%r subscriptions=%s -- this fires when "
                    "candles_ready_%s was never set, i.e. the history/load "
                    "response was dropped/misrouted rather than a slow server.",
                    asset, timeout, getattr(self.api, "_temp_status", None),
                    list(getattr(self.api, "_subscriptions", {}) or {}), asset,
                )
                return None
        except asyncio.CancelledError:
            # Cleanup still runs via `finally`, then propagate -- never
            # swallow cancellation.
            raise
        finally:
            # CLEANUP on every exit path. `candles_ready_<asset>` is a
            # fixed-name event shared with the live stream, so it is reset
            # rather than discarded -- discarding it would race with the
            # producer recreating it.
            with contextlib.suppress(Exception):
                await self.api.event_registry.clear_event(event_name)

        self._breaker.record_success()
        self._throttle_on_success()
        metrics.incr("requests_ok")
        metrics.observe_latency(time.monotonic() - started)

        # Pass the asset-specific history directly to avoid multi-asset
        # state races (never read back from api.candles.candles_data here).
        candles = self.prepare_candles(asset, period, history_data)

        if progressive:
            historical = self.api.historical_candles
            return historical.get("data", {}) if isinstance(historical, dict) else {}

        if cache_key is not None and candles:
            # TTL is the minimum of the configured TTL and one candle
            # period, so a cache entry can never outlive its candle.
            self._candle_cache.set(
                cache_key, candles, ttl=min(self._candle_cache.ttl, float(period))
            )

        return candles

    # ------------------------------------------------------------------ #
    # Batch fetch -- correlation + retry + adaptive timeout
    # ------------------------------------------------------------------ #
    async def _await_correlated_response(
            self,
            asset: str,
            index: Any,
            indexed_event: str,
            deadline: float,
    ) -> dict[str, Any]:
        """Wait for a response that actually belongs to THIS request.

        Waits on the indexed event and the per-asset event at once. The
        indexed one is exact; the per-asset one is the fallback for a
        broker that does not echo `index` back (in which case api.py never
        fires the indexed event at all, and waiting on it alone hangs until
        timeout -- the original integration bug).

        Anything whose envelope contradicts this request is discarded and
        the wait resumes within the remaining budget, so one worker can
        never consume another worker's response.

        Raises TransientHistoryError on timeout.
        """
        generic_event = f"{_EVENT_PREFIX}{asset}"
        metrics = self.history_metrics

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransientHistoryError(
                    f"timeout awaiting history response for {asset} index={index}"
                )

            indexed_task = asyncio.ensure_future(
                self.api.event_registry.wait_event(indexed_event, timeout=remaining)
            )
            generic_task = asyncio.ensure_future(
                self.api.event_registry.wait_event(generic_event, timeout=remaining)
            )
            sources = {indexed_task: indexed_event, generic_task: generic_event}
            payload = None
            source = None
            try:
                done, _pending = await asyncio.wait(
                    sources.keys(), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if task.cancelled():
                        continue
                    if task.exception() is not None:
                        continue
                    result = task.result()
                    if result is not None:
                        payload, source = result, sources[task]
                        break
            finally:
                # Cancel and drain losers immediately -- never leave a
                # pending waiter attached to an answered request.
                for task in sources:
                    if not task.done():
                        task.cancel()
                for task in sources:
                    with contextlib.suppress(BaseException):
                        await task

            if payload is None:
                raise TransientHistoryError(
                    f"timeout awaiting history response for {asset} index={index}"
                )

            ok, reason = validate_envelope(
                payload,
                expected_asset=asset,
                # Only enforce index correlation on the generic channel --
                # the indexed event is exact by construction.
                expected_index=index if source == generic_event else None,
            )
            if ok:
                return payload

            metrics.incr("correlation_mismatches")
            logger.debug(
                "[history] discarding uncorrelated response on %s for %s index=%s: %s",
                source, asset, index, reason,
            )
            # Reset the generic event so the stale payload does not satisfy
            # the next spin of this loop instantly.
            with contextlib.suppress(Exception):
                await self.api.event_registry.clear_event(generic_event)

    async def _fetch_historical_batch(
            self,
            asset: str,
            fetch_time: int,
            offset: int,
            period: int,
            index: int,
            timeout: int
    ) -> dict[str, Any] | None:
        """Low-level batch fetcher for a specific time point and index.

        Signature unchanged. Returns the raw response dict, or None if the
        chunk could not be fetched after all retries -- callers must treat
        None as "this range is missing", never as "there is no data here".
        """
        if self.api is None:
            return None

        cfg = self.history_config
        metrics = self.history_metrics
        event_name = f"{_EVENT_PREFIX}{asset}_{index}"

        payload = {
            "asset": asset,
            "index": index,
            "time": fetch_time,
            "offset": offset,
            "period": period,
        }
        ws_msg = f'42["history/load",{json.dumps_str(payload)}]'

        try:
            for attempt in range(1, cfg.max_retries + 1):
                if not self._breaker.allows():
                    metrics.incr("circuit_rejections")
                    logger.debug(
                        "[history] circuit open -- not sending %s@%s",
                        asset, fetch_time,
                    )
                    return None

                metrics.incr("requests_total")
                started = time.monotonic()
                attempt_timeout = cfg.attempt_timeout(timeout, attempt)

                try:
                    # CLEAR before every attempt so a late response to the
                    # previous attempt cannot be mistaken for this one's.
                    await self.api.event_registry.clear_event(event_name)

                    if cfg.log_healthy_requests:
                        logger.debug(
                            "[CANDLE_TRACE] history/load sent: asset=%s index=%s "
                            "time=%s offset=%s period=%s attempt=%d timeout=%.1f",
                            asset, index, fetch_time, offset, period,
                            attempt, attempt_timeout,
                        )

                    await self.api.send_websocket_request(ws_msg)

                    response = await self._await_correlated_response(
                        asset, index, event_name,
                        deadline=time.monotonic() + attempt_timeout,
                    )

                    # Envelope already validated inside the waiter.
                    self._breaker.record_success()
                    self._throttle_on_success()
                    metrics.incr("requests_ok")
                    metrics.observe_latency(time.monotonic() - started)
                    return response

                except asyncio.CancelledError:
                    raise
                except (PermanentHistoryError, CircuitOpenError) as exc:
                    if isinstance(exc, CircuitOpenError):
                        metrics.incr("circuit_rejections")
                        return None
                    # Corrupt payload: retrying gets the same corruption.
                    metrics.incr("requests_failed")
                    logger.warning(
                        "[history] permanent failure for %s index=%s: %s "
                        "-- skipping this chunk", asset, index, exc,
                    )
                    return None
                except (TransientHistoryError, TimeoutError, asyncio.TimeoutError) as exc:
                    metrics.incr("requests_failed")
                    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or \
                            "timeout" in str(exc).lower():
                        metrics.incr("timeouts")
                    self._breaker.record_failure()
                    self._throttle_on_failure()

                    if attempt >= cfg.max_retries:
                        logger.warning(
                            "Batch fetch timeout at %d (index %d) for %s "
                            "after %d attempts", fetch_time, index, asset, attempt,
                        )
                        return None

                    metrics.incr("retries")
                    delay = cfg.retry_delay(attempt, rand=random.random())
                    logger.debug(
                        "[history] retrying %s index=%s in %.2fs (attempt %d/%d): %s",
                        asset, index, delay, attempt, cfg.max_retries, exc,
                    )
                    await asyncio.sleep(delay)
                except Exception as exc:
                    # Unknown failure: treat as transient, but still bounded
                    # by max_retries.
                    metrics.incr("requests_failed")
                    self._breaker.record_failure()
                    self._throttle_on_failure()
                    logger.warning(
                        "[history] unexpected error fetching %s index=%s "
                        "(attempt %d/%d): %s",
                        asset, index, attempt, cfg.max_retries, exc, exc_info=True,
                    )
                    if attempt >= cfg.max_retries:
                        return None
                    metrics.incr("retries")
                    await asyncio.sleep(cfg.retry_delay(attempt, rand=random.random()))
            return None
        finally:
            # CLEANUP: this event is request-scoped -- one per (asset,
            # index) with index from a monotonic counter -- so it must be
            # DISCARDED, not merely reset, or the registry grows without
            # bound for the life of the process. Runs on success, failure,
            # exception and cancellation alike.
            with contextlib.suppress(Exception):
                registry = self.api.event_registry
                if hasattr(registry, "discard_event"):
                    await registry.discard_event(event_name)
                else:  # older registry without discard support
                    await registry.clear_event(event_name)

    def _parse_historical_candles(
            self, raw_data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Standardizes raw candle data into a uniform list of dicts.

        Signature and return shape unchanged. Now drops malformed entries
        instead of raising -- a single bad element used to take out the
        whole worker (and, through `asyncio.gather`, the entire deep-history
        call) via an unguarded `float()`/`int()`.

        Still returns ONLY real broker candles: nothing is fabricated for
        entries that fail to parse.
        """
        if not isinstance(raw_data, dict):
            return []
        raw_candles = raw_data.get("data") or raw_data.get("candles") or []
        if not raw_candles:
            return []

        period = 0
        with contextlib.suppress(Exception):
            period = int(raw_data.get("period") or 0)

        cfg = self.history_config
        cleaned, report = validate_and_clean(
            raw_candles,
            period,
            min_plausible_timestamp=cfg.min_plausible_timestamp,
            future_tolerance_seconds=cfg.future_tolerance_seconds,
            spacing_tolerance=cfg.spacing_tolerance,
            gap_multiplier=cfg.gap_multiplier,
        )
        self.history_metrics.absorb(report)
        if report.rejected or report.duplicates_conflicting:
            logger.debug("[history] candle quality: %s", report.to_dict())
        return cleaned

    # ------------------------------------------------------------------ #
    # get_historical_candles -- max_workers now defaults to config
    # ------------------------------------------------------------------ #
    # https://t.me/pyquotex/1/16064
    # https://github.com/usmanch96/quotex-historical-data
    async def get_historical_candles(
            self,
            asset: str,
            amount_of_seconds: int,
            period: int,
            timeout: int = DEFAULT_TIMEOUT,
            max_workers: int | None = None,
            progress_callback: Callable[[int, int, int, str], None] | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve extensive historical candle data.

        The total range is split into contiguous blocks, one per worker;
        each worker walks its block backwards sequentially so there are no
        gaps within a block and no overlap between blocks.

        ``max_workers=None`` uses ``HistoryConfig.max_workers``; an explicit
        value overrides it and is clamped to [1, 32]. The previous hardcoded
        default of ``5`` is preserved as the config default, so existing
        callers see identical concurrency.

        Returns real broker candles only, sorted by time. If a chunk fails
        after all retries the range it covered is logged and simply absent
        -- never padded, never duplicated, never faked.
        """
        if self.api is None:
            return []
        if period <= 0:
            raise ValueError("period must be a positive number of seconds")
        if amount_of_seconds <= 0:
            return []

        cfg = self.history_config
        workers = cfg.resolve_workers(max_workers)
        metrics = self.history_metrics

        current_time = int(time.time())
        amount_of_seconds = int(amount_of_seconds)
        target_start_time = current_time - amount_of_seconds

        # One request covers this many seconds of history.
        chunk_seconds = max(period, period * cfg.candles_per_chunk)
        # Never spawn more workers than there is range to divide, or a
        # worker gets a zero-width block and just occupies a slot.
        workers = max(1, min(workers, max(1, amount_of_seconds // chunk_seconds)))
        block_size = max(1, amount_of_seconds // workers)

        semaphore = asyncio.Semaphore(workers)
        missing_ranges: list[tuple[int, int]] = []

        def _safe_progress(*args: Any) -> None:
            """Progress reporting must never take down a worker and must
            never block the loop. A callback that raises is isolated here."""
            if progress_callback is None:
                return
            try:
                progress_callback(*args)
            except Exception:
                logger.debug("[history] progress_callback raised", exc_info=True)

        async def worker(start_t: int, end_t: int, worker_id: int) -> list[dict[str, Any]]:
            worker_candles: dict[int, dict[str, Any]] = {}
            worker_label = f"Worker-{worker_id}"
            async with semaphore:
                oldest_t = start_t
                # Hard iteration ceiling: even if every request fails and
                # the cursor only ever moves by chunk_seconds, this bounds
                # the loop. Termination no longer depends on broker
                # behaviour at all.
                max_iterations = (max(1, start_t - end_t) // chunk_seconds) + 8
                iterations = 0

                while oldest_t > end_t and iterations < max_iterations:
                    iterations += 1
                    # Monotonic counter so parallel workers and back-to-back
                    # iterations in one worker never share an index --
                    # prevents one worker consuming another's response.
                    index = next(_request_counter)

                    batch_data = await self._fetch_historical_batch(
                        asset, oldest_t, chunk_seconds, period, index, timeout
                    )

                    if not batch_data:
                        metrics.incr("chunks_abandoned")
                        missing_ranges.append(
                            (max(end_t, oldest_t - chunk_seconds), oldest_t)
                        )
                        oldest_t -= chunk_seconds
                        continue

                    new_batch = self._parse_historical_candles(batch_data)
                    if not new_batch:
                        metrics.incr("chunks_abandoned")
                        missing_ranges.append(
                            (max(end_t, oldest_t - chunk_seconds), oldest_t)
                        )
                        oldest_t -= chunk_seconds
                        continue

                    batch_times = []
                    for c in new_batch:
                        ts = c["time"]
                        if end_t <= ts <= start_t:
                            worker_candles[ts] = c
                            batch_times.append(ts)

                    if not batch_times:
                        oldest_t -= chunk_seconds
                        continue

                    new_oldest = min(batch_times)

                    _safe_progress(
                        start_t - new_oldest,
                        start_t - end_t,
                        len(worker_candles),
                        worker_label,
                    )

                    # The cursor MUST move backwards every iteration or this
                    # loop never terminates.
                    if new_oldest >= oldest_t:
                        oldest_t -= chunk_seconds
                    else:
                        oldest_t = new_oldest

                    # Adaptive throttle: healthy traffic pays only the
                    # configured base delay; it grows solely after failures.
                    if self._history_throttle > 0:
                        await asyncio.sleep(self._history_throttle)

                if iterations >= max_iterations and oldest_t > end_t:
                    logger.warning(
                        "[history] %s hit the iteration ceiling for %s with "
                        "range [%d, %d] unreached -- returning partial data "
                        "rather than looping.", worker_label, asset, end_t, oldest_t,
                    )
                    missing_ranges.append((end_t, oldest_t))

            return list(worker_candles.values())

        await self.start_candles_stream(asset, period)

        tasks: list[asyncio.Task] = []
        for i in range(workers):
            s = current_time - (i * block_size)
            e = max(target_start_time, s - block_size)
            if s <= e:
                continue
            tasks.append(asyncio.ensure_future(worker(s, e, i)))

        if not tasks:
            return []

        try:
            # return_exceptions=True: one worker failing must not discard
            # every other worker's already-fetched, perfectly good candles.
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # CANCELLATION SAFETY: cancel children, await them so no task is
            # left orphaned, reclaim their request-scoped events, re-raise.
            # Shutdown leaves nothing behind.
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                registry = self.api.event_registry
                if hasattr(registry, "discard_events_with_prefix"):
                    await registry.discard_events_with_prefix(
                        f"{_EVENT_PREFIX}{asset}_"
                    )
            raise

        all_candles: dict[int, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.warning(
                    "[history] a worker for %s failed: %s -- keeping the "
                    "candles the other workers did retrieve", asset, result,
                )
                continue
            for c in result:
                all_candles[c["time"]] = c

        if missing_ranges:
            merged = sorted(missing_ranges)
            logger.warning(
                "[history] %s: %d range(s) could not be retrieved after "
                "retries and are ABSENT from the result (not filled): %s",
                asset, len(merged),
                ", ".join(f"[{a}..{b}]" for a, b in merged[:10])
                + (" ..." if len(merged) > 10 else ""),
            )

        return sorted(all_candles.values(), key=lambda x: x["time"])

    async def get_candles_deep(
            self, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Deprecated alias for get_historical_candles."""
        logger.warning(
            "get_candles_deep is deprecated, "
            "use get_historical_candles instead."
        )
        return await self.get_historical_candles(*args, **kwargs)

    async def get_history_line(
            self,
            asset: str,
            end_from_time: float,
            offset: int,
            timeout: int = DEFAULT_TIMEOUT
    ) -> dict[str, Any] | None:
        """Retrieves historical price line data for an asset."""
        if self.api is None:
            return None

        index = expiration.get_timestamp()
        self.api.current_asset = asset
        # Reset to None (not {}) so the poll loop below can detect arrival.
        # An empty dict is a valid response; None is the sentinel for
        # "not yet received".
        self.api.historical_candles = None
        await self.start_candles_stream(asset)
        await self.api.get_history_line(
            self.codes_asset[asset], index, end_from_time, offset
        )
        # TODO(refactor/architecture Phase 2.7): polling here cannot be migrated
        # to SlotRegistry until we identify the WS event that should populate
        # self.api.historical_candles. No producer exists in
        # pyquotex/api.py:_on_message, so this method currently only exits via
        # the timeout branch below. Same situation as edit_practice_balance
        # and sell_option -- investigate WS traffic when calling get_history_line
        # to find the correct producer event.
        # CONFIRMED STILL TRUE during the history-pipeline audit: grepping for
        # `historical_candles` finds readers but no writer in the message
        # handler. Left as-is deliberately -- inventing a producer would be
        # guessing at the wire format, which this refactor refuses to do.
        start_time = time.time()
        while await self.check_connect() and self.api.historical_candles is None:
            if time.time() - start_time > timeout:
                logger.error(
                    "Timeout waiting for history line data for %s.",
                    asset
                )
                return None
            await asyncio.sleep(0.2)
        return self.api.historical_candles

    async def get_candle_v2(
            self, asset: str, period: int, timeout: int = DEFAULT_TIMEOUT
    ) -> list[dict[str, Any]] | None:
        """Retrieves candles using the v2 API path."""
        if self.api is None:
            return None

        # Reset the slot AND the data dict -- both serve as sentinels.
        self.api.candle_v2_data[asset] = None
        self.api.slots.release_candle_v2(asset)
        await self.start_candles_stream(asset, period)
        try:
            await self.api.slots.candle_v2(asset).wait(timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.error(
                "Timeout waiting for get_candle_v2 data for %s.",
                asset
            )
            return None
        candles = self.prepare_candles(asset, period)
        return candles

    def prepare_candles(
            self,
            asset: str,
            period: int,
            history: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Prepare candles data for a specified asset.

        DOWNSTREAM CONTRACT UNCHANGED: still calculate_candles ->
        process_candles_v2 -> merge_candles, in that order, with the same
        arguments. The history pipeline's job is to hand this function
        clean, real candles -- not to change what it does with them.
        """
        if self.api is None:
            return []

        # Use provided history if available (from event response),
        # otherwise fallback to shared state
        history_data = (
            history if history is not None else self.api.candles.candles_data
        )
        candles_data = calculate_candles(history_data, period)
        candles_v2_data = process_candles_v2(
            self.api.candle_v2_data, asset, candles_data
        )
        new_candles = merge_candles(candles_v2_data)

        return new_candles

    async def get_trader_history(
            self, account_type: int, page_number: int
    ) -> dict[str, Any]:
        """Retrieves trade history for a specific account and page."""
        if self.api:
            return await self.api.get_trader_history(account_type, page_number)
        return {}


__all__ = ["HistoryMixin", "CandleQualityReport"]
