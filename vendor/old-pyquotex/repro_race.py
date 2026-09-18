"""Standalone reproduction: an interleaved instruments/list placeholder
race that drops an in-flight history/load response.

Precise race window: the OTHER placeholder (instruments/list) must
overwrite self._temp_status AFTER the history/load placeholder but
BEFORE the real history/load payload arrives. This is NOT part of the
permanent test suite -- a throwaway script proving the race is real.

    python3 repro_race.py
"""
from __future__ import annotations

import asyncio
import logging

from pyquotex.stable_api import Quotex
from pyquotex.types import ReconnectPolicy
from pyquotex.utils.account_type import AccountType
from tests.fakes.ws_replay_server import WSReplayServer, candle_history_frames

logging.basicConfig(level=logging.INFO, format="%(message)s")


async def main() -> None:
    server = WSReplayServer()

    async def history_load_handler(conn, msg: str) -> None:
        frames = candle_history_frames(asset="EURUSD", period=60, n=30)

        # Step 1: the normal history/load placeholder -> temp_status="history/load"
        await conn.send(frames[0])

        # *** Concurrent instruments/get refresh's placeholder lands here ***
        # (exactly what orchestrator._scan_loop's every-10s get_assets() ->
        # client.get_instruments() produces on a real connection, racing
        # with an in-flight get_candles() history/load call.)
        # This OVERWRITES temp_status to "instruments/list" BEFORE the
        # real history/load payload has arrived.
        await conn.send('451-["instruments/list",{"_placeholder":true,"num":0}]')

        # Step 2: the REAL history/load payload arrives NOW, while
        # temp_status still says "instruments/list" -- this is the drop.
        await conn.send(frames[1])

        # The instruments/list payload itself, arriving after (irrelevant
        # to the candle drop, included only for realism).
        await conn.send('[[1,"EURUSD","EUR/USD","forex",4,84,60,30,3,1,0,0,[],1,true]]')

    for event in ("instruments/update", "chart_notification/get", "depth/follow"):
        server.on_event(event, reply=[])
    server.on_event_dynamic("history/load", history_load_handler)

    await server.start()
    try:
        client = Quotex(
            email="offline@test",
            password="x",
            lang="en",
            reconnect_policy=ReconnectPolicy(enabled=False, stale_timeout=0),
            wss_url_override=server.url,
        )
        client.session_data = {
            "cookies": "session=fake",
            "token": "fake-ssid",
            "user_agent": "test-agent/1.0",
        }
        client.account_is_demo = AccountType.DEMO

        ok, reason = await client.connect()
        print("connect:", ok, reason)

        candles = await client.get_candles("EURUSD", None, 1800, 60, timeout=3)
        print(
            "RESULT:",
            "candles received" if candles else "candles is None/empty (DROPPED)",
        )
        await client.close()
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
