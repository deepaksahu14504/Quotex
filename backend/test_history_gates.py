"""Regression tests for RCA F11 (warm-up gate) and F12 (gap tolerance).

Both live in `app/services/market_init.py`, the state machine that decides
which assets are READY to trade. Both made assets silently unusable rather
than raising anything.

F11 — THE WARM-UP GATE SAT EXACTLY ON THE BROKER'S CEILING
----------------------------------------------------------
`trading.min_candles_for_signal` defaults to 120, and `_init_one` certified
assets with `min_bars = max(55, that)`. But 120 is exactly this broker's
history ceiling -- config.py's own loader clamps anything higher back to 120
with the comment "which no asset can reach". Measured directly against
IndicatorCache.get_enriched:

    broker serves 118 bars -> warm/READY False
    broker serves 119 bars -> warm/READY False
    broker serves 120 bars -> warm/READY True

So an asset returning one bar less than the ceiling never became READY, and
`_candidate_assets()` filters READY-only -- the scan reported
SCAN_CYCLE_NO_CANDIDATES forever with no error explaining why.

F12 — ONE MISSING BAR THREW AWAY THE WHOLE PULL
-----------------------------------------------
`_validate_candles` returned False on the FIRST gap larger than
`gap_multiplier` x interval. pyquotex serves history in chunks and never pads
the holes a dropped chunk leaves, so a single lost chunk discarded 119
perfectly good bars, the asset went FAILED, and `_recovery_loop` re-attempted
it every `recovery_interval_seconds` (60s) for as long as the hole stayed in
the served window. `gap_tolerance` bounds how many holes one pull may contain.
Duplicate / non-increasing timestamps stay a hard reject -- those are corrupt
data, not missing data.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.config import MarketInitSettings, RuntimeSettings
from app.engine.incremental import IndicatorCache
from app.schemas import Candle
from app.services.market_init import (
    TIMEFRAMES,
    _validate_candles,
    effective_min_bars,
)


def _series(n: int, tf_seconds: int = 60, seed: int = 3):
    rng = np.random.default_rng(seed)
    ts = 1_700_000_000.0
    price = 1.10
    out = []
    for i in range(n):
        o = price
        c = o + rng.normal(0, 0.0004)
        h = max(o, c) + abs(rng.normal(0, 0.0002))
        l = min(o, c) - abs(rng.normal(0, 0.0002))
        out.append(Candle(
            timestamp=ts + i * tf_seconds, open=o, high=h, low=l, close=c,
            volume=float(rng.integers(1, 40)), volume_source="synthetic",
        ))
        price = c
    return out


def _with_gaps(n: int, drop_at, tf_seconds: int = 60):
    """A clean series with whole bars removed at the given indices, leaving
    holes pyquotex-style -- nothing padded."""
    full = _series(n, tf_seconds)
    return [c for i, c in enumerate(full) if i not in set(drop_at)]


# --------------------------------------------------------------------------- #
# F11 — effective_min_bars
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("configured,observed,expected", [
    (120, 240, 120),   # plenty of history -> configured value, unchanged
    (120, 120, 120),   # exactly at the gate
    (120, 119, 119),   # THE regression: one bar short must not disqualify
    (120, 80, 80),     # relax to what was served
    (120, 30, 55),     # never below the absolute floor
    (200, 120, 120),   # a higher configured value still adapts
    (55, 40, 55),      # already at the floor
])
def test_effective_min_bars(configured, observed, expected):
    assert effective_min_bars(configured, observed) == expected


def test_effective_min_bars_never_goes_below_the_indicator_floor():
    """55 is where the indicator maths stops returning NaN
    (IndicatorCache.ABSOLUTE_MIN_BARS). Relaxing past it would certify assets
    on numbers that are literally NaN."""
    for observed in (0, 1, 10, 54, 55):
        assert effective_min_bars(120, observed) >= 55


def test_effective_min_bars_never_raises_the_gate():
    """It relaxes; it must never demand more than configured."""
    for observed in (0, 60, 120, 500):
        assert effective_min_bars(120, observed) <= 120


def test_the_configured_default_is_exactly_the_broker_ceiling():
    """Documents why F11 bites: the default sits on the edge, not inside it."""
    assert int(RuntimeSettings().trading.min_candles_for_signal) == 120


def test_asset_one_bar_short_is_certifiable_after_relaxation():
    """End-to-end through the real IndicatorCache: 119 bars fails the
    configured gate but passes the relaxed one."""
    candles = _series(119)
    configured = max(55, int(RuntimeSettings().trading.min_candles_for_signal))
    cache = IndicatorCache()
    _edf, warm = cache.get_enriched("EURUSD|1m", candles, min_bars=configured)
    assert warm is False, "premise changed: 119 bars now clears the configured gate"

    relaxed = effective_min_bars(configured, len(candles))
    cache2 = IndicatorCache()
    _edf2, warm2 = cache2.get_enriched("EURUSD|1m", candles, min_bars=relaxed)
    assert warm2 is True, "the relaxed gate did not certify a servable asset"


def test_short_history_is_still_refused():
    """Relaxation is bounded: a genuinely thin series must still fail."""
    candles = _series(40)
    cache = IndicatorCache()
    _edf, warm = cache.get_enriched("EURUSD|1m", candles,
                                    min_bars=effective_min_bars(120, len(candles)))
    assert warm is False


# --------------------------------------------------------------------------- #
# F12 — gap tolerance
# --------------------------------------------------------------------------- #

def test_clean_series_passes():
    ok, reason = _validate_candles(_series(120), "1m", 1.5, gap_tolerance=2)
    assert ok is True, reason


def test_empty_series_is_rejected():
    ok, reason = _validate_candles([], "1m", 1.5, gap_tolerance=2)
    assert ok is False
    assert "no candles" in reason


def test_a_single_gap_within_tolerance_passes():
    """THE F12 regression: one dropped chunk used to fail the whole pull."""
    candles = _with_gaps(120, drop_at=[60])
    ok, reason = _validate_candles(candles, "1m", 1.5, gap_tolerance=2)
    assert ok is True, reason


def test_gaps_beyond_tolerance_are_rejected():
    candles = _with_gaps(120, drop_at=[30, 60, 90])
    ok, reason = _validate_candles(candles, "1m", 1.5, gap_tolerance=2)
    assert ok is False
    assert "3 gap(s)" in reason


def test_zero_tolerance_restores_the_old_fail_on_first_gap():
    candles = _with_gaps(120, drop_at=[60])
    ok, _reason = _validate_candles(candles, "1m", 1.5, gap_tolerance=0)
    assert ok is False


def test_default_tolerance_matches_the_setting():
    candles = _with_gaps(120, drop_at=[60])
    settings = MarketInitSettings()
    ok, _reason = _validate_candles(
        candles, "1m", settings.gap_multiplier,
        gap_tolerance=int(getattr(settings, "gap_tolerance", 0)),
    )
    assert ok is True


def test_duplicate_timestamp_is_a_hard_reject_regardless_of_tolerance():
    """Corrupt data, not missing data -- tolerance must not excuse it."""
    candles = _series(120)
    candles[61] = candles[60].model_copy()          # same timestamp twice
    ok, reason = _validate_candles(candles, "1m", 1.5, gap_tolerance=99)
    assert ok is False
    assert "non-increasing/duplicate" in reason


def test_out_of_order_input_is_sorted_not_rejected():
    """The validator sorts by timestamp before scanning, so a shuffled pull is
    repaired rather than refused. That makes the "non-increasing" branch
    reachable only for duplicates (delta <= 0), which the test above covers."""
    candles = _series(120)
    shuffled = candles[:]
    shuffled[60], shuffled[61] = shuffled[61], shuffled[60]
    ok, reason = _validate_candles(shuffled, "1m", 1.5, gap_tolerance=0)
    assert ok is True, reason


def test_tolerance_is_configured_not_hardcoded():
    assert hasattr(MarketInitSettings(), "gap_tolerance")
    assert MarketInitSettings().gap_tolerance >= 1, (
        "the default must actually tolerate something, or F12 is unfixed"
    )


def test_gap_multiplier_still_defines_what_counts_as_a_gap():
    """A bar spacing within gap_multiplier x interval is not a gap at all."""
    tf_seconds = TIMEFRAMES["1m"]
    candles = _series(120, tf_seconds)
    # Nudge one bar to 1.4x the interval: under the 1.5x multiplier.
    for i in range(62, len(candles)):
        candles[i] = candles[i].model_copy(
            update={"timestamp": candles[i].timestamp + 0.4 * tf_seconds * (i - 61)}
        )
    ok, _reason = _validate_candles(candles, "1m", 1.5, gap_tolerance=0)
    assert ok is True


def test_a_multi_timeframe_series_uses_its_own_interval():
    """5m bars are 300s apart; judging them against the 1m interval would call
    every bar a gap."""
    candles = _series(60, tf_seconds=TIMEFRAMES["5m"])
    ok, reason = _validate_candles(candles, "5m", 1.5, gap_tolerance=0)
    assert ok is True, reason


# --------------------------------------------------------------------------- #
# The WIRING: _init_one must actually apply both fixes
# --------------------------------------------------------------------------- #
#
# The tests above cover `effective_min_bars` and `_validate_candles` directly,
# so they would still pass if `_init_one` never called them. These drive the
# real state machine, because the whole point of both fixes is what happens to
# an asset's READY state.

class _StubErrorRecovery:
    async def execute_with_retry(self, fn, _service, *args, **kwargs):
        return await fn(*args, **kwargs)


class _StubCandleStore:
    def __init__(self):
        self.saved = []

    def upsert_many(self, symbol, timeframe, candles):
        self.saved.append((symbol, timeframe, len(candles)))


class _StubProvider:
    name = "stub"

    def __init__(self, candles):
        self._candles = candles
        self.calls = 0

    async def get_candles(self, asset, timeframe, count=120):
        self.calls += 1
        return list(self._candles)


def _manager(candles, min_bars=120, gap_tolerance=2):
    from app.engine.incremental import IndicatorCache
    from app.services.market_init import AssetInitRecord, MarketInitManager

    settings = MarketInitSettings()
    settings.gap_tolerance = gap_tolerance

    provider = _StubProvider(candles)
    mgr = MarketInitManager(
        user_id="f11-f12-user",
        get_provider=lambda: provider,
        indicators=IndicatorCache(),
        candle_store=_StubCandleStore(),
        error_recovery=_StubErrorRecovery(),
        get_timeframe=lambda: "1m",
        get_settings=lambda: settings,
        get_min_bars=lambda: min_bars,
    )
    mgr._records["EURUSD"] = AssetInitRecord(symbol="EURUSD")
    return mgr, provider


@pytest.mark.asyncio
async def test_init_one_certifies_an_asset_one_bar_short():
    """THE F11 regression, through the real state machine. Pre-fix this asset
    went FAILED forever and _candidate_assets() found no candidates."""
    from app.services.market_init import AssetInitState

    mgr, _provider = _manager(_series(119), min_bars=120)
    await mgr._init_one("EURUSD", worker_id=0)

    rec = mgr._records["EURUSD"]
    assert rec.state is AssetInitState.READY, (
        f"an asset serving 119 servable bars was left {rec.state.value}: {rec.last_error}"
    )
    assert rec.warm is True


@pytest.mark.asyncio
async def test_init_one_still_refuses_a_genuinely_thin_asset():
    """Relaxation is bounded by the absolute floor -- this must NOT be READY."""
    from app.services.market_init import AssetInitState

    mgr, _provider = _manager(_series(40), min_bars=120)
    await mgr._init_one("EURUSD", worker_id=0)

    rec = mgr._records["EURUSD"]
    assert rec.state is not AssetInitState.READY
    assert rec.warm is False


@pytest.mark.asyncio
async def test_init_one_does_not_relax_when_history_is_sufficient():
    from app.services.market_init import AssetInitState

    mgr, _provider = _manager(_series(150), min_bars=120)
    await mgr._init_one("EURUSD", worker_id=0)

    assert mgr._records["EURUSD"].state is AssetInitState.READY


@pytest.mark.asyncio
async def test_init_one_accepts_a_small_gap():
    """THE F12 regression, through the real state machine."""
    from app.services.market_init import AssetInitState

    mgr, _provider = _manager(_with_gaps(140, drop_at=[70]), min_bars=120, gap_tolerance=2)
    await mgr._init_one("EURUSD", worker_id=0)

    rec = mgr._records["EURUSD"]
    assert rec.state is AssetInitState.READY, (
        f"one dropped chunk left the asset {rec.state.value}: {rec.last_error}"
    )


@pytest.mark.asyncio
async def test_init_one_rejects_gaps_beyond_tolerance():
    from app.services.market_init import AssetInitState

    mgr, _provider = _manager(
        _with_gaps(140, drop_at=[30, 60, 90, 110]), min_bars=120, gap_tolerance=2
    )
    await mgr._init_one("EURUSD", worker_id=0)

    rec = mgr._records["EURUSD"]
    assert rec.state is not AssetInitState.READY
    assert rec.last_error and "gap" in rec.last_error


@pytest.mark.asyncio
async def test_init_one_rejects_duplicate_timestamps_even_with_tolerance():
    from app.services.market_init import AssetInitState

    candles = _series(140)
    candles[71] = candles[70].model_copy()
    mgr, _provider = _manager(candles, min_bars=120, gap_tolerance=99)
    await mgr._init_one("EURUSD", worker_id=0)

    rec = mgr._records["EURUSD"]
    assert rec.state is not AssetInitState.READY
    assert rec.last_error and "duplicate" in rec.last_error
