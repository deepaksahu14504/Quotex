"""Async WebSocket client for the Quotex API.

Resilience layer
----------------
The client supports automatic reconnect with exponential backoff and a
stale-connection watchdog. Both are governed by
:class:`pyquotex.types.ReconnectPolicy`; pass ``ReconnectPolicy(enabled=False)``
to restore the original single-connection behavior.

Reconnect flow:

1. ``run_forever`` enters an outer loop that keeps trying to connect
   until :attr:`ReconnectPolicy.max_attempts` is reached (``0`` = infinite).
2. On every successful open, the :class:`QuotexAPI` ``_on_open`` hook
   runs as before AND a re-subscription pass replays any streams the
   user had opened (candle, all-size, mood, realtime price).
3. On unexpected close or watchdog timeout, the loop sleeps using
   :func:`pyquotex._api._waits.backoff_sleep` and reconnects.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from pyquotex._api._waits import backoff_sleep
from pyquotex.global_value import WebsocketStatus
from pyquotex.types import ReconnectPolicy

logger = logging.getLogger(__name__)

#: Ceiling on a single frame write. A healthy send completes in
#: milliseconds; anything near this means the transport is gone.
#:
#: This is the LAST line of defence, and it is the one that matters most.
#: QuotexAPI.send_websocket_request() is already bounded, but eight call
#: sites in api.py bypass it and reach this method directly --
#: api.py:291 (the heartbeat), 304-308 (the _on_open bootstrap sends) and
#: 749 (get_instruments). The heartbeat is the worst of them: it is the
#: component whose entire job is to keep the connection alive and surface
#: its death, it sends every 5 seconds so it is usually the FIRST to meet
#: a half-open socket, and its `except Exception: break` never fires
#: because a half-open send does not raise -- it blocks on drain forever.
#: The task then stays alive doing nothing, with no crash and no log.
WS_SEND_TIMEOUT = 10.0


class WebsocketClient:
    """Pure-async WebSocket client with optional auto-reconnect."""

    def __init__(
        self,
        api: Any,
        reconnect_policy: ReconnectPolicy | None = None,
    ) -> None:
        """Initialize the WebSocket client.

        Args:
            api: The :class:`QuotexAPI` instance this client belongs to.
            reconnect_policy: Resilience configuration. Defaults to
                :class:`ReconnectPolicy` with auto-reconnect enabled.
        """
        self.api = api
        self.state = api.state
        self.policy = reconnect_policy or ReconnectPolicy()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._closing = False
        self._watchdog_task: asyncio.Task[None] | None = None
        # Counter of successful opens; the very first open does NOT
        # replay subscriptions (there are none yet).
        self._open_count = 0

    @property
    def wss(self) -> "WebsocketClient":
        """Returns the low-level WebSocket wrapper (self)."""
        return self

    async def send(self, data: str) -> None:
        """Send a frame; log instead of crashing if the socket is closed.

        Bounded by WS_SEND_TIMEOUT. A half-open TCP connection -- dropped
        by the peer, a NAT table or a firewall with no FIN and no RST --
        does not raise from `send()`. It writes into the OS send buffer
        and, once that fills with no ACKs coming back, awaits drain
        forever. Neither of the two `except` clauses below can catch that,
        because nothing is raised.

        On timeout the transport is declared dead so the reconnect path
        engages, and TimeoutError is raised to the caller. Raising is
        deliberate: the heartbeat's `except Exception: break` then finally
        does what it was written to do, and every other caller learns its
        frame never went out instead of assuming it did.
        """
        if self._ws and self._ws.state is State.OPEN:
            try:
                await asyncio.wait_for(self._ws.send(data), timeout=WS_SEND_TIMEOUT)
                logger.debug("Sent: %s", data)
            except asyncio.TimeoutError:
                logger.error(
                    "WebSocket send did not complete within %.0fs -- transport "
                    "considered dead; marking ERROR so reconnect engages",
                    WS_SEND_TIMEOUT,
                )
                self._mark_transport_failed()
                raise TimeoutError(
                    f"WebSocket send exceeded {WS_SEND_TIMEOUT}s -- transport dead"
                )
            except ConnectionClosed as e:
                logger.warning("Cannot send, connection closed: %s", e)
            except Exception as e:
                logger.error("Error sending WebSocket message: %s", e)

    def _mark_transport_failed(self) -> None:
        """Flip connection status so the reconnect path engages.

        Mirrors QuotexAPI._mark_transport_failed. Bounding the send alone
        would only convert a permanent hang into a permanent stream of
        timeouts against the same dead socket; the status flip is what
        Orchestrator._watchdog_loop's `not provider.connected` check reads.
        Best-effort and allocation-free -- it runs on an already-degraded
        path.
        """
        try:
            if self.api is not None and self.api.state.status != WebsocketStatus.ERROR:
                self.api.state.status = WebsocketStatus.ERROR
        except Exception:
            logger.debug("could not flag transport failure on the api", exc_info=True)

    async def run_forever(
            self,
            url: str,
            extra_headers: dict[str, str] | None = None,
            ssl: Any = None,
            **kwargs: Any,
    ) -> None:
        """Connect to the WebSocket and stay connected.

        With ``ReconnectPolicy.enabled=False`` this method connects once
        and returns when the connection ends. With auto-reconnect on, it
        keeps reconnecting until :meth:`close` is called or
        ``max_attempts`` is exceeded.
        """
        attempt = 0
        while True:
            try:
                await self._connect_once(url, extra_headers, ssl)
                if self._closing:
                    return
                attempt = 0  # successful run resets the backoff
            except ConnectionClosed as e:
                self._handle_close_exception(e)
            except Exception as e:
                logger.error("WebSocket error: %s", e)
                self.api._on_error(e)

            if not self.policy.enabled or self._closing:
                return
            if self.policy.max_attempts and attempt >= self.policy.max_attempts:
                logger.error(
                    "WebSocket auto-reconnect giving up after %d attempts",
                    attempt,
                )
                return

            logger.info("WebSocket reconnecting (attempt #%d)", attempt + 1)
            await backoff_sleep(
                attempt,
                base=self.policy.base_delay,
                cap=self.policy.max_delay,
                jitter=self.policy.jitter,
            )
            attempt += 1

    async def _connect_once(
            self,
            url: str,
            extra_headers: dict[str, str] | None,
            ssl: Any,
    ) -> None:
        """One ``connect()`` cycle. Returns when the connection ends."""
        headers = extra_headers or {}
        async with websockets.connect(
            url,
            additional_headers=headers,
            ssl=ssl,
            ping_interval=24,
            ping_timeout=20,
            max_size=2 ** 23,
            compression=None,
        ) as ws:
            self._ws = ws
            self.api.last_message_at = time.monotonic()
            await self.api._on_open()
            self._open_count += 1
            logger.debug(
                "[CANDLE_TRACE] websocket (re)connected: open_count=%d "
                "api_instance_id=%s will_replay_subscriptions=%s",
                self._open_count, id(self.api), self._open_count > 1,
            )
            if self._open_count > 1:
                asyncio.create_task(self._replay_subscriptions())

            self._start_watchdog()
            try:
                async for raw in ws:
                    await self.api._on_message(raw)
            finally:
                self._stop_watchdog()

    def _handle_close_exception(self, exc: ConnectionClosed) -> None:
        rcvd = getattr(exc, "rcvd", None)
        sent = getattr(exc, "sent", None)
        if rcvd:
            code, reason = rcvd.code, rcvd.reason
        elif sent:
            code, reason = sent.code, sent.reason
        else:
            code, reason = 1006, str(exc)
        logger.info("WebSocket closed: code=%s, reason=%s", code, reason)
        self.api._on_close(code, reason)

    # ------------------------------------------------------------------
    # Stale-connection watchdog
    # ------------------------------------------------------------------
    def _start_watchdog(self) -> None:
        if self.policy.stale_timeout <= 0:
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def _stop_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        timeout = self.policy.stale_timeout
        try:
            while self._ws is not None and self._ws.state is State.OPEN:
                await asyncio.sleep(min(timeout / 3.0, 10.0))
                silent_for = time.monotonic() - self.api.last_message_at
                if silent_for > timeout:
                    logger.warning(
                        "WebSocket idle for %.1fs (>%ds); recycling.",
                        silent_for, timeout,
                    )
                    try:
                        await self._ws.close(code=4000, reason="watchdog-stale")
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Subscription replay after reconnect
    # ------------------------------------------------------------------
    async def _replay_subscriptions(self) -> None:
        """Re-issue every tracked subscription after a successful reconnect."""
        try:
            for _ in range(40):  # ~2 s
                if self.state.status == WebsocketStatus.CONNECTED:
                    break
                await asyncio.sleep(0.05)
        except Exception:  # pragma: no cover
            pass

        subs = list(getattr(self.api, "_subscriptions", {}).values())
        logger.debug(
            "[CANDLE_TRACE] resubscribe pass starting: api_instance_id=%s "
            "count=%d subs=%s",
            id(self.api), len(subs),
            [f"{s.kind}:{s.asset}:{s.period}" for s in subs],
        )
        for sub in subs:
            try:
                await self._replay_one(sub)
                logger.debug(
                    "[CANDLE_TRACE] resubscribed: kind=%s asset=%s period=%s",
                    sub.kind, sub.asset, sub.period,
                )
            except Exception as e:
                logger.warning(
                    "Failed to replay subscription kind=%s asset=%s: %s",
                    sub.kind, sub.asset, e,
                )

    async def _replay_one(self, sub: Any) -> None:
        api = self.api
        if sub.kind == "candle":
            await api.subscribe_realtime_candle(sub.asset, sub.period or 0)
            await api.chart_notification(sub.asset)
            await api.follow_candle(sub.asset)
        elif sub.kind == "candle_all_size":
            await api.subscribe_all_size(sub.asset)
        elif sub.kind == "mood":
            instrument = sub.extra.get("instrument", "turbo-option")
            await api.subscribe_Traders_mood(sub.asset, instrument)
        elif sub.kind == "realtime_price":
            await api.subscribe_realtime_candle(sub.asset, sub.period or 0)

    async def close(self) -> None:
        """Close the websocket gracefully and stop auto-reconnect."""
        self._closing = True
        self.policy = ReconnectPolicy(enabled=False)
        self._stop_watchdog()
        if self._ws and self._ws.state is not State.CLOSED:
            await self._ws.close()

    def is_alive(self) -> bool:
        """Return True iff the underlying socket is currently OPEN."""
        return self._ws is not None and self._ws.state is State.OPEN
