import asyncio, json, sys
from pathlib import Path

# Correct path — vendor ek level upar hai
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor" / "pyquotex"))

from pyquotex.stable_api import Quotex

async def main():
    sessions_dir = Path(__file__).resolve().parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    
    client = Quotex(
        email="deepaksahu14504@gmail.com",
        password="Deepak@123@#",
        lang="en",
        root_path=str(sessions_dir)
    )
    print("Connecting... OTP aaye toh yahan type karo aur Enter dabaao")
    ok, reason = await client.connect()
    print(f"Status: {ok} | {reason}")
    
    if ok:
        bal = await client.get_balance()
        print(f"Balance: {bal}")
        
        sess_file = sessions_dir / "session.json"
        if sess_file.exists():
            data = json.loads(sess_file.read_text())
            token = data.get("token") or data.get("deepaksahu14504@gmail.com", {}).get("token")
            print(f"\n✅ SSID TOKEN:\n{token}\n")
        else:
            print("Session file nahi mili")
    else:
        print(f"Connection failed: {reason}")
    
    await client.close()

asyncio.run(main())
