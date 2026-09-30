"""Live candle streaming for the market chart.

One task per (asset, timeframe) pair, shared by every client watching that
pair. Ten browser tabs on EUR/USD 15m produce one fetch loop, not ten, and no
new connection to the broker -- the streamer reuses the orchestrator's existing
provider instance, which is already the single source of truth for candles.

Why it is event-driven rather than polled
-----------------------------------------
``PyQuotexProvider`` sets ``new_data_event`` whenever a newer tick arrives
(``market.py:1286``). Waiting on that event means an update is emitted when the
market actually moved, not on a fixed cadence that either lags behind or wastes
calls when nothing happened. A timeout is still applied so a quiet market
cannot leave the chart permanently stale if an event is ever missed, and a
minimum interval keeps a fast market from broadcasting on every single tick.

What it does NOT do
-------------------
It does not generate signals, evaluate strategies, touch risk settings, place
orders, or open a second market connection. It reads candles and forwards them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .chart_service import build_chart_payload

logger = logging.getLogger("app.chart_stream")

#: Shortest time between two broadcasts for the same pair. A fast market can
#: tick many times a second; the chart cannot usefully draw more than this.
DEFAULT_MIN_INTERVAL = 1.0

#: How long to wait for a data event before refreshing anyway. A backstop for
#: a missed event, not the primary trigger.
DEFAULT_IDLE_TIMEOUT = 5.0

#: How many candles to re-read per update. The chart only needs the tail: the
#: candle that just closed and the one still forming.
TAIL_CANDLES = 3


class ChartStreamer:
    """Fans live candle updates out to whoever is watching each pair."""

    def __init__(
        self,
        user_id: str,
        get_candles: Callable[[str, str, int], Awaitable[List[Any]]],
        broadcast: Callable[[str, Dict[str, Any]], Awaitable[None]],
        data_event: Optional[asyncio.Event] = None,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self.user_id = user_id
        self._get_candles = get_candles
        self._broadcast = broadcast
        self._data_event = data_event
        self._min_interval = min_interval
        self._idle_timeout = idle_timeout

        self._tasks: Dict[Tuple[str, str], asyncio.Task] = {}
        self._watchers: Dict[Tuple[str, str], int] = {}
        self._last_sent: Dict[Tuple[str, str], Dict[int, Tuple]] = {}
        self._stopped = False

    # ── subscription lifecycle ──────────────────────────────────────────────

    async def subscribe(self, asset: str, timeframe: str) -> bool:
        """Start streaming a pair, or attach to the stream already running.

        Idempotent: subscribing twice from the same or another tab increments a
        watcher count rather than starting a second loop for the same pair.
        """
        if self._stopped:
            return False
        key = (asset, timeframe)
        self._watchers[key] = self._watchers.get(key, 0) + 1
        if key in self._tasks and not self._tasks[key].done():
            logger.debug("chart: additional watcher for %s %s (%d total)",
                         asset, timeframe, self._watchers[key])
            return True

        self._tasks[key] = asyncio.create_task(self._run(asset, timeframe))
        logger.info("chart: streaming started for %s %s", asset, timeframe)
        return True

    async def unsubscribe(self, asset: str, timeframe: str) -> None:
        """Detach one watcher; stop the loop when the last one leaves."""
        key = (asset, timeframe)
        remaining = self._watchers.get(key, 0) - 1
        if remaining > 0:
            self._watchers[key] = remaining
            return

        self._watchers.pop(key, None)
        self._last_sent.pop(key, None)
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            logger.info("chart: streaming stopped for %s %s", asset, timeframe)

    async def unsubscribe_all(self) -> None:
        """Drop every subscription. Called on disconnect and on shutdown."""
        for asset, timeframe in list(self._watchers):
            self._watchers[(asset, timeframe)] = 1
            await self.unsubscribe(asset, timeframe)

    async def stop(self) -> None:
        self._stopped = True
        await self.unsubscribe_all()
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    def subscriptions(self) -> List[Tuple[str, str]]:
        return sorted(self._watchers)

    def watcher_count(self, asset: str, timeframe: str) -> int:
        return self._watchers.get((asset, timeframe), 0)

    # ── the stream loop ─────────────────────────────────────────────────────

    async def _run(self, asset: str, timeframe: str) -> None:
        key = (asset, timeframe)
        last_emit = 0.0

        try:
            while not self._stopped:
                # Wait for the market to move, but never longer than the idle
                # timeout, and never more often than the minimum interval.
                if self._data_event is not None:
                    self._data_event.clear()
                    try:
                        await asyncio.wait_for(self._data_event.wait(),
                                               timeout=self._idle_timeout)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(self._idle_timeout)

                wait = self._min_interval - (time.monotonic() - last_emit)
                if wait > 0:
                    await asyncio.sleep(wait)

                if await self._emit_latest(asset, timeframe):
                    last_emit = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # One pair failing must not take the connection down with it.
            logger.warning("chart: stream for %s %s ended: %s",
                           asset, timeframe, type(exc).__name__)
        finally:
            self._tasks.pop(key, None)
            self._last_sent.pop(key, None)

    async def _emit_latest(self, asset: str, timeframe: str) -> bool:
        """Fetch the tail and broadcast whatever actually changed.

        Returns True when something was sent, so the caller can rate-limit from
        real emissions rather than from attempts.
        """
        try:
            candles = await self._get_candles(asset, timeframe, TAIL_CANDLES)
        except Exception as exc:  # noqa: BLE001
            logger.debug("chart: tail fetch failed for %s %s: %s",
                         asset, timeframe, type(exc).__name__)
            return False
        if not candles:
            return False

        payload = build_chart_payload(asset, timeframe, candles)
        changed = [c for c in payload["candles"] if self._is_changed(
            (asset, timeframe), c)]
        if not changed:
            return False

        for candle in changed:
            await self._broadcast("candle_update", {
                "asset": asset,
                "timeframe": timeframe,
                "candle": candle,
            })
        return True

    def _is_changed(self, key: Tuple[str, str], candle: Dict[str, Any]) -> bool:
        """True when this candle differs from the one last broadcast.

        Suppresses re-sending an identical candle, which is what stops a busy
        market from pushing the same bar over and over.
        """
        seen = self._last_sent.setdefault(key, {})
        ts = candle["timestamp"]
        shape = (candle["open"], candle["high"], candle["low"],
                 candle["close"], candle["volume"])
        if seen.get(ts) == shape:
            return False
        seen[ts] = shape
        # Only the tail is ever streamed, so the map cannot grow without bound.
        if len(seen) > 32:
            for old in sorted(seen)[:-16]:
                seen.pop(old, None)
        return True
