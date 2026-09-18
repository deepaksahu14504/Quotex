import asyncio, json, os
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

    # Saved session load karo (agar hai)
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            client.set_session(json.load(f))
        print("📂 Loaded saved session. Auto-login attempt...")
    else:
        print("🆕 No saved session. Manual OTP needed.")

    print("🔄 Connecting...")
    await client.connect()   # OTP yahan dalo agar 2FA on hai aur session invalid
    print("✅ Connected! Balance:", await client.get_balance())

    # Session save karo (agley baar OTP skip)
    with open(SESSION_FILE, "w") as f:
        json.dump(client.session_data, f)
    print("💾 Session saved.")

    # Sahi trade call: amount, asset, direction, duration (sab positional)
    trade = await client.buy(1, "EURUSD", "call", 120)
    print("Trade placed:", trade)

    if trade and trade[0]:   # assuming returns tuple (success, trade_info)
        result = await client.check_win(trade[1]["id"])
        print("Result:", result)
    else:
        print("Trade failed.")

asyncio.run(main())
