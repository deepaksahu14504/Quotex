"""Market Evidence Score — bounded, independent confirmation for a signal.

What this module is
-------------------
A *scoring* layer over evidence that already exists. It reads:

  * tick-activity measures (`TickActivity`, keyed `(asset, timeframe)`)
  * OHLC price-action columns produced by `market_features.py`

and turns them into one bounded number that can nudge confidence by a few
points. Nothing here fetches data, votes on direction, or touches a gate.

What this module is NOT
-----------------------
*  **Not a volume model.** PyQuotex publishes no order-book volume
   (`vendor/old-pyquotex/pyquotex/api.py:790` — the tick frame is
   `[asset, ts, price, direction]`). Tick counts are *activity*, and no field
   here is named or treated as volume.
*  **Not a direction selector.** The strategy engine has already chosen
   `res.direction` before this runs. This module returns a signed number; it
   cannot pick or reverse a direction.
*  **Not a vote counter.** It never reads how many strategies agreed. Vote
   correlation is already handled by `strategy_correlation.py` upstream, and
   re-counting votes here would double-pay for the same evidence.
*  **Not a threshold bypass.** The adjustment lands *before* calibration, so
   calibration, the effective-threshold gate, the precision gate and every risk
   control still see the result and still apply unchanged.

Why these particular features
-----------------------------
Chosen for independence from the existing strategy votes (see
`INSPECTION_MARKET_EVIDENCE.md` §3). `support_distance` / `resistance_distance`
deliberately do **not** cast a directional vote — two strategies already test
whether price is *at* a level — so they act only as a "room to run" dampener.
`bullish_rejection` / `bearish_rejection` are down-weighted because
`price_strength` already carries wick structure into three votes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# Neutral values. "No evidence" must never read as "bullish" or "bearish".
NEUTRAL_SCORE = 0.0


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce to a finite float; anything else becomes `default`."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _tanh(x: float) -> float:
    try:
        return math.tanh(x)
    except (OverflowError, ValueError):
        return 0.0


# ────────────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class EvidenceConfig:
    """Tunable bounds for the evidence layer.

    Every default is deliberately conservative: a small cap, a real minimum
    sample size, and thresholds that require genuine agreement before any
    confidence is added. Raising signal frequency is not a goal here.
    """

    enabled: bool = True
    max_adjustment: float = 5.0          # hard cap in confidence points (agreement)
    disagreement_penalty: float = 3.0    # hard cap in confidence points (conflict)

    # Activity
    activity_confirmation_threshold: float = 0.25   # |activity_score| needed to confirm
    min_ticks_for_activity: int = 8                 # below this, activity is neutral
    min_activity_intensity: float = 0.40            # below this, the bar is too quiet to judge
    max_activity_intensity: float = 4.00            # above this, treat as anomalous/illiquid

    # Price action
    price_action_threshold: float = 0.20            # |price_action_score| needed to confirm
    headroom_min_atr: float = 0.35                  # less room than this (in ATR) dampens

    # Combination
    require_both_components: bool = True            # both must agree to confirm
    activity_weight: float = 0.45
    price_action_weight: float = 0.55

    # dataclass field -> RuntimeSettings.market_data attribute.
    #
    # This mapping is load-bearing and is asserted by a test. An earlier
    # version prefixed only `enabled` and read the rest unprefixed, so eleven
    # settings silently fell back to the dataclass defaults. That was invisible
    # in a smoke test because the defaults happen to match -- it only showed up
    # when a non-default value was actually set.
    _SETTINGS_KEYS: Dict[str, str] = field(default_factory=lambda: {
        "enabled": "market_evidence_enabled",
        "max_adjustment": "market_evidence_max_adjustment",
        "disagreement_penalty": "market_evidence_disagreement_penalty",
        "activity_confirmation_threshold": "market_evidence_activity_confirmation_threshold",
        "min_ticks_for_activity": "market_evidence_min_ticks_for_activity",
        "min_activity_intensity": "market_evidence_min_activity_intensity",
        "max_activity_intensity": "market_evidence_max_activity_intensity",
        "price_action_threshold": "market_evidence_price_action_threshold",
        "headroom_min_atr": "market_evidence_headroom_min_atr",
        "require_both_components": "market_evidence_require_both_components",
        "activity_weight": "market_evidence_activity_weight",
        "price_action_weight": "market_evidence_price_action_weight",
    }, repr=False, compare=False)

    @classmethod
    def from_settings(cls, market_data: Optional[Any]) -> "EvidenceConfig":
        """Build from `RuntimeSettings.market_data`, tolerating absent fields.

        Reads via `getattr` with the dataclass default as fallback so an older
        `runtime_settings.json` (or a partially populated settings object)
        still yields a valid, safe configuration.
        """
        md = market_data
        cfg = cls()
        if md is None:
            return cfg
        for name, key in cfg._SETTINGS_KEYS.items():
            val = getattr(md, key, None)
            if val is None:
                continue
            try:
                cur = getattr(cfg, name)
                setattr(cfg, name, bool(val) if isinstance(cur, bool) else type(cur)(val))
            except (TypeError, ValueError):
                continue
        # A negative or zero cap would make the layer a no-op or invert it.
        cfg.max_adjustment = abs(cfg.max_adjustment)
        cfg.disagreement_penalty = abs(cfg.disagreement_penalty)
        return cfg


# ────────────────────────────────────────────────────────────────────────────
# Result
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class MarketEvidence:
    """Full attribution for one evaluation.

    Every field is JSON-safe so it can go straight into a structured log line
    or the UI. No credentials, cookies, session paths or tokens appear here.
    """

    # component scores, each in [-1, +1]; +1 = strong CALL evidence
    activity_score: float = NEUTRAL_SCORE
    price_action_score: float = NEUTRAL_SCORE
    # combined, in [-1, +1]
    market_evidence_score: float = NEUTRAL_SCORE

    # the raw inputs, echoed for attribution
    tick_direction_balance: float = 0.0
    intrabar_momentum: float = 0.0
    intrabar_price_change: float = 0.0
    intrabar_range: float = 0.0
    activity_intensity: float = 1.0
    tick_count: int = 0

    # how the score was reached
    alignment: float = NEUTRAL_SCORE     # score expressed in the signal's direction
    verdict: str = "neutral"             # confirms | conflicts | neutral | insufficient_data | disabled
    reason: str = "disabled"
    adjustment: float = 0.0              # final, signed, capped, confidence points
    headroom_dampener: float = 1.0       # 1.0 = no dampening
    data_quality: str = "ok"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "activity_score": round(self.activity_score, 4),
            "price_action_score": round(self.price_action_score, 4),
            "market_evidence_score": round(self.market_evidence_score, 4),
            "tick_direction_balance": round(self.tick_direction_balance, 4),
            "intrabar_momentum": round(self.intrabar_momentum, 6),
            "intrabar_price_change": round(self.intrabar_price_change, 6),
            "intrabar_range": round(self.intrabar_range, 6),
            "activity_intensity": round(self.activity_intensity, 4),
            "tick_count": int(self.tick_count),
            "market_evidence_alignment": round(self.alignment, 4),
            "market_evidence_verdict": self.verdict,
            "market_evidence_reason": self.reason,
            "market_evidence_adjustment": round(self.adjustment, 4),
            "market_evidence_headroom": round(self.headroom_dampener, 4),
            "market_evidence_quality": self.data_quality,
        }


# ────────────────────────────────────────────────────────────────────────────
# Activity score
# ────────────────────────────────────────────────────────────────────────────
def activity_score(
    tick_direction_balance: float,
    activity_intensity: float,
    intrabar_price_change: float,
    intrabar_range: float,
    tick_count: int,
    cfg: EvidenceConfig,
) -> tuple[float, str]:
    """Signed activity evidence in [-1, +1]; positive favours CALL.

    Built from two *independent* microstructure measures:

      * `tick_direction_balance` — (up_moves - down_moves) / total_moves. This
        is the share of price updates that pushed each way, so it is already
        scale-free and bounded.
      * net displacement — `intrabar_price_change / intrabar_range`, i.e. how
        much of the bar's total range was travelled in one direction rather
        than round-tripped. Also naturally bounded to [-1, 1].

    `activity_intensity` is NOT directional. It gates how much the two measures
    above may be trusted: a bar with very few ticks has a direction balance that
    is mostly noise, and an absurdly intense bar is more likely a data artefact
    than a tradeable move.

    Tick count is never treated as volume anywhere in this function.
    """
    if tick_count < int(cfg.min_ticks_for_activity):
        return NEUTRAL_SCORE, f"insufficient_ticks({tick_count}<{int(cfg.min_ticks_for_activity)})"

    intensity = _f(activity_intensity, 1.0)
    if intensity < float(cfg.min_activity_intensity):
        return NEUTRAL_SCORE, f"bar_too_quiet({intensity:.2f}<{cfg.min_activity_intensity:.2f})"
    if intensity > float(cfg.max_activity_intensity):
        return NEUTRAL_SCORE, f"activity_anomalous({intensity:.2f}>{cfg.max_activity_intensity:.2f})"

    balance = _clip(_f(tick_direction_balance, 0.0))

    rng = _f(intrabar_range, 0.0)
    if rng > 0:
        displacement = _clip(_f(intrabar_price_change, 0.0) / rng)
    else:
        # A bar with no range at all carries no directional information.
        return NEUTRAL_SCORE, "no_intrabar_range"

    score = _clip(0.60 * balance + 0.40 * displacement)

    # Trust ramps in with sample size: 8 ticks is a weak basis, 40 is plenty.
    # This is a confidence multiplier, never a direction.
    n = max(0, int(tick_count))
    ramp = _clip((n - cfg.min_ticks_for_activity) / max(1.0, 40.0 - cfg.min_ticks_for_activity), 0.0, 1.0)
    trust = 0.5 + 0.5 * ramp
    return score * trust, "ok"


# ────────────────────────────────────────────────────────────────────────────
# Price-action score
# ────────────────────────────────────────────────────────────────────────────
# Weights sum to 1.0. Rejection is deliberately the smallest term because
# `price_strength` (read by three strategies) already encodes wick structure.
_PA_WEIGHTS = {
    "close_location_value": 0.30,
    "range_position": 0.20,
    "atr_momentum": 0.30,
    "rejection": 0.10,
    "breakout": 0.10,
}


def price_action_score(row: Mapping[str, Any], cfg: EvidenceConfig) -> tuple[float, str]:
    """Signed OHLC price-action evidence in [-1, +1]; positive favours CALL.

    Uses only features no existing strategy vote reads directly (see
    `INSPECTION_MARKET_EVIDENCE.md` §3). `support_distance` /
    `resistance_distance` are handled separately by `headroom_dampener` and are
    intentionally absent here.
    """
    if not row:
        return NEUTRAL_SCORE, "no_row"

    clv = row.get("close_location_value")
    pos = row.get("price_position_in_range")
    mom = row.get("atr_normalized_momentum")
    bull_rej = row.get("bullish_rejection")
    bear_rej = row.get("bearish_rejection")
    brk = row.get("breakout_distance")
    brkdn = row.get("breakdown_distance")

    present = [v for v in (clv, pos, mom) if v is not None]
    if not present:
        return NEUTRAL_SCORE, "no_price_action_features"

    # Close Location Value: -1 closed at the low, +1 at the high.
    s_clv = _clip(_f(clv, 0.0))

    # Position in the recent rolling range, mapped 0..1 -> -1..+1.
    s_pos = _clip(2.0 * _f(pos, 0.5) - 1.0)

    # ATR-normalised momentum is unbounded; squash it so one large bar cannot
    # dominate the composite.
    s_mom = _tanh(_f(mom, 0.0))

    # Wick rejection asymmetry. Down-weighted (see module docstring).
    s_rej = _clip(_f(bull_rej, 0.0) - _f(bear_rej, 0.0))

    # Breakout/breakdown of the prior rolling extreme, ATR-normalised. A bar is
    # normally one or the other, not both; subtract so a genuine breakout reads
    # positive and a breakdown negative.
    s_brk = _tanh(_f(brk, 0.0)) - _tanh(_f(brkdn, 0.0))

    total = (
        _PA_WEIGHTS["close_location_value"] * s_clv
        + _PA_WEIGHTS["range_position"] * s_pos
        + _PA_WEIGHTS["atr_momentum"] * s_mom
        + _PA_WEIGHTS["rejection"] * s_rej
        + _PA_WEIGHTS["breakout"] * s_brk
    )
    return _clip(total), "ok"


def headroom_dampener(
    direction_value: Optional[str],
    row: Mapping[str, Any],
    cfg: EvidenceConfig,
) -> float:
    """Scale in (0, 1] reducing evidence when the direction has no room to run.

    This is the *only* use of `support_distance` / `resistance_distance`, and it
    is deliberately not directional. Two strategies already vote on whether
    price is at a level; asking the same question again would double-count. The
    question here is different: if price is pressed against the level it would
    have to move through, a confirming signal is worth less.

    Never amplifies (returns at most 1.0) and never reaches 0, so evidence can
    be weakened by structure but not erased by it.
    """
    if not row or direction_value is None:
        return 1.0
    is_call = str(direction_value).upper() in ("CALL", "BUY", "UP", "HIGHER", "1")
    key = "resistance_distance" if is_call else "support_distance"
    raw = row.get(key)
    if raw is None:
        return 1.0
    room = _f(raw, float(cfg.headroom_min_atr))
    floor = float(cfg.headroom_min_atr)
    if floor <= 0:
        return 1.0
    if room >= floor:
        return 1.0
    if room <= 0:
        # Pressed against or through the level: halve the evidence, no more.
        return 0.5
    # Linear ramp from 0.5 (no room) to 1.0 (full room).
    return 0.5 + 0.5 * (room / floor)


# ────────────────────────────────────────────────────────────────────────────
# Combination
# ────────────────────────────────────────────────────────────────────────────
def evaluate_market_evidence(
    *,
    direction_value: Optional[str],
    row: Optional[Mapping[str, Any]],
    tick_direction_balance: float = 0.0,
    activity_intensity: float = 1.0,
    intrabar_momentum: float = 0.0,
    intrabar_price_change: float = 0.0,
    intrabar_range: float = 0.0,
    tick_count: int = 0,
    cfg: Optional[EvidenceConfig] = None,
) -> MarketEvidence:
    """Compute the bounded evidence adjustment for one already-chosen direction.

    Contract, all of it enforced here and covered by tests:

      *  CALL is confirmed only when activity AND price action both point CALL.
      *  PUT  is confirmed only when both point PUT.
      *  Weak, missing or low-quality data yields **neutral (0.0)** — it never
         penalises, because absence of evidence is not evidence against.
      *  Genuine disagreement applies a modest penalty capped by
         `disagreement_penalty`, and it can never flip the direction: this
         function returns a number, and the caller still clamps to [0, 100].
      *  `|adjustment| <= max_adjustment` on every path.
    """
    cfg = cfg or EvidenceConfig()
    ev = MarketEvidence(
        tick_direction_balance=_f(tick_direction_balance, 0.0),
        intrabar_momentum=_f(intrabar_momentum, 0.0),
        intrabar_price_change=_f(intrabar_price_change, 0.0),
        intrabar_range=_f(intrabar_range, 0.0),
        activity_intensity=_f(activity_intensity, 1.0),
        tick_count=int(tick_count or 0),
    )

    if not cfg.enabled:
        ev.verdict, ev.reason, ev.data_quality = "disabled", "disabled", "disabled"
        return ev
    if direction_value is None:
        ev.verdict, ev.reason, ev.data_quality = "neutral", "no_direction", "no_direction"
        return ev

    act, act_quality = activity_score(
        ev.tick_direction_balance, ev.activity_intensity,
        ev.intrabar_price_change, ev.intrabar_range, ev.tick_count, cfg,
    )
    pa, pa_quality = price_action_score(row or {}, cfg)
    ev.activity_score = act
    ev.price_action_score = pa
    ev.data_quality = "ok" if (act_quality == "ok" and pa_quality == "ok") else f"{act_quality}|{pa_quality}"

    act_th = float(cfg.activity_confirmation_threshold)
    pa_th = float(cfg.price_action_threshold)
    act_ok = abs(act) >= act_th
    pa_ok = abs(pa) >= pa_th

    if not act_ok and not pa_ok:
        ev.verdict, ev.reason = "insufficient_data", "both_components_weak"
        return ev

    # Weighted composite. Weighting by whether each component cleared its own
    # threshold means a single loud component cannot carry the verdict alone.
    if cfg.require_both_components:
        if not (act_ok and pa_ok):
            weak = "activity" if not act_ok else "price_action"
            ev.verdict, ev.reason = "insufficient_data", f"{weak}_weak"
            return ev
        w_act, w_pa = cfg.activity_weight, cfg.price_action_weight
    else:
        w_act = cfg.activity_weight if act_ok else 0.0
        w_pa = cfg.price_action_weight if pa_ok else 0.0
    denom = (w_act + w_pa) or 1.0
    composite = _clip((w_act * act + w_pa * pa) / denom)
    ev.market_evidence_score = composite

    # Express the composite in the direction the strategy already chose.
    is_call = str(direction_value).upper() in ("CALL", "BUY", "UP", "HIGHER", "1")
    sign = 1.0 if is_call else -1.0
    ev.alignment = _clip(composite * sign)
    ev.headroom_dampener = headroom_dampener(direction_value, row or {}, cfg)

    if ev.alignment > 0:
        ev.verdict = "confirms"
        ev.reason = "activity_and_price_action_agree"
        # Scale linearly from the weaker of the two thresholds up to full
        # agreement, so the payout tracks measured evidence rather than being a
        # flat bonus. Then apply the structural headroom dampener, then cap.
        th = min(act_th, pa_th) or 1e-9
        scale = _clip((abs(composite) - th) / max(1e-9, 1.0 - th), 0.0, 1.0)
        ev.adjustment = round(float(cfg.max_adjustment) * scale * ev.headroom_dampener, 4)
    elif ev.alignment < 0:
        ev.verdict = "conflicts"
        ev.reason = "evidence_opposes_signal_direction"
        th = min(act_th, pa_th) or 1e-9
        scale = _clip((abs(composite) - th) / max(1e-9, 1.0 - th), 0.0, 1.0)
        ev.adjustment = round(-float(cfg.disagreement_penalty) * scale, 4)
    else:
        ev.verdict, ev.reason = "neutral", "no_alignment"

    # Final clamp — belt and braces, so no combination of weights, dampener or
    # config can exceed the configured cap.
    cap = float(cfg.max_adjustment)
    pen = float(cfg.disagreement_penalty)
    ev.adjustment = max(-pen, min(cap, ev.adjustment))
    return ev
