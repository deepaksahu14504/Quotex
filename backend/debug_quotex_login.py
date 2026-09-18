"""Standalone Quotex login debugger — uses the EXACT same PyQuotexProvider
class the backend uses (not a reimplementation), so there is zero chance of
divergence between "what this script does" and "what the app does". Run
this directly on the server, outside the web app entirely, to see precisely
where the login sequence succeeds or fails.

Usage:
    cd backend
    source .venv/bin/activate
    python debug_quotex_login.py you@example.com 'your-password' --demo

Add --real instead of --demo to test against a REAL account (be sure you
mean to — this only connects, it does not place trades).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# pyquotex itself logs via print(), not `logging` — those show up on stdout
# regardless of level. This just makes sure OUR wrapper's logger.* calls
# (services/market.py) are visible too.
logging.getLogger("app.services.market").setLevel(logging.DEBUG)


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    email, password = sys.argv[1], sys.argv[2]
    is_demo = "--real" not in sys.argv[3:]

    from app.services.market import PyQuotexProvider

    print("=" * 70)
    print(f"STEP 1: constructing PyQuotexProvider  email={email!r}  demo={is_demo}")
    print("=" * 70)

    provider = PyQuotexProvider(
        email=email,
        password=password,
        is_demo=is_demo,
        otp_callback=debug_otp_prompt,
        sessions_dir="./debug_session_data",  # isolated from any real user's dir
    )

    print("\n" + "=" * 70)
    print("STEP 2: connect() — watch for pyquotex's own [DEBUG]/[WS DEBUG] lines below")
    print("=" * 70 + "\n")

    try:
        ok = await provider.connect()
    except Exception as exc:
        print("\n" + "!" * 70)
        print(f"connect() raised an exception (this also means the real app would")
        print(f"show 'Connect failed: {exc}' as a toast, caught one layer up in")
        print(f"orchestrator.py — but here's the raw exception for full detail):")
        print(f"  {type(exc).__name__}: {exc}")
        print("!" * 70)
        await provider.disconnect()
        return

    print("\n" + "=" * 70)
    print(f"STEP 3: connect() returned: {ok}")
    print("=" * 70)

    if ok:
        try:
            balance, currency = await provider.get_balance()
            print(f"STEP 4: get_balance() -> {balance} {currency}")
        except Exception as exc:
            print(f"STEP 4: get_balance() FAILED: {exc}")

        ssid, cookies, ua = provider.get_session_snapshot()
        print("\nSTEP 5: session snapshot after connect (lengths only, not raw secrets):")
        print(f"  ssid_present={bool(ssid)}  ssid_len={len(ssid or '')}")
        print(f"  cookies_present={bool(cookies)}  cookies_len={len(cookies or '')}")
        print(f"  user_agent={ua!r}")
    else:
        print("\nConnect failed. Scroll up for the pyquotex [DEBUG] lines — specifically look for:")
        print('  - "[DEBUG] Websocket authorization SUCCESS!"')
        print('  - "[DEBUG] Websocket authorization rejected"')
        print("  - any HTTP error / non-200 status during the sign-in POST")
        print("  - a Cloudflare / captcha challenge page instead of the real sign-in form")

    await provider.disconnect()


async def debug_otp_prompt(message: str) -> str:
    print(f"\n>>> OTP REQUIRED: {message}")
    return input(">>> Enter the PIN code Quotex emailed you: ").strip()


if __name__ == "__main__":
    asyncio.run(main())
