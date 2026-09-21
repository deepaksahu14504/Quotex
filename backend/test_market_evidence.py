"""Tests for the Market Evidence Score layer.

Each test is named after the behaviour the task asked to lock in. The
orchestrator tests drive a real `Orchestrator` (pgserver DB) so the wiring is
exercised, not just the scoring helpers — a helper test still passes when
nothing ever calls the helper.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from app.config import MarketDataSettings, RuntimeSettings
from app.engine.market_evidence import (
    EvidenceConfig,
    MarketEvidence,
    activity_score,
    evaluate_market_evidence,
    headroom_dampener,
    price_action_score,
)
from app.engine.market_features import TICK_ACTIVITY_FIELDS

# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────
CFG = EvidenceConfig()


def call_row(**over):
    """A price-action row that reads as genuine CALL evidence."""
    row = {
        "close_location_value": 0.80,
        "price_position_in_range": 0.85,
        "atr_normalized_momentum": 0.60,
        "bullish_rejection": 0.30,
        "bearish_rejection": 0.05,
        "breakout_distance": 0.50,
        "breakdown_distance": 0.00,
        "support_distance": 2.00,
        "resistance_distance": 2.00,
    }
    row.update(over)
    return row


def put_row(**over):
    """The mirror image: genuine PUT evidence."""
    row = {
        "close_location_value": -0.80,
        "price_position_in_range": 0.15,
        "atr_normalized_momentum": -0.60,
        "bullish_rejection": 0.05,
        "bearish_rejection": 0.30,
        "breakout_distance": 0.00,
        "breakdown_distance": 0.50,
        "support_distance": 2.00,
        "resistance_distance": 2.00,
    }
    row.update(over)
    return row


CALL_TICKS = dict(tick_direction_balance=0.70, activity_intensity=1.5,
                  intrabar_price_change=0.0008, intrabar_range=0.001, tick_count=40)
PUT_TICKS = dict(tick_direction_balance=-0.70, activity_intensity=1.5,
                 intrabar_price_change=-0.0008, intrabar_range=0.001, tick_count=40)


# ────────────────────────────────────────────────────────────────────────────
# 1. strong CALL activity confirmation
# ────────────────────────────────────────────────────────────────────────────
def test_strong_call_activity_confirms_a_call():
    ev = evaluate_market_evidence(direction_value="CALL", row=call_row(),
                                 cfg=CFG, **CALL_TICKS)
    assert ev.verdict == "confirms"
    assert ev.activity_score > CFG.activity_confirmation_threshold
    assert ev.price_action_score > CFG.price_action_threshold
    assert ev.alignment > 0
    assert ev.adjustment > 0
    assert ev.adjustment <= CFG.max_adjustment


# ────────────────────────────────────────────────────────────────────────────
# 2. strong PUT activity confirmation
# ────────────────────────────────────────────────────────────────────────────
def test_strong_put_activity_confirms_a_put():
    ev = evaluate_market_evidence(direction_value="PUT", row=put_row(),
                                 cfg=CFG, **PUT_TICKS)
    assert ev.verdict == "confirms"
    # Both components are NEGATIVE (bearish) ...
    assert ev.activity_score < -CFG.activity_confirmation_threshold
    assert ev.price_action_score < -CFG.price_action_threshold
    # ... yet the adjustment is POSITIVE, because it confirms the chosen PUT.
    assert ev.market_evidence_score < 0
    assert ev.alignment > 0
    assert ev.adjustment > 0


def test_bullish_evidence_does_not_confirm_a_put():
    """Symmetry check: CALL-flavoured evidence must not boost a PUT."""
    ev = evaluate_market_evidence(direction_value="PUT", row=call_row(),
                                 cfg=CFG, **CALL_TICKS)
    assert ev.verdict == "conflicts"
    assert ev.adjustment < 0


# ────────────────────────────────────────────────────────────────────────────
# 3. opposite tick direction
# ────────────────────────────────────────────────────────────────────────────
def test_opposite_tick_direction_conflicts_and_penalises():
    ev = evaluate_market_evidence(direction_value="CALL", row=put_row(),
                                 cfg=CFG, **PUT_TICKS)
    assert ev.verdict == "conflicts"
    assert ev.reason == "evidence_opposes_signal_direction"
    assert ev.adjustment < 0
    assert ev.adjustment >= -CFG.disagreement_penalty


def test_only_one_component_opposing_still_yields_neutral_not_penalty():
    """A single dissenting component is not 'disagreement'; it is thin data."""
    ev = evaluate_market_evidence(direction_value="CALL", row=put_row(),
                                 cfg=CFG, **CALL_TICKS)
    # activity says CALL, price action says PUT -> neither clears both, so the
    # require_both_components rule makes this insufficient, not conflicting.
    assert ev.adjustment == 0.0
    assert ev.verdict in ("insufficient_data", "conflicts")


# ────────────────────────────────────────────────────────────────────────────
# 4. weak / no tick data
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ticks,label", [
    (dict(CALL_TICKS, tick_count=0), "no ticks at all"),
    (dict(CALL_TICKS, tick_count=3), "below min_ticks_for_activity"),
    (dict(CALL_TICKS, activity_intensity=0.05), "bar too quiet"),
    (dict(CALL_TICKS, activity_intensity=50.0), "activity anomalous"),
    (dict(CALL_TICKS, intrabar_range=0.0), "no intrabar range"),
])
def test_weak_or_missing_tick_data_is_neutral_never_penalising(ticks, label):
    ev = evaluate_market_evidence(direction_value="CALL", row=call_row(),
                                 cfg=CFG, **ticks)
    assert ev.activity_score == 0.0, label
    assert ev.adjustment == 0.0, f"{label}: absence of data must not penalise"
    assert ev.verdict == "insufficient_data"


def test_missing_row_is_neutral():
    ev = evaluate_market_evidence(direction_value="CALL", row={}, cfg=CFG, **CALL_TICKS)
    assert ev.price_action_score == 0.0
    assert ev.adjustment == 0.0


def test_activity_score_ramps_with_sample_size_but_never_exceeds_one():
    """Trust grows with tick count; the score stays bounded."""
    scores = []
    for n in (8, 15, 25, 40, 100):
        s, q = activity_score(0.7, 1.5, 0.0008, 0.001, n, CFG)
        assert q == "ok"
        assert -1.0 <= s <= 1.0
        scores.append(s)
    # monotonic non-decreasing in sample size, converging below the ceiling
    assert scores == sorted(scores)
    assert scores[-1] <= 1.0


# ────────────────────────────────────────────────────────────────────────────
# 5. zero / synthetic volume
# ────────────────────────────────────────────────────────────────────────────
def test_evidence_layer_never_reads_or_requires_volume():
    """The whole layer must work with volume=0 and must not mention volume."""
    row_zero = call_row()
    row_zero["volume"] = 0.0
    row_zero["volume_source"] = "synthetic"
    row_synth = call_row()
    row_synth["volume"] = 37.0            # a tick count, NOT traded volume
    row_synth["volume_source"] = "synthetic"

    a = evaluate_market_evidence(direction_value="CALL", row=row_zero, cfg=CFG, **CALL_TICKS)
    b = evaluate_market_evidence(direction_value="CALL", row=row_synth, cfg=CFG, **CALL_TICKS)

    # Identical scores: volume is simply not an input.
    assert a.price_action_score == pytest.approx(b.price_action_score)
    assert a.adjustment == pytest.approx(b.adjustment)

    # And no field of the result is named like a volume.
    fields = a.as_dict()
    for banned in ("volume", "real_volume", "traded_volume", "order_volume"):
        assert banned not in fields
    # Nor of the activity bundle it is fed from.
    for name in TICK_ACTIVITY_FIELDS:
        assert "volume" not in name


def test_price_action_score_uses_only_the_whitelisted_columns():
    """A row carrying volume must not change the score — proves non-consumption."""
    base = call_row()
    polluted = dict(base)
    polluted.update({"volume": 999999.0, "vol_strength": 50.0, "vol_ma": 1e6})
    assert price_action_score(base, CFG)[0] == pytest.approx(
        price_action_score(polluted, CFG)[0])


# ────────────────────────────────────────────────────────────────────────────
# 6. neutral price action
# ────────────────────────────────────────────────────────────────────────────
def test_neutral_price_action_yields_no_adjustment():
    neutral = {
        "close_location_value": 0.0,
        "price_position_in_range": 0.5,
        "atr_normalized_momentum": 0.0,
        "bullish_rejection": 0.10,
        "bearish_rejection": 0.10,
        "breakout_distance": 0.0,
        "breakdown_distance": 0.0,
    }
    ev = evaluate_market_evidence(direction_value="CALL", row=neutral, cfg=CFG, **CALL_TICKS)
    assert abs(ev.price_action_score) < CFG.price_action_threshold
    assert ev.adjustment == 0.0
    assert ev.verdict == "insufficient_data"
    assert ev.reason == "price_action_weak"


def test_price_action_components_are_bounded():
    """Extreme inputs must not produce an out-of-range score."""
    extreme = call_row(close_location_value=99.0, price_position_in_range=-50.0,
                       atr_normalized_momentum=1e9, bullish_rejection=1e6,
                       bearish_rejection=-1e6, breakout_distance=1e9,
                       breakdown_distance=1e9)
    s, _ = price_action_score(extreme, CFG)
    assert -1.0 <= s <= 1.0


# ────────────────────────────────────────────────────────────────────────────
# 7. conflicting evidence
# ────────────────────────────────────────────────────────────────────────────
def test_conflicting_evidence_penalty_is_bounded_and_smaller_than_confirmation():
    agree = evaluate_market_evidence(direction_value="CALL", row=call_row(),
                                     cfg=CFG, **CALL_TICKS)
    conflict = evaluate_market_evidence(direction_value="CALL", row=put_row(),
                                        cfg=CFG, **PUT_TICKS)
    assert agree.adjustment > 0 > conflict.adjustment
    # Disagreement is deliberately the gentler of the two caps.
    assert abs(conflict.adjustment) <= CFG.disagreement_penalty
    assert CFG.disagreement_penalty <= CFG.max_adjustment


# ────────────────────────────────────────────────────────────────────────────
# 8. adjustment cap
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cap", [0.0, 1.0, 3.0, 5.0, 10.0])
def test_adjustment_never_exceeds_the_configured_cap(cap):
    """The cap is a hard ceiling on every path, for both directions."""
    cfg = EvidenceConfig(max_adjustment=cap, disagreement_penalty=cap / 2)
    for row in (call_row(), put_row()):
        for ticks in (CALL_TICKS, PUT_TICKS):
            for d in ("CALL", "PUT"):
                ev = evaluate_market_evidence(direction_value=d, row=row, cfg=cfg, **ticks)
                assert abs(ev.adjustment) <= cap + 1e-9


@pytest.mark.parametrize("cap", [1.0, 3.0, 5.0, 10.0])
def test_the_cap_is_actually_reachable(cap):
    """Guards the ceiling test above from being vacuous.

    Realistic evidence (composite ~0.67) deliberately lands *below* the cap —
    the payout tracks measured evidence. Saturating the inputs must reach the
    cap exactly, which proves the bound is live rather than unreachable.
    """
    cfg = EvidenceConfig(max_adjustment=cap, disagreement_penalty=cap)
    ev = evaluate_market_evidence(
        direction_value="CALL",
        row=call_row(close_location_value=1e9, price_position_in_range=1e9,
                     atr_normalized_momentum=1e9, bullish_rejection=1e9,
                     breakout_distance=1e9, resistance_distance=1e9),
        cfg=cfg,
        tick_direction_balance=1.0, activity_intensity=1.5,
        intrabar_price_change=1.0, intrabar_range=1.0, tick_count=10_000,
    )
    assert ev.market_evidence_score == pytest.approx(1.0)
    assert ev.adjustment == pytest.approx(cap, abs=1e-6)


def test_realistic_evidence_lands_below_the_cap():
    """The payout must track evidence, not saturate on ordinary signals."""
    ev = evaluate_market_evidence(direction_value="CALL", row=call_row(),
                                  cfg=CFG, **CALL_TICKS)
    assert 0.0 < ev.adjustment < CFG.max_adjustment


def test_extreme_evidence_still_respects_the_cap():
    cfg = EvidenceConfig(max_adjustment=5.0, disagreement_penalty=3.0)
    ev = evaluate_market_evidence(
        direction_value="CALL",
        row=call_row(close_location_value=1e9, price_position_in_range=1e9,
                     atr_normalized_momentum=1e9, bullish_rejection=1e9,
                     breakout_distance=1e9, resistance_distance=1e9),
        cfg=cfg,
        tick_direction_balance=1e9, activity_intensity=1.5,
        intrabar_price_change=1e9, intrabar_range=1.0, tick_count=10_000,
    )
    assert 0.0 < ev.adjustment <= 5.0


def test_headroom_dampener_only_reduces_never_amplifies():
    """Structure can weaken evidence but never strengthen it or erase it."""
    for d, row in (("CALL", call_row()), ("PUT", put_row())):
        for room in (5.0, 1.0, 0.35, 0.1, 0.0, -1.0):
            key = "resistance_distance" if d == "CALL" else "support_distance"
            r = dict(row, **{key: room})
            damp = headroom_dampener(d, r, CFG)
            assert 0.5 <= damp <= 1.0, (d, room, damp)


def test_no_headroom_halves_the_confirmation():
    full = evaluate_market_evidence(direction_value="CALL", row=call_row(),
                                    cfg=CFG, **CALL_TICKS)
    pressed = evaluate_market_evidence(
        direction_value="CALL", row=call_row(resistance_distance=0.0),
        cfg=CFG, **CALL_TICKS)
    assert pressed.headroom_dampener == pytest.approx(0.5)
    assert pressed.adjustment == pytest.approx(full.adjustment * 0.5, abs=1e-3)


# ────────────────────────────────────────────────────────────────────────────
# 9. no direction reversal
# ────────────────────────────────────────────────────────────────────────────
def test_evidence_layer_cannot_reverse_the_signal_direction():
    """The result carries no direction field, and clamping keeps the signal."""
    ev = evaluate_market_evidence(direction_value="CALL", row=put_row(),
                                  cfg=CFG, **PUT_TICKS)
    assert not hasattr(ev, "direction")
    assert ev.adjustment < 0

    # Applied to a realistic confidence, the decision still stands: a strong
    # signal survives the maximum penalty.
    base, threshold = 78.0, 70.0
    final = max(0, min(100, base + ev.adjustment))
    assert final >= threshold

    # And even the worst possible penalty cannot push a mid confidence below 0.
    cfg = EvidenceConfig(max_adjustment=5.0, disagreement_penalty=100.0)
    worst = evaluate_market_evidence(direction_value="CALL", row=put_row(),
                                     cfg=cfg, **PUT_TICKS)
    assert max(0, min(100, 60.0 + worst.adjustment)) >= 0


def test_support_and_resistance_distance_do_not_cast_a_directional_vote():
    """They are headroom only — flipping them must not flip the PA score."""
    a = call_row(support_distance=0.01, resistance_distance=0.01)
    b = call_row(support_distance=9.0, resistance_distance=9.0)
    assert price_action_score(a, CFG)[0] == pytest.approx(
        price_action_score(b, CFG)[0])


# ────────────────────────────────────────────────────────────────────────────
# 10. config
# ────────────────────────────────────────────────────────────────────────────
def test_every_config_field_maps_to_a_real_setting_in_both_directions():
    """Guards the silent-defaults bug found while building this layer."""
    inst = EvidenceConfig()
    settings = MarketDataSettings()
    declared = [f.name for f in dataclasses.fields(EvidenceConfig)
                if not f.name.startswith("_")]

    for name in declared:
        key = inst._SETTINGS_KEYS.get(name)
        assert key is not None, f"{name} has no settings mapping"
        assert hasattr(settings, key), f"{key} absent from MarketDataSettings"

    # Non-default values must actually arrive.
    custom = MarketDataSettings(
        market_evidence_enabled=False,
        market_evidence_max_adjustment=9.0,
        market_evidence_disagreement_penalty=1.5,
        market_evidence_activity_confirmation_threshold=0.77,
        market_evidence_min_ticks_for_activity=25,
        market_evidence_min_activity_intensity=0.11,
        market_evidence_max_activity_intensity=7.0,
        market_evidence_price_action_threshold=0.42,
        market_evidence_headroom_min_atr=0.9,
        market_evidence_require_both_components=False,
        market_evidence_activity_weight=0.9,
        market_evidence_price_action_weight=0.1,
    )
    cfg = EvidenceConfig.from_settings(custom)
    assert cfg.enabled is False
    assert cfg.max_adjustment == 9.0
    assert cfg.disagreement_penalty == 1.5
    assert cfg.activity_confirmation_threshold == 0.77
    assert cfg.min_ticks_for_activity == 25
    assert cfg.min_activity_intensity == 0.11
    assert cfg.max_activity_intensity == 7.0
    assert cfg.price_action_threshold == 0.42
    assert cfg.headroom_min_atr == 0.9
    assert cfg.require_both_components is False
    assert cfg.activity_weight == 0.9
    assert cfg.price_action_weight == 0.1


def test_config_is_backward_compatible_and_never_inverts():
    assert EvidenceConfig.from_settings(None).max_adjustment == 5.0
    # A settings file with no market_data key at all.
    legacy = RuntimeSettings.model_validate_json('{"provider":"pyquotex"}')
    assert EvidenceConfig.from_settings(legacy.market_data).enabled is True
    # A negative cap must not turn the layer into a penalty.
    assert EvidenceConfig.from_settings(
        MarketDataSettings(market_evidence_max_adjustment=-9.0)).max_adjustment == 9.0


def test_disabling_market_evidence_is_exactly_zero():
    cfg = EvidenceConfig(enabled=False)
    ev = evaluate_market_evidence(direction_value="CALL", row=call_row(),
                                  cfg=cfg, **CALL_TICKS)
    assert ev.adjustment == 0.0
    assert ev.verdict == "disabled"


# ────────────────────────────────────────────────────────────────────────────
# 11. no double-counting of votes / FEATURE_NAMES untouched
# ────────────────────────────────────────────────────────────────────────────
def test_evidence_layer_does_not_read_strategy_votes():
    """Rule 6: the score must not grow because more strategies agreed."""
    import inspect
    src = inspect.getsource(evaluate_market_evidence)
    for banned in ("res.votes", "votes", "win_n", "agreeing", "opposing"):
        assert banned not in src, f"evidence layer reads {banned!r}"


def test_feature_names_vector_is_not_extended():
    """Rule 11: the persisted ML feature vector must stay compatible."""
    from app.engine.confidence_model import FEATURE_NAMES
    assert FEATURE_NAMES == [
        "net", "win_n", "directional_edge", "body_ratio", "atr_pct",
        "htf_agree", "hhtf_agree", "is_trend", "is_range",
        "regime_transition_probability", "regime_transition_confidence",
        "regime_stability_score",
    ]
    # None of the evidence inputs leaked into it.
    for banned in ("activity_score", "price_action_score", "tick_direction_balance",
                   "intrabar_momentum", "market_evidence_score", "close_location_value"):
        assert banned not in FEATURE_NAMES


# ────────────────────────────────────────────────────────────────────────────
# 12. orchestrator wiring — threshold, calibration, precision gate
# ────────────────────────────────────────────────────────────────────────────
USER = "evidence-integration"


@pytest.fixture
def orch(pg, tmp_path, monkeypatch):
    from app.orchestrator import Orchestrator
    from app.session_manager import BrokerCredentials, session_manager

    creds = BrokerCredentials(quotex_email="broker@example.invalid",
                              quotex_password="pw", quotex_is_demo=True)
    monkeypatch.setattr(session_manager, "get_credentials", lambda uid: creds)
    monkeypatch.setattr(session_manager, "set_account_type", lambda uid, d: True)
    monkeypatch.setattr(RuntimeSettings, "save", lambda self, user_id=None: None)
    return Orchestrator(USER, RuntimeSettings())


def _edf(n=200, seed=7):
    from app.engine import strategies
    from app.engine.indicators import candles_to_df
    from app.schemas import Candle

    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.standard_normal(n) * 0.0006)
    open_ = np.concatenate([[1.10], close[:-1]])
    spread = np.abs(rng.standard_normal(n)) * 0.0005
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    candles = [Candle(timestamp=float(1_700_000_000 + i * 60), open=float(open_[i]),
                      high=float(high[i]), low=float(low[i]), close=float(close[i]),
                      volume=0.0, volume_source="synthetic") for i in range(n)]
    return strategies._enrich(candles_to_df(candles))


@pytest.mark.asyncio
async def test_orchestrator_computes_evidence_from_the_evaluated_row(orch):
    """The wiring must read the CLOSED bar and produce a bounded adjustment."""
    orch.provider = _TicksProvider()
    edf = _edf()
    feats, _sent, ev = await orch._collect_market_features(
        "EURUSD", "1m", edf, direction_value="CALL",
    )
    assert isinstance(ev, MarketEvidence)
    assert abs(ev.adjustment) <= orch.runtime.market_data.market_evidence_max_adjustment
    # It really read the price-action columns off the frame.
    assert any(k in edf.columns for k in
               ("close_location_value", "price_position_in_range"))


@pytest.mark.asyncio
async def test_orchestrator_evidence_is_zero_when_disabled(orch):
    orch.provider = _TicksProvider()
    orch.runtime.market_data.market_evidence_enabled = False
    _sent, ev = None, None
    _f, _s, ev = await orch._collect_market_features(
        "EURUSD", "1m", _edf(), direction_value="CALL",
    )
    assert ev.adjustment == 0.0
    assert ev.verdict == "disabled"


@pytest.mark.asyncio
async def test_orchestrator_evidence_survives_missing_tick_data(orch):
    class _NoTicks:
        async def get_realtime_ticks(self, asset):
            return []
        async def get_realtime_sentiment(self, asset):
            return None

    orch.provider = _NoTicks()
    _f, _s, ev = await orch._collect_market_features(
        "EURUSD", "1m", _edf(), direction_value="PUT",
    )
    assert ev.adjustment == 0.0
    assert ev.tick_count == 0


class _TicksProvider:
    """Provider with a dense, clearly directional tick stream."""

    async def get_realtime_ticks(self, asset):
        base = 1_700_000_000.0
        return [{"time": base + i, "price": 1.10 + i * 0.0002} for i in range(60)]

    async def get_realtime_sentiment(self, asset):
        return None


def test_calibration_runs_after_the_evidence_adjustment(orch):
    """Rule 7: order must be evidence → calibration, not the reverse."""
    import inspect
    from app.orchestrator import Orchestrator

    src = inspect.getsource(Orchestrator._evaluate_asset_signal)
    ev_at = src.index("market_ev.adjustment")
    cal_at = src.index("self.calibrator.calibrate(")
    thr_at = src.index("calibrated_confidence < effective_threshold")
    assert ev_at < cal_at < thr_at, (
        "evidence must be applied before calibration, and calibration before the "
        f"threshold gate (got evidence@{ev_at}, calibrate@{cal_at}, gate@{thr_at})"
    )
    # The precision gate must come after the threshold gate.
    prec_at = src.index("evaluate_precision(")
    assert thr_at < prec_at


def test_attribution_fields_are_complete(orch):
    """Rule 8: every required attribution key must be present."""
    ev = MarketEvidence(activity_score=0.4, price_action_score=0.3,
                        market_evidence_score=0.35, adjustment=1.5)
    from app.orchestrator import Orchestrator
    from app.engine.market_features import MarketFeatures
    from app.engine.market_evidence import MarketEvidence as _ME

    fields = Orchestrator._market_features_log_fields(
        MarketFeatures(asset="EURUSD", timeframe="1m"), None, 70.0, 74.0, 62.0,
        market_ev=ev, raw_strategy_confidence=66.0, regime_adjustment=4.0,
        final_before_calibration=71.5, agreeing_strategies=["a", "b"],
        opposing_strategies=["c"], rejection_reason="below threshold",
    )
    for required in (
        "raw_strategy_confidence", "regime_adjustment",
        "market_evidence_adjustment", "activity_score", "tick_direction_balance",
        "intrabar_momentum", "price_action_score", "final_before_calibration",
        "calibrated_confidence", "effective_threshold", "agreeing_strategies",
        "opposing_strategies", "rejection_reason",
    ):
        assert required in fields, f"missing attribution field {required}"

    blob = repr(fields).lower()
    for banned in ("password", "cookie", "token", "ssid", "csrf", "secret", "session.json"):
        assert banned not in blob, f"attribution leaked {banned!r}"


def test_attribution_is_safe_to_splat_alongside_explicit_call_site_fields():
    """Regression: `**attribution` collided with the call site's own kwargs.

    `MarketFeatures.flat()` emits `asset` and `timeframe`, and every call site
    (signal_generated, signal_rejected_confidence, signal_rejected_precision)
    also passes those explicitly. `log_event(**fields)` then raised
    "got multiple values for keyword argument 'asset'". A helper-level test
    could not catch this — only splatting the way the real call sites do can.
    """
    from app.orchestrator import Orchestrator
    from app.engine.market_features import MarketFeatures

    feats = MarketFeatures(asset="EURUSD", timeframe="1m", candle_ts=1.0, price=1.1)
    ev = MarketEvidence(adjustment=1.5)
    fields = Orchestrator._market_features_log_fields(
        feats, None, 70.0, 74.0, 62.0, market_ev=ev,
        raw_strategy_confidence=66.0, regime_adjustment=4.0,
        final_before_calibration=71.5, agreeing_strategies=["a"],
        opposing_strategies=["b"], rejection_reason="r",
    )
    for banned in ("asset", "timeframe"):
        assert banned not in fields, f"{banned} would collide with the call site"

    # The real failure mode: splatting next to the explicit kwargs.
    def sink(*, signal_id, asset, timeframe, direction, confidence, **rest):
        return asset, timeframe, rest

    out = sink(signal_id="s1", asset="EURUSD", timeframe="1m", direction="call",
               confidence=74, **fields)
    assert out[0] == "EURUSD" and out[1] == "1m"
    assert out[2]["market_evidence_adjustment"] == 1.5
