import asyncio
import os
from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()
os.environ["HEADLESS"] = "true"

async def main():
    client = Quotex(
        email=os.getenv("QUOTEX_EMAIL"),
        password=os.getenv("QUOTEX_PASSWORD")
    )

    ok, reason = await client.connect()

    if not ok:
        print(reason)
        return

    print("=" * 60)
    print("Balance:", await client.get_balance())
    print("=" * 60)

    # Load instruments
    await client.get_instruments()

    assets = client.get_all_asset_name()
    payouts = client.get_payment()

    print("Total Assets:", len(assets))

    for code, name in assets:
        payout = payouts.get(name, {})
        print(
            f"{code:20} | "
            f"{name:30} | "
            f"Open={payout.get('open')} | "
            f"Payout={payout.get('payment')}"
        )

asyncio.run(main())
