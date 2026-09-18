import asyncio
import time
from pyquotex.stable_api import Quotex

async def main():
    client = Quotex(
        email="deepaksahu14504@gmail.com",
        password="Deepak@123@#"
    )

    await client.connect()

    end = time.time()

    raw = await client.get_candles(
        "EURUSD",
        end,
        36000,      # 10 hours
        300         # 5m
    )

    print(type(raw))
    print(len(raw))

    if raw:
        print(raw[:5])

asyncio.run(main())
