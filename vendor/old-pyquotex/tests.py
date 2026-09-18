import asyncio
import json
import os

from dotenv import load_dotenv
from pyquotex.stable_api import Quotex

load_dotenv()
os.environ["HEADLESS"] = "true"

SESSION_FILE = "session.json"

async def main():
    client = Quotex(
        email=os.getenv("QUOTEX_EMAIL"),
        password=os.getenv("QUOTEX_PASSWORD")
    )

    # Load saved session
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                client.set_session(json.load(f))
            print("📂 Loaded saved session.")
        except Exception:
            print("⚠️ Invalid session file. Fresh login will be used.")

    print("🔄 Connecting...")
    await client.connect()

    # Verify connection
    if not await client.check_connect():
        print("⚠️ Saved session is invalid or expired.")
        otp = input("🔐 Enter OTP (if required): ").strip()

        await client.connect(otp=otp)

        if not await client.check_connect():
            print("❌ Connection failed.")
            return

    print("✅ Connected! Balance:", await client.get_balance())

    # Save session
    with open(SESSION_FILE, "w") as f:
        json.dump(client.session_data, f, indent=4)

    print("💾 Session saved.")

    # Place trade
    trade = await client.buy(
        1,
        "EURUSD_otc",   # Use _otc if OTC market
        "call",
        60
    )

    print("Trade placed:", trade)

    if trade and trade[0]:
        result = await client.check_win(trade[1]["id"])
        print("Trade Result:", result)
    else:
        print("❌ Trade failed.")

if __name__ == "__main__":
    asyncio.run(main())
