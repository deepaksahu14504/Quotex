import asyncio
import os
from dotenv import load_dotenv

from app.config import settings
from app.schemas import Direction
from app.services.market import build_provider

load_dotenv()


async def main():
    provider = build_provider(
        os.getenv("MARKET_PROVIDER", "pyquotex"),
        settings,
    )

    print("=" * 60)
    print("TEST 1 - CONNECT")
    print("=" * 60)

    ok = await provider.connect()

    print("Connected:", ok)

    if not ok:
        print("Provider connection failed.")
        return

    print()

    print("=" * 60)
    print("TEST 2 - BALANCE")
    print("=" * 60)

    balance = await provider.get_balance()

    print(balance)

    print()

    print("=" * 60)
    print("TEST 3 - ASSETS")
    print("=" * 60)

    assets = await provider.get_assets()

    print("Total Assets:", len(assets))

    if not assets:
        print("No assets returned")
        return

    for a in assets[:10]:
        print(a)

    print()

    asset = None

    for a in assets:
        if a.is_open:
            asset = a
            break

    if asset is None:
        print("No open asset found")
        return

    print("Using Asset:", asset.symbol)

    print()

    print("=" * 60)
    print("TEST 4 - PAYOUT")
    print("=" * 60)

    payout = await provider.get_payout(asset.symbol, "5m")

    print("Payout:", payout)

    print()

    print("=" * 60)
    print("TEST 5 - CANDLES")
    print("=" * 60)

    candles = await provider.get_candles(
        asset.symbol,
        "5m",
        20,
    )

    print("Candles:", len(candles))

    if candles:
        print("First:", candles[0])
        print("Last :", candles[-1])
    else:
        print("No candles returned")

    print()

    print("=" * 60)
    print("TEST 6 - BUY")
    print("=" * 60)

    try:

        order_id, price = await provider.place_order(
            asset.symbol,
            1,
            Direction.CALL,
            60,
        )

        print("Order ID:", order_id)
        print("Entry:", price)

    except Exception as e:
        print("BUY ERROR:", e)
        await provider.disconnect()
        return

    print()

    print("=" * 60)
    print("TEST 7 - WAIT RESULT")
    print("=" * 60)

    while True:

        result = await provider.check_result(order_id)

        if result:

            print(result)

            break

        print("Waiting...")

        await asyncio.sleep(2)

    print()

    print("=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)

    await provider.disconnect()


asyncio.run(main())
