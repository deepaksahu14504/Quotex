import asyncio
import time
import os
from dotenv import load_dotenv

from pyquotex.stable_api import Quotex

load_dotenv()

EMAIL = os.getenv("QUOTEX_EMAIL")
PASSWORD = os.getenv("QUOTEX_PASSWORD")


async def main():
    client = Quotex(
        email=EMAIL,
        password=PASSWORD,
        lang="en",
    )

    print("Connecting...")

    ok, reason = await client.connect()

    if not ok:
        print("Connection Failed:", reason)
        return

    print("Connected Successfully")

    asset = "EURUSD_otc"
    period = 60
    count = 120

    end = time.time()
    offset = period * count

    print(f"\nFetching {count} candles...\n")

    raw = await client.get_candles(
        asset=asset,
        end_from_time=end,
        offset=offset,
        period=period
    )

    if raw is None:
        print("raw = None")
    else:
        print("=" * 60)
        print("RAW Candle Count :", len(raw))
        print("=" * 60)

        if len(raw) > 0:
            print("\nFIRST CANDLE")
            print(raw[0])

            print("\nLAST CANDLE")
            print(raw[-1])

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
