"""Manual helper: log in to Quotex once and print the resulting SSID token.

Credentials come from the environment -- never hardcode them here. RCA F3:
this file used to contain a real email and a real plaintext password, and both
were committed.

    QUOTEX_EMAIL=you@example.com QUOTEX_PASSWORD='...' python get_session.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.pyquotex_vendor import ensure_pyquotex_on_path  # noqa: E402

# RCA F1: the old hardcoded `parents[1]/"vendor"/"pyquotex"` points at an
# EMPTY directory in a fresh clone; the resolver also finds
# vendor/old-pyquotex, which is the copy actually committed.
ensure_pyquotex_on_path()

from pyquotex.stable_api import Quotex  # noqa: E402


def _credential(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is not set. Export it before running this script.")
    return value


async def main():
    sessions_dir = Path(__file__).resolve().parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    client = Quotex(
        email=_credential("QUOTEX_EMAIL"),
        password=_credential("QUOTEX_PASSWORD"),
        lang="en",
        root_path=str(sessions_dir),
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
            token = data.get("token")
            if token is None:
                # Older layout keyed the payload by email address.
                for value in data.values():
                    if isinstance(value, dict) and value.get("token"):
                        token = value["token"]
                        break
            print(f"\nSSID TOKEN written to {sess_file}")
            print("(it is a live session credential -- do not paste it anywhere)")
        else:
            print("Session file nahi mili")
    else:
        print(f"Connection failed: {reason}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
