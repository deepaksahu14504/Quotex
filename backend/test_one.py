"""Live-broker smoke test: pull 10 hours of 5m candles for EURUSD.

This talks to the REAL Quotex API, so it is skipped unless credentials are
exported. It exists to prove the vendored library and the transport still work
end to end -- it is not part of the offline suite.

    QUOTEX_EMAIL=... QUOTEX_PASSWORD=... python -m pytest test_one.py -q -s

RCA F3: this file used to contain a real email and plaintext password.
RCA F1: it used to put an empty `vendor/pyquotex` on sys.path, so it failed at
collection with ModuleNotFoundError instead of skipping.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.pyquotex_vendor import ensure_pyquotex_on_path  # noqa: E402

ensure_pyquotex_on_path()

pytest.importorskip("pyquotex.stable_api", reason="vendored pyquotex not available")

pytestmark = pytest.mark.skipif(
    not (os.environ.get("QUOTEX_EMAIL") and os.environ.get("QUOTEX_PASSWORD")),
    reason="QUOTEX_EMAIL / QUOTEX_PASSWORD not set -- this hits the live broker",
)


@pytest.mark.asyncio
async def test_get_candles_live_broker():
    from pyquotex.stable_api import Quotex

    client = Quotex(
        email=os.environ["QUOTEX_EMAIL"],
        password=os.environ["QUOTEX_PASSWORD"],
    )
    await client.connect()
    try:
        raw = await client.get_candles("EURUSD", time.time(), 36000, 300)
    finally:
        await client.close()

    print(type(raw), len(raw))
    assert raw, "broker returned no candles"
    assert len(raw) > 10, f"only {len(raw)} candles for a 10-hour window"
