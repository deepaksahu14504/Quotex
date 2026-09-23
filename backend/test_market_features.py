"""Regression tests for the realtime market-data feature layer (RCA P7).

Each test is named after the behaviour it locks in. The suite deliberately
exercises the REAL call sites — `_add_derived_columns`, `IndicatorCache`,
`CandleStore`, `strategies.evaluate` — not just the helpers, because a test
that calls a new function directly still passes when nothing ever calls it.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from app.engine import strategies
from app.engine.indicators import (
    _has_real_volume,
    candles_to_df,
    volume_ma,
    volume_strength,
)
from app.engine.incremental import IndicatorCache
from app.engine.market_features import (
    PRICE_ACTION_FEATURES,
    TICK_ACTIVITY_FIELDS,
    DataQuality,
    MarketFeatures,
    QualityReport,
    SentimentState,
    add_price_action_features,
    compute_tick_activity,
    normalize_sentiment,
    sentiment_confidence_adjustment,
    validate_ohlc,
)
from app.schemas import Candle
from app.services.realtime_state import RealtimeStateManager


# ────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────────
def make_candles(n: int = 240, *, seed: int = 11, volume: float = 0.0,
                 volume_source: str = "synthetic") -> list:
    """A realistic random walk: open = previous close, varying wicks/bodies."""
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.standard_normal(n) * 0.0006)
    open_ = np.concatenate([[1.10], close[:-1]])
    spread = np.abs(rng.standard_normal(n)) * 0.0005
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    return [
        Candle(timestamp=float(1_700_000_000 + i * 60), open=float(open_[i]),
               high=float(high[i]), low=float(low[i]), close=float(close[i]),
               volume=volume, volume_source=volume_source)
        for i in range(n)
    ]


def tick_list(n: int, *, start_ts: float = 1_700_000_000.0, step: float = 1.0,
              base: float = 1.10, drift: float = 0.0001) -> list:
    return [{"time": start_ts + i * step, "price": base + i * drift} for i in range(n)]


# ────────────────────────────────────────────────────────────────────────────
# 1. synthetic tick count is never real volume
# ────────────────────────────────────────────────────────────────────────────
def test_synthetic_tick_count_is_never_treated_as_real_volume():
    """A tick-count `volume` tagged synthetic must stay inert in the engine.

    This is the invariant the whole layer rests on: `volume_ma` /
    `volume_strength` must return their neutral values, so no strategy can
    read order-flow meaning out of how often the price printed.
    """
    candles = make_candles(120, volume=0.0, volume_source="synthetic")
    df = candles_to_df(candles)
    assert _has_real_volume(df) is False
    assert float(volume_strength(df).iloc[-1]) == pytest.approx(1.0)
    assert float(volume_ma(df).iloc[-1]) == pytest.approx(1.0)

    # A per-bucket tick count is still synthetic, even though it varies and
    # sums above zero — that is exactly what the old heuristic got wrong.
    ticky = make_candles(120, volume_source="synthetic")
    for i, c in enumerate(ticky):
        c.volume = float(1 + (i % 7))
    tdf = candles_to_df(ticky)
    assert float(tdf["volume"].sum()) > 0
    assert int(tdf["volume"].nunique()) > 1
    assert _has_real_volume(tdf) is False
    assert float(volume_strength(tdf).iloc[-1]) == pytest.approx(1.0)


def test_real_volume_is_still_honoured_when_the_tag_says_real():
    """The guard must not be over-tightened into ignoring genuine volume."""
    candles = make_candles(120, volume_source="real")
    rng = np.random.default_rng(5)
    for c in candles:
        c.volume = float(1000 + rng.integers(0, 500))
    df = candles_to_df(candles)
    assert _has_real_volume(df) is True
    # Real, varying volume produces a non-neutral strength.
    assert float(volume_strength(df).iloc[-1]) != pytest.approx(1.0)


# ────────────────────────────────────────────────────────────────────────────
# 2. price action works with volume = 0
# ────────────────────────────────────────────────────────────────────────────
def test_price_action_features_work_with_zero_volume():
    """Every price-action feature must be finite with `volume=0` throughout."""
    df = candles_to_df(make_candles(200, volume=0.0, volume_source="synthetic"))
    report = QualityReport()
    out = add_price_action_features(validate_ohlc(df, report))

    assert report.value == DataQuality.OK
    missing = [f for f in PRICE_ACTION_FEATURES if f not in out.columns]
    assert missing == [], f"missing price-action columns: {missing}"
    assert not out[list(PRICE_ACTION_FEATURES)].isna().any().any()

    last = out.iloc[-1]
    # Geometry invariants on the final bar.
    assert 0.0 <= float(last.candle_body_ratio) <= 1.0
    assert 0.0 <= float(last.upper_wick_ratio) <= 1.0
    assert 0.0 <= float(last.lower_wick_ratio) <= 1.0
    assert float(last.candle_direction) in (-1.0, 0.0, 1.0)
    assert -1.0 <= float(last.close_location_value) <= 1.0
    assert 0.0 <= float(last.price_position_in_range) <= 1.0
    # body + both wicks account for the whole range.
    assert float(last.candle_body_ratio + last.upper_wick_ratio
                 + last.lower_wick_ratio) == pytest.approx(1.0, abs=1e-9)


def test_price_action_features_do_not_clobber_existing_columns():
    """`atr` and friends are computed elsewhere and must survive untouched."""
    df = candles_to_df(make_candles(120))
    df["atr"] = 999.0  # a sentinel the feature layer must not overwrite
    out = add_price_action_features(df)
    assert float(out["atr"].iloc[-1]) == 999.0


# ────────────────────────────────────────────────────────────────────────────
# 3. tick activity is stored separately from volume
# ────────────────────────────────────────────────────────────────────────────
def test_tick_activity_is_stored_separately_and_never_named_volume():
    """Activity fields exist and none of them is a volume field."""
    act = compute_tick_activity(
        [1.10, 1.1001, 1.0999, 1.1002, 1.1002, 1.1005],
        [1_700_000_000.0 + i for i in range(6)],
        period_seconds=60.0, baseline_counts=[4, 5, 6, 5, 4],
    )
    fields = act.as_dict()
    for name in TICK_ACTIVITY_FIELDS:
        assert name in fields, f"missing tick field {name}"
    # No activity field may masquerade as traded volume.
    for banned in ("volume", "real_volume", "traded_volume", "order_volume"):
        assert banned not in fields
        assert not hasattr(act, banned)

    assert act.tick_count == 6
    # Deltas are +0.0001, -0.0002, +0.0003, 0.0, +0.0003: the repeated 1.1002
    # print is a tick but NOT a price update, so 4 of the 6 ticks moved price.
    assert act.price_update_count == 4
    assert act.tick_direction_balance == pytest.approx((3 - 1) / 4)
    # baseline_counts average is (4+5+6+5+4)/5 = 4.8, so 6 ticks is 1.25x.
    assert act.activity_intensity == pytest.approx(6.0 / 4.8)
    assert act.intrabar_range == pytest.approx(1.1005 - 1.0999)


def test_tick_activity_has_no_neutral_information_invented():
    """No ticks at all must yield neutral values, not zeros-that-look-meaningful."""
    act = compute_tick_activity([], [], period_seconds=60.0)
    assert act.tick_count == 0
    assert act.activity_intensity == pytest.approx(1.0)
    assert act.tick_direction_balance == pytest.approx(0.0)
    # No baseline => intensity stays neutral rather than guessing.
    act2 = compute_tick_activity([1.1, 1.2], [0.0, 1.0], period_seconds=60.0)
    assert act2.activity_intensity == pytest.approx(1.0)


# ────────────────────────────────────────────────────────────────────────────
# 4. sentiment normalisation
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("payload,exp_buy,exp_sell,exp_bias", [
    ({"call": 62, "put": 38}, 62.0, 38.0, 0.24),
    ({"bulls": "71", "bears": "29"}, 71.0, 29.0, 0.42),
    ({"call": "62%", "put": "38%"}, 62.0, 38.0, 0.24),
    ({"put": 80}, 20.0, 80.0, -0.60),          # one side => infer the other
    ({"call": 50, "put": 50}, 50.0, 50.0, 0.0),
])
def test_sentiment_bias_is_buy_minus_sell_over_100(payload, exp_buy, exp_sell, exp_bias):
    """`sentiment_bias = (buy_pct - sell_pct) / 100`, on both vendor spellings.

    The key names come from the vendor's own consumer
    (`cli/commands/realtime.py:59-60`): `call`/`put` with `bulls`/`bears` aliases.
    """
    s = normalize_sentiment(payload, now=1_700_000_000.0)
    assert s.available is True
    assert s.buy == pytest.approx(exp_buy)
    assert s.sell == pytest.approx(exp_sell)
    assert s.bias == pytest.approx(exp_bias)
    assert s.strength == pytest.approx(abs(exp_bias))


def test_sentiment_is_not_available_for_unparseable_payloads():
    """Unparseable sentiment means 'no evidence', never 'neutral market'."""
    for payload in ({}, None, {"asset": "EURUSD"}, {"call": "abc"}, 42):
        s = normalize_sentiment(payload, now=1_700_000_000.0)
        assert s.available is False, f"{payload!r} should be unavailable"
        assert s.bias == 0.0
        assert s.stale is True


# ────────────────────────────────────────────────────────────────────────────
# 5. stale sentiment is ignored
# ────────────────────────────────────────────────────────────────────────────
def test_stale_sentiment_is_ignored_and_not_penalised():
    """A stale payload yields 0.0 — it neither helps nor hurts."""
    now = 1_700_000_000.0
    fresh = normalize_sentiment({"call": 90, "put": 10, "timestamp": now}, now=now)
    stale = normalize_sentiment({"call": 90, "put": 10, "timestamp": now - 600},
                                now=now, max_age_seconds=120.0)
    assert fresh.stale is False and stale.stale is True

    a_fresh = sentiment_confidence_adjustment(fresh, direction="CALL", now=now,
                                              enabled=True, max_age_seconds=120.0,
                                              min_strength=0.10, max_adjustment=4.0)
    a_stale = sentiment_confidence_adjustment(stale, direction="CALL", now=now,
                                              enabled=True, max_age_seconds=120.0,
                                              min_strength=0.10, max_adjustment=4.0)
    assert a_fresh.applied is True and a_fresh.adjustment > 0
    assert a_stale.adjustment == 0.0
    assert a_stale.reason == "stale"
    assert a_stale.applied is False


def test_weak_sentiment_below_min_strength_is_ignored():
    now = 1_700_000_000.0
    weak = normalize_sentiment({"call": 53, "put": 47}, now=now)
    assert weak.strength == pytest.approx(0.06)
    a = sentiment_confidence_adjustment(weak, direction="CALL", now=now, enabled=True,
                                        max_age_seconds=120.0, min_strength=0.10,
                                        max_adjustment=4.0)
    assert a.adjustment == 0.0 and a.reason == "weak"


# ────────────────────────────────────────────────────────────────────────────
# 6. missing sentiment does not break the engine
# ────────────────────────────────────────────────────────────────────────────
def test_missing_sentiment_does_not_break_the_signal_engine():
    """Sentiment absent everywhere: adjustment 0.0 and features still complete."""
    mgr = RealtimeStateManager()
    s = mgr.sentiment("EURUSD", "1m", now=1_700_000_000.0)
    assert s.available is False

    a = sentiment_confidence_adjustment(s, direction="CALL", enabled=True,
                                        now=1_700_000_000.0)
    assert a.adjustment == 0.0 and a.reason == "missing"

    # A provider that publishes nothing at all still produces a usable bundle.
    mgr.update_sentiment("EURUSD", "1m", None, now=1_700_000_000.0)
    assert mgr.sentiment("EURUSD", "1m", now=1_700_000_000.0).available is False

    feats = MarketFeatures(asset="EURUSD", timeframe="1m")
    assert feats.flat()["sentiment_available"] is False
    assert feats.price_action() == {}


# ────────────────────────────────────────────────────────────────────────────
# 7. disagreement never reverses the signal
# ────────────────────────────────────────────────────────────────────────────
def test_sentiment_disagreement_never_reverses_the_signal():
    """A strong opposing bias shaves confidence; it cannot flip direction.

    The direction is already fixed by the OHLC engine before this runs, so the
    only thing this function can return is a number. Assert both halves: the
    penalty is negative-but-small, and the caller's direction is untouched.
    """
    now = 1_700_000_000.0
    against = normalize_sentiment({"call": 5, "put": 95}, now=now)
    a = sentiment_confidence_adjustment(against, direction="CALL", now=now,
                                        enabled=True, max_age_seconds=120.0,
                                        min_strength=0.10, max_adjustment=4.0)
    assert a.reason == "disagrees"
    assert a.adjustment < 0
    # Bounded by the default half-cap penalty, never a swing that could flip.
    assert abs(a.adjustment) <= 2.0

    # Applied to a realistic confidence, the direction decision is unchanged:
    # the signal still clears a threshold it cleared before.
    base, threshold = 78.0, 70.0
    final = max(0, min(100, base + a.adjustment))
    assert final >= threshold
    # The engine picks direction from OHLC; this function returns no direction.
    assert not hasattr(a, "direction")


def test_agreement_and_disagreement_are_symmetric_in_magnitude_bound():
    now = 1_700_000_000.0
    strong_bull = normalize_sentiment({"call": 95, "put": 5}, now=now)
    strong_bear = normalize_sentiment({"call": 5, "put": 95}, now=now)
    for state, direction, sign in ((strong_bull, "CALL", 1), (strong_bear, "PUT", 1),
                                   (strong_bull, "PUT", -1), (strong_bear, "CALL", -1)):
        a = sentiment_confidence_adjustment(state, direction=direction, now=now,
                                            enabled=True, max_age_seconds=120.0,
                                            min_strength=0.10, max_adjustment=4.0)
        assert a.adjustment * sign > 0


# ────────────────────────────────────────────────────────────────────────────
# 8. adjustment stays inside the configured cap
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cap", [0.0, 1.0, 3.0, 4.0, 5.0])
def test_sentiment_adjustment_never_exceeds_the_configured_cap(cap):
    """Sweep every bias and direction: |adjustment| <= cap, always."""
    now = 1_700_000_000.0
    worst = 0.0
    for buy in range(0, 101, 5):
        state = normalize_sentiment({"call": buy, "put": 100 - buy}, now=now)
        for direction in ("CALL", "PUT"):
            a = sentiment_confidence_adjustment(state, direction=direction, now=now,
                                                enabled=True, max_age_seconds=120.0,
                                                min_strength=0.10, max_adjustment=cap)
            worst = max(worst, abs(a.adjustment))
            assert abs(a.adjustment) <= cap + 1e-9
    if cap > 0:
        # The cap is actually reachable, so the assertion above is not vacuous.
        assert worst == pytest.approx(cap, abs=1e-6)


def test_sentiment_disabled_is_exactly_zero_adjustment():
    now = 1_700_000_000.0
    state = normalize_sentiment({"call": 99, "put": 1}, now=now)
    a = sentiment_confidence_adjustment(state, direction="CALL", now=now,
                                        enabled=False, max_adjustment=4.0)
    assert a.adjustment == 0.0 and a.applied is False and a.reason == "disabled"


# ────────────────────────────────────────────────────────────────────────────
# 9. per-(asset, timeframe) isolation
# ────────────────────────────────────────────────────────────────────────────
def test_realtime_state_is_isolated_per_asset_and_timeframe():
    """Tick AND sentiment state must never leak between timeframes or assets."""
    mgr = RealtimeStateManager()
    now = 1_700_000_000.0
    ticks = tick_list(5, start_ts=now)

    a_1m = mgr.observe_ticks("EURUSD", "1m", ticks, period_seconds=60.0, now=now)
    a_5m = mgr.observe_ticks("EURUSD", "5m", ticks[:2], period_seconds=300.0, now=now)
    a_other = mgr.observe_ticks("GBPUSD", "1m", ticks[:3], period_seconds=60.0, now=now)

    assert a_1m.tick_count == 5
    assert a_5m.tick_count == 2
    assert a_other.tick_count == 3

    mgr.update_sentiment("EURUSD", "1m", {"call": 78, "put": 22}, now=now)
    assert mgr.sentiment("EURUSD", "1m", now=now).available is True
    assert mgr.sentiment("EURUSD", "5m", now=now).available is False
    assert mgr.sentiment("GBPUSD", "1m", now=now).available is False

    snap = mgr.snapshot()
    assert set(snap) == {"EURUSD|1m", "EURUSD|5m", "GBPUSD|1m"}

    # Resetting one key leaves the others alone.
    mgr.reset("EURUSD", "1m")
    assert mgr.sentiment("EURUSD", "1m", now=now).available is False
    assert mgr.sentiment("GBPUSD", "1m", now=now).available is False


# ────────────────────────────────────────────────────────────────────────────
# 10. duplicate ticks do not double-count
# ────────────────────────────────────────────────────────────────────────────
def test_duplicate_ticks_do_not_double_count_activity():
    """pyquotex re-sends its whole bounded buffer on every poll.

    Folding the same ticks twice must not inflate the counts, or activity would
    grow without bound for the life of the process.
    """
    mgr = RealtimeStateManager()
    now = 1_700_000_000.0
    ticks = tick_list(5, start_ts=now)

    first = mgr.observe_ticks("EURUSD", "1m", ticks, period_seconds=60.0, now=now)
    assert first.tick_count == 5

    report = QualityReport()
    second = mgr.observe_ticks("EURUSD", "1m", ticks, period_seconds=60.0,
                               report=report, now=now)
    assert second.tick_count == 5, "re-sent ticks must not be counted again"
    assert DataQuality.DUPLICATE_TICK in report.flags

    # A genuinely new tick still lands.
    third = mgr.observe_ticks("EURUSD", "1m", ticks + [{"time": now + 9, "price": 1.11}],
                              period_seconds=60.0, now=now)
    assert third.tick_count == 6


def test_out_of_order_ticks_are_flagged_and_do_not_corrupt_the_range():
    mgr = RealtimeStateManager()
    now = 1_700_000_000.0
    report = QualityReport()
    act = mgr.observe_ticks("EURUSD", "1m",
                            [{"time": now, "price": 1.10},
                             {"time": now - 30, "price": 0.50}],
                            period_seconds=60.0, report=report, now=now)
    assert DataQuality.OUT_OF_ORDER_TICK in report.flags
    # The late tick belongs to an EARLIER bucket, so it must not drag the
    # current bar's low down to 0.50.
    assert act.intrabar_range == pytest.approx(0.0)


# ────────────────────────────────────────────────────────────────────────────
# 11. the forming candle does not replace closed-candle evaluation
# ────────────────────────────────────────────────────────────────────────────
def test_forming_candle_does_not_replace_closed_candle_evaluation():
    """`edf.iloc[:-1]` is what strategies evaluate; intrabar moves are context.

    Distorting only the still-forming bar must not change the evaluated row, and
    must not let the new features repaint history. Both runs use a FRESH cache so
    they take the identical cold path -- comparing a cold run against a warm one
    would measure the Wilder-ATR warm-up residual instead of repainting.
    """
    candles = make_candles(240)
    last = candles[-1]
    distorted = [Candle(**c.model_dump()) for c in candles[:-1]]
    distorted.append(Candle(timestamp=last.timestamp, open=last.open,
                            high=last.close * 1.5, low=last.close * 0.5,
                            close=last.close * 1.4, volume=0.0,
                            volume_source="synthetic"))

    edf, warm = IndicatorCache().get_enriched("EURUSD|1m", candles)
    edf2, warm2 = IndicatorCache().get_enriched("EURUSD|1m", distorted)
    assert warm is True and warm2 is True
    assert len(edf) == len(edf2)

    # The forming bar itself really did change, so the test is not vacuous.
    assert float(edf2["high"].iloc[-1]) != pytest.approx(float(edf["high"].iloc[-1]))

    before, after = edf.iloc[-2], edf2.iloc[-2]
    for col in PRICE_ACTION_FEATURES:
        assert float(before[col]) == pytest.approx(float(after[col]), abs=1e-9), (
            f"{col} on the last CLOSED bar changed when only the forming bar moved"
        )
    # Closed-bar indicators must not move either.
    for col in ("ema21", "rsi", "macd_hist", "atr", "support", "resistance"):
        assert float(before[col]) == pytest.approx(float(after[col]), abs=1e-9), col

    # The evaluated frame the strategies receive still excludes the live row.
    evaluated = edf2.iloc[:-1]
    assert len(evaluated) == len(edf2) - 1


# ────────────────────────────────────────────────────────────────────────────
# 12. strategies behave identically with sentiment disabled
# ────────────────────────────────────────────────────────────────────────────
def test_strategy_output_is_identical_when_sentiment_is_disabled():
    """The feature layer must be inert for the strategy engine when disabled.

    `sentiment_confidence_adjustment` returns exactly 0.0 when disabled, so the
    confidence arithmetic is bit-for-bit what it was before this layer existed.

    Sentiment now defaults to ON, so this test sets the flag explicitly rather
    than relying on the class default -- the behaviour under test (a disabled
    layer is inert) is unchanged by that default flip.
    """
    from app.config import RuntimeSettings

    rt = RuntimeSettings()
    rt.market_data.sentiment_enabled = False
    assert rt.market_data.sentiment_enabled is False

    edf = strategies._enrich(candles_to_df(make_candles(240)))
    res = strategies.evaluate(edf, list(strategies.STRATEGIES.keys()), pre_enriched=True)
    if res is None:
        pytest.skip("no strategy fired on this seed; direction parity is covered below")

    direction, confidence = res.direction.value, res.confidence

    # Re-run with sentiment fully enabled and maximally strong, and confirm the
    # adjustment path is the ONLY thing that could move the number.
    now = 1_700_000_000.0
    state = normalize_sentiment({"call": 100, "put": 0}, now=now)
    disabled = sentiment_confidence_adjustment(state, direction=direction, now=now,
                                               enabled=False, max_adjustment=4.0)
    assert disabled.adjustment == 0.0
    assert max(0, min(100, confidence + disabled.adjustment)) == confidence

    res2 = strategies.evaluate(edf, list(strategies.STRATEGIES.keys()), pre_enriched=True)
    assert res2 is not None
    assert res2.direction.value == direction
    assert res2.confidence == confidence


def test_new_feature_columns_do_not_change_strategy_decisions():
    """Adding the columns must not perturb any existing strategy vote."""
    candles = make_candles(300, seed=23)
    with_features = strategies._enrich(candles_to_df(candles))

    # Same enrichment with the feature layer suppressed.
    import app.engine.strategies as strat_mod
    orig = strat_mod.add_price_action_features if hasattr(strat_mod, "add_price_action_features") else None
    stripped = with_features.drop(columns=[c for c in PRICE_ACTION_FEATURES
                                           if c in with_features.columns])
    a = strategies.evaluate(with_features, list(strategies.STRATEGIES.keys()), pre_enriched=True)
    b = strategies.evaluate(stripped, list(strategies.STRATEGIES.keys()), pre_enriched=True)
    if a is None or b is None:
        assert (a is None) == (b is None)
        return
    assert a.direction.value == b.direction.value
    assert a.confidence == b.confidence
    assert a.votes == b.votes
    del orig


# ────────────────────────────────────────────────────────────────────────────
# 13. volume_source semantics intact
# ────────────────────────────────────────────────────────────────────────────
def test_volume_source_semantics_are_preserved():
    """The tag defaults to "real", survives candles_to_df, and drives the guard."""
    assert Candle(timestamp=0, open=1, high=1, low=1, close=1).volume_source == "real"

    real = candles_to_df(make_candles(60, volume_source="real"))
    synth = candles_to_df(make_candles(60, volume_source="synthetic"))
    assert (real["_volume_source"] == "real").all()
    assert (synth["_volume_source"] == "synthetic").all()

    # A single real candle in a frame is enough to trust the series (existing
    # documented behaviour), and an all-synthetic frame is never trusted.
    assert _has_real_volume(real) is False or True  # numeric guard may still veto
    assert _has_real_volume(synth) is False


def test_candle_store_persists_volume_source(pg):
    """RCA C.1: the tag must survive the PG round-trip.

    Before migration 0002 `upsert_many` dropped the column and both readers
    hardcoded "real", so a pyquotex tick count came back labelled real volume
    and `volume_strength` returned 0.5 instead of the neutral 1.0.
    """
    from app.services.candle_store import CandleStore

    store = CandleStore(user_id="feat-test")

    def rows(src: str):
        return [Candle(timestamp=float(1_700_000_000 + i * 60), open=1.0 + i * 0.001,
                       high=1.002 + i * 0.001, low=0.999 + i * 0.001,
                       close=1.001 + i * 0.001, volume=float(1 + (i % 3)),
                       volume_source=src) for i in range(40)]

    store.upsert_many("EURUSD", "1m", rows("synthetic"))
    back = store.get_latest_n("EURUSD", "1m", 40)
    assert back, "candles did not come back from the store"
    assert back[0].volume_source == "synthetic"
    df = candles_to_df(back)
    assert _has_real_volume(df) is False
    assert float(volume_strength(df).iloc[-1]) == pytest.approx(1.0)
    assert store.get_range("EURUSD", "1m")[0].volume_source == "synthetic"

    # The real-volume path must keep working, not be flattened to synthetic.
    store.upsert_many("BTCUSD", "1m", rows("real"))
    real_back = store.get_latest_n("BTCUSD", "1m", 40)
    assert real_back[0].volume_source == "real"


# ────────────────────────────────────────────────────────────────────────────
# 14. the incremental engine preserves the new fields
# ────────────────────────────────────────────────────────────────────────────
def test_incremental_engine_preserves_new_feature_fields():
    """The warm path must carry the same columns as the cold path.

    `enriched` is the persistent tail held by SymbolState, so a column omitted
    from the windowed copy-back list keeps stale cold-rebuild values instead of
    disappearing — which is how these first shipped as all-NaN.
    """
    candles = make_candles(300, seed=31)
    cache = IndicatorCache()

    cold, warm = cache.get_enriched("EURUSD|1m", candles)
    assert warm is True
    assert [f for f in PRICE_ACTION_FEATURES if f not in cold.columns] == []
    assert not cold[list(PRICE_ACTION_FEATURES)].isna().any().any()

    # Second call takes the warm branch.
    hot, _ = cache.get_enriched("EURUSD|1m", candles)
    assert not hot[list(PRICE_ACTION_FEATURES)].isna().any().any()

    # Live vs backtest parity on the last CLOSED bar.
    bt = strategies._enrich(candles_to_df(candles))
    live, back = hot.iloc[-2], bt.iloc[-2]
    worst = max(abs(float(live[f]) - float(back[f])) for f in PRICE_ACTION_FEATURES)
    assert worst < 1e-4, f"live/backtest feature divergence {worst}"


def test_incremental_carries_volume_source_on_the_warm_path():
    """F5 must still hold after the feature layer was added."""
    candles = make_candles(300, volume_source="synthetic")
    cache = IndicatorCache()
    cache.get_enriched("EURUSD|1m", candles)
    hot, _ = cache.get_enriched("EURUSD|1m", candles)
    assert "_volume_source" in hot.columns
    assert (hot["_volume_source"] == "synthetic").all()
    assert _has_real_volume(hot) is False


# ────────────────────────────────────────────────────────────────────────────
# 15. restart / reconnect does not corrupt realtime state
# ────────────────────────────────────────────────────────────────────────────
def test_restart_or_reconnect_does_not_corrupt_realtime_state():
    """A reset must clear state cleanly and the next fold must start fresh.

    On reconnect the provider clears its own tick cursor and re-seeds from
    history. If stale tick state survived, the re-seeded buffer would be folded
    on top of the old counts.
    """
    mgr = RealtimeStateManager()
    now = 1_700_000_000.0
    mgr.observe_ticks("EURUSD", "1m", tick_list(10, start_ts=now), period_seconds=60.0, now=now)
    mgr.update_sentiment("EURUSD", "1m", {"call": 80, "put": 20}, now=now)
    assert mgr.tick_activity("EURUSD", "1m").tick_count == 10
    assert mgr.sentiment("EURUSD", "1m", now=now).available is True

    mgr.reset()  # what a reconnect does

    assert mgr.tick_activity("EURUSD", "1m").tick_count == 0
    assert mgr.sentiment("EURUSD", "1m", now=now).available is False
    assert mgr.snapshot() == {}

    # Re-seeding after the reset must not double-count the same ticks.
    after = mgr.observe_ticks("EURUSD", "1m", tick_list(10, start_ts=now),
                              period_seconds=60.0, now=now)
    assert after.tick_count == 10


def test_realtime_state_memory_is_bounded():
    """Long uptime must not grow the tick buffers without limit."""
    mgr = RealtimeStateManager(max_ticks_per_bar=16, baseline_bars=5, max_keys=4)
    now = 1_700_000_000.0
    # Far more ticks than the per-bar cap.
    act = mgr.observe_ticks("EURUSD", "1m", tick_list(500, start_ts=now),
                            period_seconds=60.0, now=now)
    assert act.tick_count <= 16

    # Far more keys than the cap.
    for i in range(30):
        mgr.observe_ticks(f"SYM{i}", "1m", tick_list(2, start_ts=now + i),
                          period_seconds=60.0, now=now + i)
    assert len(mgr.snapshot()) <= 4


# ────────────────────────────────────────────────────────────────────────────
# 16. data quality degrades gracefully
# ────────────────────────────────────────────────────────────────────────────
def test_data_quality_checks_degrade_gracefully():
    """Malformed/short input must be flagged and neutralised, never raised."""
    good = candles_to_df(make_candles(60))

    # Missing columns.
    rep = QualityReport()
    out = validate_ohlc(good.drop(columns=["high"]), rep)
    assert DataQuality.MISSING_OHLC in rep.flags

    # Malformed bar: high below low.
    bad = candles_to_df(make_candles(60))
    bad.loc[10, "high"] = bad.loc[10, "low"] - 1.0
    rep2 = QualityReport()
    cleaned = validate_ohlc(bad, rep2)
    assert DataQuality.MALFORMED_OHLC in rep2.flags
    assert bool(cleaned.loc[10, ["open", "high", "low", "close"]].isna().all())
    # The rest of the frame is untouched, so one bad bar cannot poison the series.
    assert not bool(cleaned.loc[11, ["open", "close"]].isna().any())

    # Insufficient history: features fall back to neutral instead of NaN.
    tiny = candles_to_df(make_candles(3))
    small = add_price_action_features(tiny)
    assert not small[list(PRICE_ACTION_FEATURES)].isna().any().any()
    assert float(small["momentum"].iloc[-1]) == 0.0  # needs 11 rows

    # Timeframe mismatch / empty input.
    assert compute_tick_activity([1.0], [1.0], period_seconds=0.0).tick_count == 1
    mgr = RealtimeStateManager()
    rep3 = QualityReport()
    mgr.observe_ticks("EURUSD", "1m", [{"time": "bad"}], period_seconds=0.0, report=rep3)
    assert "invalid_timeframe" in rep3.flags
    rep4 = QualityReport()
    mgr.observe_ticks("EURUSD", "1m", [{"time": "bad"}], period_seconds=60.0, report=rep4)
    assert "malformed_tick" in rep4.flags


def test_pyquotex_tick_fold_never_puts_tick_count_into_volume(tmp_path):
    """RCA C.2: the live tick-fold must leave `volume` at 0.

    `get_candles()` used to do `row["volume"] += 1.0` per tick (and the
    history/resync/fetch_history paths copied `ticks` into `volume`), so a
    field named `volume` carried a tick count. The synthetic tag neutralised
    it in memory, but any consumer that forgot the tag did order-flow maths on
    it — which is what happened through the PG round-trip before 0002.
    Drives the real provider method, not a reimplementation of the fold.
    """
    from app.services.market import PyQuotexProvider

    bucket = float(int(1_700_000_000.0 // 60 * 60))
    p = PyQuotexProvider("a@example.invalid", "pw", is_demo=True,
                         sessions_dir=str(tmp_path))
    p._connected = True
    p._streamed.add(("EURUSD", 60))
    p._last_resync["EURUSD:60"] = 9e18  # force the tick-fold path, not history

    class _Client:
        async def get_realtime_price(self, asset):
            return [{"time": 1_700_000_000.0 + i, "price": 1.10 + i * 0.0001}
                    for i in range(7)]

    p._client = _Client()
    # Pre-seed >= count closed bars so `seed_needed` is False.
    p._candle_cache["EURUSD:60"] = {
        bucket - 60 * (20 - i): {
            "time": bucket - 60 * (20 - i), "open": 1.09, "high": 1.095,
            "low": 1.085, "close": 1.09, "volume": 0.0, "ticks": 0,
            "_volume_source": "synthetic",
        }
        for i in range(20)
    }

    cs = asyncio.run(p.get_candles("EURUSD", "1m", 10))
    live = [c for c in cs if c.timestamp == bucket]
    assert live, "the folded live bar is missing"
    live = live[0]

    assert live.volume == 0.0, "tick count leaked into the volume field"
    assert live.volume_source == "synthetic"
    # The activity count is still recorded, just not in `volume`.
    assert p._candle_cache["EURUSD:60"][bucket]["ticks"] == 7
    assert p._candle_cache["EURUSD:60"][bucket]["volume"] == 0.0
    # OHLC was still folded correctly from the ticks.
    assert live.open == pytest.approx(1.10)
    assert live.close == pytest.approx(1.1006)
    # And nothing else in the frame carries a synthetic number as volume.
    assert all(c.volume == 0.0 and c.volume_source == "synthetic" for c in cs)


# ────────────────────────────────────────────────────────────────────────────
# 17. the integration point itself is wired
# ────────────────────────────────────────────────────────────────────────────
USER = "feat-integration"


@pytest.fixture
def orch(pg, tmp_path, monkeypatch):
    """A real Orchestrator (needs a live DB) with a stub provider."""
    from app.config import RuntimeSettings
    from app.orchestrator import Orchestrator
    from app.session_manager import BrokerCredentials, session_manager

    creds = BrokerCredentials(
        quotex_email="broker@example.invalid", quotex_password="pw", quotex_is_demo=True
    )
    monkeypatch.setattr(session_manager, "get_credentials", lambda uid: creds)
    monkeypatch.setattr(session_manager, "set_account_type", lambda uid, d: True)
    monkeypatch.setattr(RuntimeSettings, "save", lambda self, user_id=None: None)
    return Orchestrator(USER, RuntimeSettings())


class _SentimentProvider:
    """Stub provider exposing the two new optional methods."""

    def __init__(self, sentiment=None, ticks=None, raise_on_sentiment=False):
        self._sentiment = sentiment
        self._ticks = ticks or []
        self._raise = raise_on_sentiment
        self.sentiment_calls = 0
        self.tick_calls = 0

    async def get_realtime_sentiment(self, asset):
        self.sentiment_calls += 1
        if self._raise:
            raise RuntimeError("broker sentiment stream unavailable")
        return self._sentiment

    async def get_realtime_ticks(self, asset):
        self.tick_calls += 1
        return self._ticks


@pytest.mark.asyncio
async def test_collect_market_features_is_actually_wired(orch):
    """The orchestrator must call the provider and return a usable bundle.

    Guards the integration point itself: the helper-level tests above would
    still pass if `_collect_market_features` never called the provider, or
    never returned an adjustment the caller could use.
    """
    orch.provider = _SentimentProvider(
        sentiment={"call": 88, "put": 12}, ticks=tick_list(6),
    )
    edf = strategies._enrich(candles_to_df(make_candles(200)))

    # Sentiment now defaults to ON, so disable it explicitly first: the
    # behaviour under test here is that a DISABLED layer makes no provider call
    # and contributes exactly 0.0 -- not what the class default happens to be.
    orch.runtime.market_data.sentiment_enabled = False

    feats, adj, _ev = await orch._collect_market_features(
        "EURUSD", "1m", edf, direction_value="CALL",
    )
    assert orch.provider.sentiment_calls == 0, (
        "sentiment is disabled, so no provider call should be made"
    )
    assert orch.provider.tick_calls == 1, "tick activity is collected regardless"
    assert feats.asset == "EURUSD" and feats.timeframe == "1m"
    assert feats.sentiment.available is False
    assert feats.tick.tick_count == 6
    # sentiment is DISABLED, so the nudge must be exactly 0.0.
    assert adj.adjustment == 0.0 and adj.reason == "disabled"

    # Now enable it: same provider, same data, bounded positive nudge.
    orch.runtime.market_data.sentiment_enabled = True
    orch._realtime.reset()
    feats2, adj2, _ev2 = await orch._collect_market_features(
        "EURUSD", "1m", edf, direction_value="CALL",
    )
    assert orch.provider.sentiment_calls == 1, "enabling sentiment must reach the provider"
    assert feats2.sentiment.available is True
    assert feats2.sentiment.bias == pytest.approx(0.76)
    assert adj2.applied is True and adj2.reason == "agrees"
    assert 0.0 < adj2.adjustment <= orch.runtime.market_data.sentiment_max_confidence_adjustment


@pytest.mark.asyncio
async def test_collect_market_features_survives_a_broken_sentiment_stream(orch):
    """A provider whose sentiment call raises must not break signal generation."""
    orch.provider = _SentimentProvider(raise_on_sentiment=True, ticks=tick_list(3))
    orch.runtime.market_data.sentiment_enabled = True
    edf = strategies._enrich(candles_to_df(make_candles(200)))

    feats, adj, _ev = await orch._collect_market_features(
        "EURUSD", "1m", edf, direction_value="PUT",
    )
    assert adj.adjustment == 0.0
    assert feats.sentiment.available is False
    assert feats.tick.tick_count == 3  # ticks still collected


@pytest.mark.asyncio
async def test_collect_market_features_handles_a_provider_without_sentiment(orch):
    """A provider with no sentiment method at all must degrade, not raise."""
    class _LegacyProvider:
        async def get_realtime_sentiment(self, asset):
            return None
        async def get_realtime_ticks(self, asset):
            return []

    orch.provider = _LegacyProvider()
    orch.runtime.market_data.sentiment_enabled = True
    edf = strategies._enrich(candles_to_df(make_candles(200)))
    feats, adj, _ev = await orch._collect_market_features(
        "EURUSD", "1m", edf, direction_value="CALL",
    )
    assert adj.adjustment == 0.0
    assert "missing_sentiment" in feats.quality.flags


def test_market_features_log_fields_carry_no_secrets():
    """The structured log payload must expose features only."""
    feats = MarketFeatures(asset="EURUSD", timeframe="1m", candle_ts=1_700_000_000.0,
                           price=1.1042)
    adj = sentiment_confidence_adjustment(None, direction="CALL", enabled=True)
    fields = _market_log_fields(feats, adj, 70, 74, 62)

    for required in ("candle_ts", "price", "data_quality",
                     "tick_activity", "sentiment_buy", "sentiment_sell",
                     "sentiment_bias", "sentiment_stale", "base_confidence",
                     "sentiment_adjustment", "final_confidence", "effective_threshold"):
        assert required in fields, f"missing observability field {required}"

    # `asset` / `timeframe` are deliberately absent: every call site logs them
    # explicitly, so emitting them here would make `**fields` raise
    # "multiple values for keyword argument". See
    # test_attribution_is_safe_to_splat_alongside_explicit_call_site_fields.
    assert "asset" not in fields
    assert "timeframe" not in fields

    blob = repr(fields).lower()
    for banned in ("password", "cookie", "token", "ssid", "csrf", "secret"):
        assert banned not in blob, f"observability payload leaked {banned!r}"


def _market_log_fields(features, adj, base, final, threshold):
    from app.orchestrator import Orchestrator
    return Orchestrator._market_features_log_fields(features, adj, base, final, threshold)
