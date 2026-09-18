"""Regression tests for the instrument-feed / empty-snapshot bug.

Covers the 12 required cases. Every test in sections 1-3 fails against
the pre-fix code.

Run: cd backend && python3 -m pytest test_instrument_feed.py -q
"""
from __future__ import annotations

import asyncio
import sys
import time
import types as _t
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from app.schemas import AssetInfo  # noqa: E402
from app.services.market import (  # noqa: E402
    ASSET_SNAPSHOT_STALE_TTL,
    AssetFeedSnapshot,
    PyQuotexProvider,
)


def row(symbol, is_open=True, payout=85.0, short=False):
    if short:
        return [0, symbol]                  # too short -> malformed
    r = [0] * 20
    r[1] = symbol
    r[2] = symbol + "\n"
    r[5] = payout
    r[14] = is_open
    r[len(r) - 9] = payout
    return r


def good_snapshot(n=57, malformed=0):
    return ([row(f"A{i}") for i in range(n)]
            + [row("BAD", short=True) for _ in range(malformed)])


class Client:
    def __init__(self, mode="ok", n=57, malformed=0, delay=0.0):
        self.mode, self.n, self.malformed, self.delay = mode, n, malformed, delay
        self.calls = 0

    async def get_instruments(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "ok":
            return good_snapshot(self.n, self.malformed)
        if self.mode == "empty":
            return []
        if self.mode == "none":
            return None
        if self.mode == "garbage":
            return ["junk", None, 42, {"a": 1}]
        raise ConnectionError("socket gone")


def provider(client):
    p = PyQuotexProvider.__new__(PyQuotexProvider)
    p._client = client
    p._connected = True
    p._asset_feed = AssetFeedSnapshot()
    p._last_good_assets = []
    p._asset_refresh_lock = asyncio.Lock()
    return p


# ================================================================== #
# 1. Last-known-good preservation   (cases 1, 2, 5, 6, 7)
# ================================================================== #
@pytest.mark.asyncio
async def test_case1_valid_snapshot_is_stored_as_last_known_good():
    p = provider(Client("ok", n=57))
    assets = await p.get_assets()
    assert len(assets) == 57
    assert len(p._last_good_assets) == 57
    assert p.get_asset_feed_status()["status"] == "ok"


@pytest.mark.asyncio
async def test_case2_transient_empty_does_not_destroy_the_snapshot():
    """THE ROOT-CAUSE CASE. get_instruments() returns [] on four
    non-exception paths, so this never raised and the empty list became
    the new truth, wiping a valid 57-asset universe."""
    c = Client("ok", n=57)
    p = provider(c)
    await p.get_assets()
    c.mode = "empty"
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert len(assets) == 57, "the known-good universe must be preserved"
    assert st["status"] == "stale"
    assert st["serving_last_known_good"] is True
    assert st["consecutive_empty"] == 1


@pytest.mark.asyncio
async def test_case5_valid_snapshot_returns_and_refreshes_the_universe():
    c = Client("ok", n=57)
    p = provider(c)
    await p.get_assets()
    c.mode = "empty"
    await p.get_assets()
    c.mode = "ok"
    c.n = 60
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert len(assets) == 60
    assert st["status"] == "ok" and st["consecutive_empty"] == 0


@pytest.mark.asyncio
async def test_case6_no_previous_snapshot_plus_empty_fabricates_nothing():
    p = provider(Client("empty"))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert assets == []
    assert st["status"] == "empty"
    assert p._last_good_assets == []


@pytest.mark.asyncio
async def test_case7_all_rows_unparseable_preserves_the_previous_snapshot():
    c = Client("ok", n=57)
    p = provider(c)
    await p.get_assets()
    c.mode = "garbage"
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert len(assets) == 57, "a schema break must not empty the universe"
    assert st["status"] == "stale"


@pytest.mark.asyncio
async def test_none_response_is_treated_as_empty_not_valid():
    c = Client("ok", n=57)
    p = provider(c)
    await p.get_assets()
    c.mode = "none"
    assert len(await p.get_assets()) == 57


@pytest.mark.asyncio
async def test_exception_still_preserves_within_ttl():
    c = Client("ok", n=57)
    p = provider(c)
    await p.get_assets()
    c.mode = "raise"
    assert len(await p.get_assets()) == 57
    assert p.get_asset_feed_status()["status"] == "stale"


@pytest.mark.asyncio
async def test_snapshot_is_dropped_once_the_ttl_lapses():
    """Bounded: is_open is time-sensitive; stale open-flags are not safe."""
    c = Client("ok", n=57)
    p = provider(c)
    await p.get_assets()
    c.mode = "empty"
    p._asset_feed.fetched_at = time.time() - (ASSET_SNAPSHOT_STALE_TTL + 5)
    assets = await p.get_assets()
    assert assets == []
    assert p.get_asset_feed_status()["status"] in ("empty", "error")


# ================================================================== #
# 2. Parser counters   (case 9)
# ================================================================== #
@pytest.mark.asyncio
async def test_case9_counters_never_produce_an_impossible_ratio():
    """PRE-FIX: a good refresh left malformed_count=4, then a failed
    refresh reset total_instruments to 0 without resetting it, rendering
    '4/0 instrument rows failed validation'."""
    # good_snapshot()'s "malformed" rows are 2-element rows, which the
    # vendor-aligned parser now KEEPS as closed assets (see
    # test_short_rows_are_kept_as_closed_not_rejected). They therefore
    # count as short, not malformed. What this test is actually about is
    # the RATIO never becoming impossible.
    c = Client("ok", n=58, malformed=4)
    p = provider(c)
    await p.get_assets()
    st = p.get_asset_feed_status()
    assert st["last_attempt_rows"] == 62
    assert st["last_attempt_short_rows"] == 4
    assert st["last_attempt_malformed"] == 0

    c.mode = "raise"
    p._asset_feed.fetched_at = time.time() - (ASSET_SNAPSHOT_STALE_TTL + 5)
    await p.get_assets()
    st = p.get_asset_feed_status()
    assert st["malformed_count"] <= st["total_instruments"], \
        f"impossible ratio {st['malformed_count']}/{st['total_instruments']}"
    assert st["last_attempt_malformed"] <= max(st["last_attempt_rows"], 0)


@pytest.mark.asyncio
async def test_attempt_counters_describe_one_refresh():
    c = Client("ok", n=58, malformed=4)
    p = provider(c)
    await p.get_assets()
    c.mode = "empty"
    await p.get_assets()
    st = p.get_asset_feed_status()
    assert st["last_attempt_rows"] == 0 and st["last_attempt_malformed"] == 0


@pytest.mark.asyncio
async def test_served_counters_match_the_served_snapshot():
    p = provider(Client("ok", n=58, malformed=4))
    await p.get_assets()
    st = p.get_asset_feed_status()
    # 62 rows in, all 62 parsed (58 full + 4 short-but-usable), 0 malformed.
    # The invariant that matters is that the served counters agree with
    # each other -- that is what produced the impossible "4/0".
    assert st["total_instruments"] == 62
    assert st["valid_rows"] == 62
    assert st["malformed_count"] == 0
    assert st["valid_rows"] + st["malformed_count"] == st["total_instruments"]


# ================================================================== #
# 3. Concurrency   (case 8)
# ================================================================== #
@pytest.mark.asyncio
async def test_case8_slow_empty_cannot_overwrite_a_newer_valid_snapshot():
    class Racy:
        def __init__(self):
            self.calls = 0

        async def get_instruments(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.2)      # slow empty
                return []
            return good_snapshot(57)          # fast valid

    p = provider(Racy())
    a, b = await asyncio.gather(p.get_assets(), p.get_assets())
    assert len(p._last_good_assets) == 57, "a slow empty overwrote the valid snapshot"
    assert max(len(a), len(b)) == 57


@pytest.mark.asyncio
async def test_case4_zero_refresh_is_retried_on_the_next_call():
    c = Client("ok", n=57)
    p = provider(c)
    await p.get_assets()
    c.mode = "empty"
    before = c.calls
    for _ in range(3):
        await p.get_assets()
    assert c.calls == before + 3, "each call must re-attempt, not latch"
    assert p.get_asset_feed_status()["consecutive_empty"] == 3


# ================================================================== #
# 4. Scanner continuity   (cases 3, 10, 11, 12)
# ================================================================== #
class Ready:
    def __init__(self, ready):
        self.ready = set(ready)

    def is_ready(self, s):
        return s in self.ready


def orchestrator(prov, ready_symbols):
    from app.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.provider = prov
    o._assets_cache = []
    o._assets_ts = 0.0
    o._asset_funnel = {}
    o.runtime = _t.SimpleNamespace(
        trading=_t.SimpleNamespace(asset_whitelist=[]),
        risk=_t.SimpleNamespace(min_payout=75.0))
    o.market_init = Ready(ready_symbols)
    return o


@pytest.mark.asyncio
async def test_case3_existing_candidates_survive_a_zero_refresh():
    """AUDJPY/AUDNZD/AUDUSD must not be killed by one empty refresh."""
    c = Client("ok", n=57)
    p = provider(c)
    o = orchestrator(p, [f"A{i}" for i in range(57)])

    before = [a.symbol for a in await o._candidate_assets()]
    assert len(before) == 12

    c.mode = "empty"
    o._assets_ts = 0.0
    after = [a.symbol for a in await o._candidate_assets()]
    assert after == before, "the scanner lost its candidates on a transient empty"


@pytest.mark.asyncio
async def test_case10_ready_plus_eligible_becomes_a_candidate():
    p = provider(Client("ok", n=57))
    o = orchestrator(p, [f"A{i}" for i in range(57)])
    assert len(await o._candidate_assets()) == 12


@pytest.mark.asyncio
async def test_case11_ready_does_not_make_assets_tradeable_by_itself():
    """57/57 READY with NO valid snapshot must yield ZERO candidates --
    never 57 fabricated ones."""
    p = provider(Client("empty"))
    o = orchestrator(p, [f"A{i}" for i in range(57)])
    assert await o._candidate_assets() == []


@pytest.mark.asyncio
async def test_ready_but_market_closed_is_still_zero():
    """READY must never bypass the is_open gate."""
    class Closed(Client):
        async def get_instruments(self):
            self.calls += 1
            return [row(f"A{i}", is_open=False) for i in range(57)]

    p = provider(Closed())
    o = orchestrator(p, [f"A{i}" for i in range(57)])
    assert await o._candidate_assets() == []


@pytest.mark.asyncio
async def test_ready_but_payout_below_minimum_is_still_zero():
    class LowPayout(Client):
        async def get_instruments(self):
            self.calls += 1
            return [row(f"A{i}", payout=50.0) for i in range(57)]

    p = provider(LowPayout())
    o = orchestrator(p, [f"A{i}" for i in range(57)])
    assert await o._candidate_assets() == []


@pytest.mark.asyncio
async def test_case12_recovery_admits_new_candidates_without_restart():
    c = Client("ok", n=12)
    p = provider(c)
    o = orchestrator(p, [f"A{i}" for i in range(60)])
    assert len(await o._candidate_assets()) == 12

    c.mode = "empty"
    o._assets_ts = 0.0
    assert len(await o._candidate_assets()) == 12      # preserved

    c.mode = "ok"
    c.n = 60
    o._assets_ts = 0.0
    after = await o._candidate_assets()
    assert len(after) == 12                            # top-12 policy intact
    assert p.get_asset_feed_status()["open_count"] == 60


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ================================================================== #
# 5. PRODUCTION REGRESSION — the observed "4/4" + "empty response"
# ================================================================== #
DICT_PAYLOAD = {"asset": "EURUSD", "period": 60, "index": 123, "data": []}


class DictClient(Client):
    """Reproduces the payload that reached production: a 4-key dict stored
    into api.instruments by the unvalidated writer at
    api.py::_h_instruments_list."""

    async def get_instruments(self):
        self.calls += 1
        if self.mode == "ok":
            return good_snapshot(self.n, self.malformed)
        return DICT_PAYLOAD


@pytest.mark.asyncio
async def test_production_dict_payload_does_not_wipe_the_universe():
    """Observed in production:
         Assets feed        : Broker returned an empty instrument response
         Instrument parsing : 4/4 instrument rows failed validation
    `len(dict)` is the key count and `for i in dict` iterates the keys, so
    a 4-key dict read as exactly 4 malformed rows."""
    c = DictClient("ok", n=58)
    p = provider(c)
    assert len(await p.get_assets()) == 58
    c.mode = "dict"
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert len(assets) == 58, "a wrong-shaped payload must not empty the universe"
    assert st["status"] == "stale"
    assert st["serving_last_known_good"] is True


@pytest.mark.asyncio
async def test_dict_payload_never_reports_phantom_malformed_rows():
    c = DictClient("dict")
    p = provider(c)
    await p.get_assets()
    st = p.get_asset_feed_status()
    assert (st["last_attempt_malformed"], st["last_attempt_rows"]) == (0, 0), \
        "dict keys must not be counted as instrument rows"


@pytest.mark.asyncio
async def test_dict_payload_records_a_named_error():
    c = DictClient("dict")
    p = provider(c)
    await p.get_assets()
    err = p.get_asset_feed_status()["last_instrument_error"]
    assert "MalformedInstrumentPayload" in err and "dict" in err


@pytest.mark.asyncio
async def test_recovery_after_a_wrong_shaped_payload():
    c = DictClient("ok", n=58)
    p = provider(c)
    await p.get_assets()
    c.mode = "dict"
    await p.get_assets()
    c.mode = "ok"
    assert len(await p.get_assets()) == 58
    assert p.get_asset_feed_status()["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"a": 1, "b": 2, "c": 3, "d": 4}, "a string", 42, {"list": None},
])
async def test_any_non_list_payload_is_rejected_safely(payload):
    class Odd(Client):
        async def get_instruments(self):
            self.calls += 1
            return good_snapshot(10) if self.mode == "ok" else payload

    c = Odd("ok")
    p = provider(c)
    await p.get_assets()
    c.mode = "odd"
    assert len(await p.get_assets()) == 10, f"{payload!r} wiped the universe"


# ================================================================== #
# 6. ROW-LENGTH TOLERANCE + SHAPE DIAGNOSTICS
# ================================================================== #
def short_row(sym, n, payout=85.0):
    r = [0] * n
    if n > 1: r[1] = sym
    if n > 2: r[2] = sym + "\n"
    if n > 5: r[5] = payout
    return r


class PayloadClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def get_instruments(self):
        self.calls += 1
        return self.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [2, 3, 6, 8, 14])
async def test_short_rows_are_kept_as_closed_not_rejected(n):
    """Aligned with the vendor's own contract (types.py:173:
    `is_open=bool(row[14]) if len(row) > 14 else False`). The parser used
    to require len >= 15 and drop anything shorter -- a plausible cause of
    the production '4/4 instrument rows failed validation' against a
    non-empty response."""
    p = provider(PayloadClient([short_row(f"A{i}", n) for i in range(4)]))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert len(assets) == 4, "short rows must not be discarded"
    assert st["last_attempt_malformed"] == 0
    assert st["last_attempt_short_rows"] == 4


@pytest.mark.asyncio
async def test_short_rows_are_never_tradeable():
    """Kept, but CLOSED -- so _eligible_assets filters them out. Tolerance
    must never turn into 'unknown assets become tradeable'."""
    p = provider(PayloadClient([short_row(f"A{i}", 8) for i in range(4)]))
    assets = await p.get_assets()
    assert all(a.is_open is False for a in assets)


@pytest.mark.asyncio
async def test_full_rows_still_parse_exactly_as_before():
    p = provider(PayloadClient(good_snapshot(58)))
    assets = await p.get_assets()
    assert len(assets) == 58
    assert all(a.is_open for a in assets)
    assert p.get_asset_feed_status()["last_attempt_short_rows"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
    (["x", "y", "z", "w"], "str"),
    ([[1], [2], [3], [4]], "list(len=1)"),
    ([{"a": 1}, {"b": 2}], "dict"),
])
async def test_unparseable_payloads_report_their_actual_shape(payload, expect):
    """'4/4 rows failed' alone does not say WHAT arrived. Without this the
    same counter covers a dict read as keys, short rows, and non-row
    values -- three different bugs."""
    p = provider(PayloadClient(payload))
    await p.get_assets()
    shape = p.get_asset_feed_status()["last_attempt_shape"]
    assert shape, "no shape sample captured"
    assert expect in shape[0]


@pytest.mark.asyncio
async def test_shape_samples_are_bounded():
    p = provider(PayloadClient(["bad"] * 500))
    await p.get_assets()
    assert len(p.get_asset_feed_status()["last_attempt_shape"]) <= 3


# ================================================================== #
# 7. CANDLE-PAYLOAD MISROUTE — the live "4/4" case
# ================================================================== #
def candle_rows(n=4):
    """A history/load response body: [time, open, close, high, low]."""
    return [[1_700_000_000 + i * 60, 1.0855, 1.0857, 1.0860, 1.0850]
            for i in range(n)]


@pytest.mark.asyncio
async def test_four_candle_rows_reproduce_the_production_symptom_shape():
    """Four candle rows read as four unusable instrument rows -- the exact
    counter the dashboard showed. They must be REJECTED, never turned into
    assets whose symbol is str(open_price)."""
    p = provider(PayloadClient(candle_rows(4)))
    assets = await p.get_assets()
    st = p.get_asset_feed_status()
    assert assets == []
    assert (st["last_attempt_malformed"], st["last_attempt_rows"]) == (4, 4)
    assert st["last_attempt_numeric_rows"] == 4
    assert "candle/quote row" in st["last_attempt_shape"][0]


@pytest.mark.asyncio
async def test_candle_payload_cannot_wipe_a_valid_universe():
    class Flip(Client):
        async def get_instruments(self):
            self.calls += 1
            return good_snapshot(58) if self.mode == "ok" else candle_rows(4)

    c = Flip("ok")
    p = provider(c)
    assert len(await p.get_assets()) == 58
    c.mode = "candles"
    assert len(await p.get_assets()) == 58, "a misrouted candle frame wiped the universe"
    assert p.get_asset_feed_status()["status"] == "stale"
    c.mode = "ok"
    assert len(await p.get_assets()) == 58
    assert p.get_asset_feed_status()["status"] == "ok"


@pytest.mark.asyncio
async def test_a_candle_row_never_becomes_a_tradeable_asset():
    """The length guard alone cannot catch this: a candle row is length 5,
    which passes any pure length test."""
    p = provider(PayloadClient(candle_rows(20)))
    for a in await p.get_assets():
        assert False, f"candle row became asset {a.symbol!r}"


def test_vendor_classifier_separates_candles_from_instruments():
    import sys as _s
    from pathlib import Path as _P
    _s.path.append(str(_P(__file__).resolve().parents[1] / "vendor" / "pyquotex"))
    from pyquotex.api import QuotexAPI

    ok, why = QuotexAPI._is_instrument_payload(candle_rows(4))
    assert ok is False and "NUMBER at index 1" in why

    rows = [[0, "EURUSD", "EUR/USD\n", 0, 0, 85.0] + [0] * 8 + [True] + [0] * 5]
    assert QuotexAPI._is_instrument_payload(rows)[0] is True
    assert QuotexAPI._is_instrument_payload({"list": rows})[0] is True
    assert QuotexAPI._is_instrument_payload({"asset": "EURUSD"})[0] is False
    assert QuotexAPI._is_instrument_payload([])[0] is False
    assert QuotexAPI._is_instrument_payload("junk")[0] is False


@pytest.mark.asyncio
async def test_vendor_handler_preserves_instruments_on_a_candle_frame():
    import sys as _s
    from pathlib import Path as _P
    _s.path.append(str(_P(__file__).resolve().parents[1] / "vendor" / "pyquotex"))
    from pyquotex.api import QuotexAPI
    from pyquotex.utils.async_utils import EventRegistry

    good = [[0, "EURUSD", "EUR/USD\n", 0, 0, 85.0] + [0] * 8 + [True] + [0] * 5]
    api = QuotexAPI.__new__(QuotexAPI)
    api.instruments = list(good)
    api.event_registry = EventRegistry()
    api._temp_status = ""

    await api._h_instruments_list(candle_rows(4))
    assert api.instruments == good, "a candle frame overwrote the instrument list"

    newer = [[0, "GBPUSD", "GBP/USD\n", 0, 0, 80.0] + [0] * 8 + [True] + [0] * 5]
    await api._h_instruments_list(newer)
    assert api.instruments[0][1] == "GBPUSD"
