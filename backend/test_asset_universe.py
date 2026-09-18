"""Regression tests for the asset-universe / zero-candidate diagnostics.

Covers the 16 cases in the audit brief. Section 1 fails against the
pre-fix code, where every failure mode collapsed into "0 open assets".

Run: cd backend && python3 -m pytest test_asset_universe.py -q
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.services.market import (  # noqa: E402
    ASSET_SNAPSHOT_STALE_TTL,
    PyQuotexProvider,
)


def row(symbol, is_open=True, payout=85.0, name=None):
    """A Quotex instrument row. Index positions are the vendor's, verified
    against _api/assets.py:116,143,145,148, types.py:173 and
    cli/commands/market.py:40-42."""
    r = [0] * 20
    r[1] = symbol
    r[2] = (name or symbol) + "\n"
    r[5] = payout
    r[14] = is_open
    r[len(r) - 9] = payout
    return r


class FakeClient:
    def __init__(self, instruments=None, raises=None, delay=0.0):
        self.instruments = instruments if instruments is not None else []
        self.raises = raises
        self.delay = delay
        self.calls = 0

    async def get_instruments(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.instruments


def provider(client):
    from app.services.market import AssetFeedSnapshot
    p = PyQuotexProvider.__new__(PyQuotexProvider)
    p._client = client
    p._connected = True
    p._asset_feed = AssetFeedSnapshot()
    p._last_good_assets = []
    # These fixtures bypass __init__ via __new__, so anything __init__
    # creates has to be mirrored here. Refreshes are serialized so a slow
    # empty response cannot overwrite a newer valid snapshot.
    p._asset_refresh_lock = asyncio.Lock()
    return p


# ================================================================== #
# 1. Failure modes must be distinguishable  (cases 1-5, 13)
# ================================================================== #
@pytest.mark.asyncio
async def test_success_is_reported_as_ok():
    p = provider(FakeClient([row(f"A{i}") for i in range(57)]))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert len(assets) == 57
    assert st["status"] == "ok" and st["instrument_fetch_success"] is True
    assert st["total_instruments"] == 57 and st["open_count"] == 57
    assert st["last_instrument_error"] is None


@pytest.mark.asyncio
async def test_fetch_exception_is_error_not_zero_assets():
    """THE ROOT-CAUSE CASE. PRE-FIX this returned [] and the UI said
    '0 open assets', identical to a closed market."""
    p = provider(FakeClient(raises=ConnectionError("socket gone")))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert assets == []
    assert st["status"] == "error"
    assert st["instrument_fetch_success"] is False
    assert st["instrument_fetch_error"] is True
    assert "ConnectionError" in st["last_instrument_error"]
    assert "socket gone" in st["last_instrument_error"]


@pytest.mark.asyncio
async def test_fetch_timeout_is_error_and_names_the_reason():
    p = provider(FakeClient(raises=TimeoutError("instruments timed out")))
    await p.get_assets()
    st = p.get_asset_feed_status()
    assert st["status"] == "error"
    assert "TimeoutError" in st["last_instrument_error"]


@pytest.mark.asyncio
async def test_slow_fetch_is_bounded():
    p = provider(FakeClient([row("A")], delay=30.0))
    t0 = time.monotonic()
    from app.services import market as mkt
    mkt.PROVIDER_IO_TIMEOUT = 0.2
    try:
        await p.get_assets()
    finally:
        mkt.PROVIDER_IO_TIMEOUT = 20.0
    assert time.monotonic() - t0 < 3.0
    assert p.get_asset_feed_status()["status"] == "error"


@pytest.mark.asyncio
async def test_empty_response_is_distinct_from_a_failure():
    p = provider(FakeClient([]))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert assets == []
    assert st["status"] == "empty"
    assert st["instrument_fetch_success"] is True     # the broker answered
    assert st["last_instrument_error"] is None
    assert st["total_instruments"] == 0


@pytest.mark.asyncio
async def test_57_instruments_zero_open_is_not_an_error():
    """Case 1 from the brief: a genuinely closed market."""
    p = provider(FakeClient([row(f"A{i}", is_open=False) for i in range(57)]))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert len(assets) == 57
    assert st["status"] == "ok"
    assert st["total_instruments"] == 57
    assert st["open_count"] == 0 and st["closed_count"] == 57
    assert st["instrument_fetch_error"] is False


# ================================================================== #
# 2. Bounded stale fallback  (case 5)
# ================================================================== #
@pytest.mark.asyncio
async def test_transient_failure_serves_the_last_good_snapshot():
    client = FakeClient([row(f"A{i}") for i in range(10)])
    p = provider(client)
    assert len(await p.get_assets()) == 10
    client.raises = ConnectionError("blip")
    assets = await p.get_assets()
    assert len(assets) == 10, "a single blip must not empty the scanner"
    assert p.get_asset_feed_status()["status"] == "stale"


@pytest.mark.asyncio
async def test_stale_snapshot_expires_at_the_ttl():
    """is_open is time-sensitive; trading on an old open-flag is not safe."""
    client = FakeClient([row("A")])
    p = provider(client)
    await p.get_assets()
    p._asset_feed.fetched_at = time.time() - (ASSET_SNAPSHOT_STALE_TTL + 5)
    client.raises = ConnectionError("still down")
    assets = await p.get_assets()
    assert assets == []
    assert p.get_asset_feed_status()["status"] == "error"


@pytest.mark.asyncio
async def test_recovery_after_a_failure_is_automatic():
    client = FakeClient([row(f"A{i}") for i in range(5)])
    p = provider(client)
    await p.get_assets()
    client.raises = ConnectionError("down")
    await p.get_assets()
    assert p.get_asset_feed_status()["status"] == "stale"
    client.raises = None
    client.instruments = [row(f"A{i}") for i in range(9)]
    assets = await p.get_assets()
    assert len(assets) == 9
    st = p.get_asset_feed_status()
    assert st["status"] == "ok" and st["last_instrument_error"] is None


# ================================================================== #
# 3. Parsing  (cases 6, 7)
# ================================================================== #
@pytest.mark.asyncio
async def test_is_open_parsing_uses_index_14():
    p = provider(FakeClient([row("OPEN", is_open=True), row("SHUT", is_open=False)]))
    assets = {a.symbol: a for a in await p.get_assets()}
    assert assets["OPEN"].is_open is True
    assert assets["SHUT"].is_open is False


@pytest.mark.asyncio
async def test_payout_parsing_prefers_index_minus_9_then_5():
    r = row("A", payout=0.0)
    r[len(r) - 9] = 0            # 1M payout absent
    r[5] = 77.5                  # fall back to i[5]
    p = provider(FakeClient([r]))
    assets = await p.get_assets()
    assert assets[0].payout == 77.5


@pytest.mark.asyncio
async def test_otc_flag_from_symbol():
    p = provider(FakeClient([row("EURUSD_otc"), row("EURUSD")]))
    a = {x.symbol: x for x in await p.get_assets()}
    assert a["EURUSD_otc"].is_otc is True and a["EURUSD"].is_otc is False


@pytest.mark.asyncio
async def test_short_rows_are_kept_and_unusable_rows_counted():
    """CONTRACT UPDATED, deliberately. This used to assert that a 2-element
    row was malformed. The parser now matches the vendor's own tolerance
    (types.py:173: `is_open=bool(row[14]) if len(row) > 14 else False`) --
    a row carrying a symbol is KEPT and marked CLOSED rather than dropped,
    because rejecting it made the whole universe vanish when the broker
    sent short rows. A closed asset is filtered out by _eligible_assets(),
    so tolerance never turns into 'tradeable'. Rows with no symbol at all
    (None, a bare string) are still malformed."""
    p = provider(FakeClient([row("GOOD"), [0, "SHORT"], None, "junk"]))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert sorted(a.symbol for a in assets) == ["GOOD", "SHORT"]
    assert next(a for a in assets if a.symbol == "SHORT").is_open is False
    assert st["malformed_count"] == 2          # None and "junk"
    assert st["total_instruments"] == 4


@pytest.mark.asyncio
async def test_no_asset_is_ever_invented():
    p = provider(FakeClient([row("A"), row("B")]))
    assets = await p.get_assets()
    assert {a.symbol for a in assets} == {"A", "B"}
    p2 = provider(FakeClient(raises=RuntimeError("x")))
    assert await p2.get_assets() == []


# ================================================================== #
# 4. Gate funnel  (cases 8, 9, 10, 16)
# ================================================================== #
class FakeMarketInit:
    def __init__(self, ready):
        self.ready = set(ready)

    def is_ready(self, s):
        return s in self.ready


def orch(assets, ready=None, min_payout=75.0, whitelist=None):
    import types as _t
    from app.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._assets_cache = assets
    o._assets_ts = time.time()
    o._asset_funnel = {}
    o.runtime = _t.SimpleNamespace(
        trading=_t.SimpleNamespace(asset_whitelist=whitelist or []),
        risk=_t.SimpleNamespace(min_payout=min_payout),
    )
    o.provider = _t.SimpleNamespace(_asset_feed=None,
                                    get_asset_feed_status=lambda: {})
    o.market_init = FakeMarketInit(ready if ready is not None
                                   else [a.symbol for a in assets])
    return o


from app.schemas import AssetInfo  # noqa: E402


def A(sym, is_open=True, payout=85.0):
    return AssetInfo(symbol=sym, name=sym, payout=payout,
                     is_open=is_open, is_otc=False)


@pytest.mark.asyncio
async def test_funnel_attributes_zero_to_the_closed_gate():
    o = orch([A(f"A{i}", is_open=False) for i in range(57)])
    assert await o._candidate_assets() == []
    f = o.asset_funnel()
    assert f["total_instruments"] == 57
    assert f["open_count"] == 0 and f["closed_count"] == 57
    assert f["final_candidate_count"] == 0


@pytest.mark.asyncio
async def test_funnel_attributes_zero_to_the_payout_gate():
    """Case 3: 57 open, none meet the minimum payout."""
    o = orch([A(f"A{i}", payout=50.0) for i in range(57)], min_payout=75.0)
    assert await o._candidate_assets() == []
    f = o.asset_funnel()
    assert f["open_count"] == 57
    assert f["open_and_payout_qualified_count"] == 0
    assert f["min_payout"] == 75.0


@pytest.mark.asyncio
async def test_funnel_attributes_zero_to_the_ready_gate():
    """Case 4: eligible assets exist, none are READY."""
    o = orch([A(f"A{i}") for i in range(12)], ready=[])
    assert await o._candidate_assets() == []
    f = o.asset_funnel()
    assert f["eligible_before_ready_gate"] == 12
    assert f["ready_count"] == 0
    assert f["final_candidate_count"] == 0


@pytest.mark.asyncio
async def test_funnel_attributes_zero_to_the_whitelist():
    o = orch([A(f"A{i}") for i in range(20)], whitelist=["NOPE"])
    assert await o._candidate_assets() == []
    assert o.asset_funnel()["whitelist_count"] == 0


@pytest.mark.asyncio
async def test_healthy_funnel_reaches_the_scanner():
    """Case 5: eligible + READY."""
    o = orch([A(f"A{i}", payout=80.0 + i) for i in range(20)])
    cands = await o._candidate_assets()
    f = o.asset_funnel()
    assert len(cands) == 12                     # top-12 preserved
    assert f["ready_count"] == 20 and f["final_candidate_count"] == 12
    assert [c.payout for c in cands] == sorted([c.payout for c in cands], reverse=True)


@pytest.mark.asyncio
async def test_gates_are_unchanged_open_and_payout_both_still_apply():
    o = orch([A("OPEN_OK", True, 85.0), A("SHUT_OK", False, 85.0),
              A("OPEN_LOW", True, 10.0)], min_payout=75.0)
    assert [a.symbol for a in await o._candidate_assets()] == ["OPEN_OK"]


# ================================================================== #
# 5. Reopen / reconnect recovery  (cases 11, 12, 14, 15)
# ================================================================== #
@pytest.mark.asyncio
async def test_universe_recovers_when_assets_reopen():
    """0 -> N with no restart."""
    assets = [A(f"A{i}", is_open=False) for i in range(10)]
    o = orch(assets)
    assert await o._candidate_assets() == []
    for a in assets:
        a.is_open = True
    o._assets_ts = 0                        # force the 10s cache to refresh
    o._assets_cache = assets
    o._assets_ts = time.time()
    assert len(await o._candidate_assets()) == 10


@pytest.mark.asyncio
async def test_universe_goes_back_to_zero_when_assets_close():
    assets = [A(f"A{i}") for i in range(10)]
    o = orch(assets)
    assert len(await o._candidate_assets()) == 10
    for a in assets:
        a.is_open = False
    assert await o._candidate_assets() == []


@pytest.mark.asyncio
async def test_ready_assets_become_eligible_again_after_reopen():
    """market_init keeps them READY across a close/reopen, so the scanner
    resumes without waiting for a fresh warm-up."""
    assets = [A(f"A{i}") for i in range(5)]
    o = orch(assets, ready=[f"A{i}" for i in range(5)])
    for a in assets:
        a.is_open = False
    assert await o._candidate_assets() == []
    for a in assets:
        a.is_open = True
    assert len(await o._candidate_assets()) == 5


@pytest.mark.asyncio
async def test_repeated_refresh_does_not_duplicate_the_universe():
    o = orch([A(f"A{i}") for i in range(8)])
    for _ in range(20):
        cands = await o._candidate_assets()
    assert len(cands) == 8
    assert len({c.symbol for c in cands}) == 8


@pytest.mark.asyncio
async def test_provider_reconnect_refreshes_instruments():
    client = FakeClient([row(f"A{i}", is_open=False) for i in range(57)])
    p = provider(client)
    assert sum(1 for a in await p.get_assets() if a.is_open) == 0
    client.instruments = [row(f"A{i}", is_open=True) for i in range(57)]
    assert sum(1 for a in await p.get_assets() if a.is_open) == 57
    assert p.get_asset_feed_status()["open_count"] == 57


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
