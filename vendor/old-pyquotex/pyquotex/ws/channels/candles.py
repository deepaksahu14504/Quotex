"""Quotex `history/load` request channel.

SCOPE OF THIS FILE (verified, not assumed)
------------------------------------------
This module is a REQUEST EMITTER and nothing else. It builds one
`history/load` Socket.IO frame and hands it to
QuotexAPI.send_websocket_request. It does not receive, decode, route,
or dispatch anything.

The receive side of the candle pipeline lives elsewhere, and the audit
that accompanies this file traced it there rather than assuming it was
here:

  * frame reception .............. ws/client.py::_connect_once (line 156)
  * bytes/str decode ............. api.py::_on_message
                                   (`msg.decode("utf-8", errors="ignore")`)
  * Socket.IO prefix strip ....... api.py::_on_message (first `[`/`{` scan)
  * history vs live detection .... api.py::_on_message, matching
                                   'history/list/v2' | 'history/load'
  * asset resolution ............. api.py::_on_message, from `data["asset"]`
  * candles_ready dispatch ....... api.py::_on_message ->
                                   event_registry.set_event(
                                       f'candles_ready_{asset}') and, only
                                   when the broker echoes an index,
                                   f'candles_ready_{asset}_{index}'
  * candle state object .......... ws/objects/candles.py::Candles

Consequently the reported symptoms "candles_ready_<asset> was never
set", "History response dropped" and "History response misrouted"
cannot be fixed from this file alone -- their mechanisms are on the
receive side. What this file CAN do, and now does, is make sure a
malformed or undeliverable request fails immediately and loudly
instead of turning into a silent 30-second wait on an event that was
never going to fire. That accounts for a real share of the observed
"Timeout waiting for candles" lines.

WHAT CHANGED AND WHY (every item traced to a verified defect)
--------------------------------------------------------------
 1. Input validation. Previously ANY value was serialised and sent:
    period=0, an empty asset, a None index. The broker answers such a
    frame with nothing at all, so the caller waited out its full
    timeout and logged "candles_ready_<asset> was never set" -- a
    programming error reported as a network symptom. Now rejected at
    the source with a message naming the offending field.

 2. Send failures carry context. QuotexAPI.send_websocket_request is
    now bounded and raises TimeoutError on a dead transport. Without
    the wrapper below the caller received a bare TimeoutError with no
    indication of which asset/timeframe/request it belonged to.
    The exception is re-raised, never swallowed.

 3. Structured trace logging with a failure branch. The previous
    single DEBUG line fired only on the success path, so a request
    that never reached the socket left no trace at all.

 4. Duplicate in-flight request detection. `index` is supplied by the
    caller and its uniqueness is NOT guaranteed: _api/history.py's
    get_candles() passes expiration.get_timestamp(), a WHOLE-SECOND
    value, so two concurrent requests for the same asset inside the
    same second share an index. Detected and logged here (the fix for
    the resulting cross-delivery belongs to the waiter, which is
    outside this file's scope -- see the accompanying report).

 5. Bounded memory. The in-flight table above is pruned on every call
    and hard-capped, so this module's footprint is O(1) for the life
    of the process regardless of request volume.

NOT changed: the wire format, the payload keys and their order, the
method signature, the return type, or any behaviour on the success
path. A valid request produces byte-identical output to before.
"""
from __future__ import annotations

import logging
import time as _time   # aliased: the `time` PARAMETER of __call__ is part of
                       # the public signature and would otherwise shadow it
from typing import Any

from pyquotex.utils import json_utils as json
from pyquotex.ws.channels.base import Base

logger = logging.getLogger(__name__)

#: Requests older than this are assumed answered (or timed out) and are
#: dropped from the in-flight table. Comfortably above _constants.
#: DEFAULT_TIMEOUT (30s) so a slow-but-legitimate request is not
#: mistaken for a stale entry.
_INFLIGHT_TTL_SECONDS = 60.0

#: Hard cap on the in-flight table. Reached only if pruning somehow
#: cannot keep up; the oldest entries are evicted so this module can
#: never grow without bound.
_INFLIGHT_MAX = 512

#: (asset, index) -> monotonic timestamp of the request that used it.
#: Module-level rather than per-instance because QuotexAPI.get_candles
#: is a @property that constructs a NEW GetCandles on every single call
#: (api.py:1219) -- per-instance state would be discarded immediately
#: and detect nothing.
_inflight: dict[tuple[str, Any], float] = {}


class CandleRequestError(ValueError):
    """Raised for a `history/load` request that cannot be satisfied as
    written.

    A ValueError subclass so existing callers that catch ValueError or
    Exception keep working unchanged, while code that wants to
    distinguish "I built a bad request" from "the network failed" can.
    """


def _prune_inflight(now: float) -> None:
    """Drop expired entries, then hard-cap. O(n) only over a table that
    is bounded to _INFLIGHT_MAX, so this stays cheap on the hot path."""
    if not _inflight:
        return
    stale = [k for k, ts in _inflight.items() if now - ts > _INFLIGHT_TTL_SECONDS]
    for k in stale:
        _inflight.pop(k, None)
    if len(_inflight) > _INFLIGHT_MAX:
        for k, _ts in sorted(_inflight.items(), key=lambda kv: kv[1])[
            : len(_inflight) - _INFLIGHT_MAX
        ]:
            _inflight.pop(k, None)


class GetCandles(Base):
    """Class for Quotex candles websocket channel."""

    name = "candles"

    @staticmethod
    def _validate(
            asset: str, index: Any, time_value: Any, offset: Any, period: Any
    ) -> None:
        """Reject a request the broker cannot answer.

        Deliberately narrow: it rejects only values that are certain to
        produce no response, never values that are merely unusual. A
        large offset or an old timestamp is legitimate and passes.
        """
        if not isinstance(asset, str) or not asset.strip():
            raise CandleRequestError(
                f"history/load requires a non-empty asset symbol, got {asset!r}"
            )
        if index is None:
            raise CandleRequestError(
                f"history/load requires an index for correlation (asset={asset!r}); "
                "the response cannot be matched to this request without one"
            )
        if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
            raise CandleRequestError(
                f"history/load requires a positive integer period in seconds "
                f"(asset={asset!r}), got {period!r}"
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset <= 0:
            raise CandleRequestError(
                f"history/load requires a positive integer offset in seconds "
                f"(asset={asset!r}), got {offset!r}"
            )
        if not isinstance(time_value, (int, float)) or isinstance(time_value, bool):
            raise CandleRequestError(
                f"history/load requires a numeric time (asset={asset!r}), "
                f"got {time_value!r}"
            )
        if time_value <= 0:
            raise CandleRequestError(
                f"history/load requires a positive epoch time (asset={asset!r}), "
                f"got {time_value!r}"
            )

    async def __call__(
            self,
            asset: str,
            index: int,
            time: int,
            offset: int,
            period: int
    ) -> None:
        """Method to send message to candles websocket chanel.

        :param asset: The active/asset identifier.
        :param index: The index of candles.
        :param time: The time of candles.
        :param offset: Time interval in which you want to catalog the candles
        :param period: The candle duration (timeframe for the candles).

        :raises CandleRequestError: the request is malformed and would
            never be answered.
        :raises TimeoutError: the transport did not accept the frame
            (see QuotexAPI.send_websocket_request).

        Signature, parameter order, wire format and return type are
        unchanged. `time` shadows the stdlib module inside this scope,
        which is why the module is imported as `_time` below -- kept
        that way rather than renaming the parameter, because the name is
        part of the public signature.
        """
        self._validate(asset, index, time, offset, period)

        now = _time.monotonic()
        key = (asset, index)
        previous = _inflight.get(key)
        if previous is not None:
            # Not fatal -- the request is still sent. But the caller has
            # reused an (asset, index) pair that is still outstanding, so
            # two waiters may now match the same response.
            logger.warning(
                "[CANDLE_TRACE] DUPLICATE_INFLIGHT asset=%s index=%s age=%.1fs "
                "period=%s -- an identical request is still outstanding; "
                "responses for these two requests cannot be told apart",
                asset, index, now - previous, period,
            )
        _inflight[key] = now
        # Pruned AFTER insertion, not before: pruning first left the table
        # at _INFLIGHT_MAX + 1 once the new entry landed, so the "hard cap"
        # was off by one and never actually held.
        _prune_inflight(now)

        payload = {
            "asset": asset,
            "index": index,
            "time": time,
            "offset": offset,
            "period": period
        }
        data = f'42["history/load",{json.dumps_str(payload)}]'
        logger.debug(
            "[CANDLE_TRACE] REQUEST_BUILT asset=%s index=%s time=%s "
            "offset=%s period=%s bytes=%d",
            asset, index, time, offset, period, len(data),
        )
        try:
            await self.send_websocket_request(data)
        except BaseException as exc:
            # The request never reached the socket, so nothing will ever
            # fire candles_ready_<asset> for it. Drop the in-flight entry
            # so a retry with the same index is not reported as a
            # duplicate, log with full context, and re-raise untouched --
            # swallowing here would convert a hard failure into exactly
            # the silent timeout this change exists to eliminate.
            _inflight.pop(key, None)
            logger.error(
                "[CANDLE_TRACE] SEND_FAILED asset=%s index=%s period=%s "
                "offset=%s error=%s: %s -- candles_ready_%s will NOT fire "
                "for this request",
                asset, index, period, offset, type(exc).__name__, exc, asset,
            )
            raise
        logger.debug(
            "[CANDLE_TRACE] REQUEST_SENT asset=%s index=%s period=%s -- "
            "awaiting candles_ready_%s",
            asset, index, period, asset,
        )


def inflight_snapshot() -> dict[str, Any]:
    """Read-only view of outstanding requests, for diagnostics.

    Additive -- no existing caller is affected. Intended for correlating
    a "Timeout waiting for candles" line against what this side actually
    sent and when.
    """
    now = _time.monotonic()
    return {
        "count": len(_inflight),
        "oldest_age_s": round(now - min(_inflight.values()), 2) if _inflight else 0.0,
        "requests": [
            {"asset": a, "index": i, "age_s": round(now - ts, 2)}
            for (a, i), ts in sorted(_inflight.items(), key=lambda kv: kv[1])[:20]
        ],
    }


__all__ = ["GetCandles", "CandleRequestError", "inflight_snapshot"]
