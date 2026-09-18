import asyncio
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

    print("\nFetching historical candles...\n")

    try:
        candles = await client.get_historical_candles(
            asset="EURUSD_otc",
            amount_of_seconds=7200,   # Last 2 hours
            period=60,                # 1 minute candles
            max_workers=5
        )

        print("=" * 60)
        print("Historical Count :", len(candles))
        print("=" * 60)

        if candles:
            print("\nFIRST")
            print(candles[0])

            print("\nLAST")
            print(candles[-1])

    except Exception as e:
        print("ERROR:", e)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
