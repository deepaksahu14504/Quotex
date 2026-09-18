"""Broadcast hub: pushes engine/state/signal/trade events to connected UI
clients — scoped per user_id so one user never sees another's data over the
shared /ws endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)

#: Ceiling on delivering one broadcast to all of a user's connections.
#: Generous for a local websocket write; anything past it means the peer
#: is gone, not busy.
BROADCAST_TIMEOUT = 5.0


class WSHub:
    def __init__(self) -> None:
        # user_id -> set of connected websockets for that user
        self._clients: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    def client_count(self, user_id: str) -> int:
        return len(self._clients.get(user_id, ()))

    async def connect(self, ws: WebSocket, user_id: str) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.setdefault(user_id, set()).add(ws)

    async def disconnect(self, ws: WebSocket, user_id: str) -> None:
        async with self._lock:
            conns = self._clients.get(user_id)
            if conns:
                conns.discard(ws)
                if not conns:
                    self._clients.pop(user_id, None)

    async def broadcast(self, user_id: str, event: str, data: Any) -> None:
        msg = json.dumps({"event": event, "data": data}, default=str)
        async with self._lock:
            conns = list(self._clients.get(user_id, ()))
        if not conns:
            return

        # Latency fix: previously each connection was awaited one at a time
        # in a for loop -- with more than one tab/device open for the same
        # user, one slow/stalled connection delayed delivery to every other
        # connection behind it in the loop. Send to all of this user's
        # connections concurrently instead; a slow one no longer blocks the
        # others.
        # Bounded. The lock is already released above, so a slow client
        # cannot block registration -- but this gather could still hang
        # forever, and broadcast() is awaited from 51 places in the
        # orchestrator, including inside the scan loop. A browser tab on a
        # half-open TCP connection stops draining, send_text() never
        # returns and never raises, and the whole scan cycle stalls behind
        # it. `return_exceptions=True` does not help: a hang is not an
        # exception. The 180s progress watchdog would restart the scan
        # loop, which would hang again on the same dead client.
        #
        # A local websocket write that takes longer than this is not slow,
        # it is gone. A timed-out connection is treated exactly like one
        # that raised: dropped and closed below.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(ws.send_text(msg) for ws in conns),
                               return_exceptions=True),
                timeout=BROADCAST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[ws_hub] broadcast to user=%s exceeded %.0fs -- dropping all %d "
                "connection(s) for this user rather than stalling the caller",
                user_id, BROADCAST_TIMEOUT, len(conns),
            )
            results = [asyncio.TimeoutError("broadcast timed out")] * len(conns)
        dead = [ws for ws, result in zip(conns, results) if isinstance(result, BaseException)]
        for ws, result in zip(conns, results):
            if isinstance(result, BaseException):
                logger.debug("WebSocket send failed: %s", type(result).__name__)
        if dead:
            async with self._lock:
                for ws in dead:
                    conns_set = self._clients.get(user_id)
                    if conns_set:
                        conns_set.discard(ws)
            # Close dead sockets concurrently too, outside the lock (closing
            # doesn't need it, and we don't want a slow close() blocking the
            # cleanup of other dead connections).
            # Bounded for the same reason: closing a socket whose peer has
            # vanished can block just as long as sending to it.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(self._safe_close(ws) for ws in dead),
                                   return_exceptions=True),
                    timeout=BROADCAST_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.debug(
                    "[ws_hub] closing %d dead connection(s) exceeded %.0fs -- "
                    "abandoning them; they are already unregistered",
                    len(dead), BROADCAST_TIMEOUT,
                )

    @staticmethod
    async def _safe_close(ws: WebSocket) -> None:
        try:
            await ws.close(code=1000)
        except Exception:
            pass


hub = WSHub()
