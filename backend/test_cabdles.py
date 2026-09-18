import asyncio
from app.config import settings
from app.services.market import build_provider

async def main():
    provider = build_provider(settings.market_provider, settings)

    print("Connecting...")
    ok = await provider.connect()
    print("Connected:", ok)

    assets = await provider.get_assets()
    print(f"Total Assets: {len(assets)}")

    for asset in assets:
        if not asset.is_open:
            continue

        try:
            candles = await provider.get_candles(
                asset.symbol,
                "5m",
                120
            )

            print(
                f"{asset.symbol:<20} -> {len(candles):3} candles"
            )

            if candles:
                print(
                    f"   First: {candles[0].timestamp}  Close={candles[0].close}"
                )
                print(
                    f"   Last : {candles[-1].timestamp} Close={candles[-1].close}"
                )

        except Exception as e:
            print(f"{asset.symbol}: ERROR -> {e}")

    await provider.disconnect()

asyncio.run(main())
