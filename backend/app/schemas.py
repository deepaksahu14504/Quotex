"""Internal + API data models (provider-agnostic)."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


class Direction(str, Enum):
    CALL = "call"
    PUT = "put"


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"
    ERROR = "error"


class Candle(BaseModel):
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    # "real"       = genuine exchange volume (Binance/Bybit klines carry it).
    # "synthetic"  = tick-count proxy or 0 placeholder (Quotex/pyquotex path —
    #                broker does not publish real order-book volume; we count
    #                ticks per bucket so the UI still sees an activity number).
    # Indicators/strategies MUST treat "synthetic" volume as neutral (volume
    # gates return 1.0) — applying real-volume maths to tick counts is not
    # mathematically valid. Defaults to "real" so existing callers/tests that
    # don't set the flag keep their existing behaviour.
    volume_source: str = "real"


class AssetInfo(BaseModel):
    symbol: str
    name: str
    payout: float
    is_open: bool = True
    is_otc: bool = False


class TradePlan(BaseModel):
    """The concrete trade a signal resolves to: when to enter, when it expires,
    how much is staked, and what it pays. Every field is produced by the same
    code path the auto-executor runs (`_candle_phase`, `RiskManager.stake_for`,
    `signal_ttl_seconds`), so what the panel shows and what the bot does cannot
    drift apart.

    `stake` is a projection, not a promise: it is computed from the balance and
    martingale step AT SIGNAL TIME. If a trade resolves between now and entry,
    the executor recomputes and the real stake can differ -- `stake_is_estimate`
    says so rather than letting the number look more certain than it is."""
    direction: str
    asset: str
    entry_at: float                    # epoch: when the order fires
    # The ONE instant both auto-trade and a manual Enter Trade press are meant
    # to honour. Added because a manual press used to fire immediately even
    # when the card above it said "enter at 10:07:00" -- the button and the
    # displayed plan disagreed. Equal to entry_at; kept as its own field (with
    # entry_at as the frontend fallback) so the frontend never has to
    # re-derive a schedule locally -- there is exactly one schedule, owned
    # here, and both the countdown and the button read the same value.
    scheduled_entry_at: float
    entry_in_seconds: float            # convenience for a countdown
    expiry_at: float                   # epoch: when the option settles
    # Whether expiry_at is a pre-trade projection or a broker-confirmed value.
    #   "scheduled"      -- computed here as scheduled_entry_at + duration,
    #                       before any order has been placed. This is what
    #                       every TradePlan from build_trade_plan() carries,
    #                       since it always runs before execution.
    #   "broker_receipt" -- the option's actual settlement time as reported
    #                       by the broker after the order was accepted. Binary
    #                       options commonly start the clock on broker receipt
    #                       of the order, not on the instant we intended to
    #                       enter, so the two can differ by however long
    #                       placement took. Only ever set by code that has a
    #                       confirmed order to read a real timestamp from --
    #                       not this method.
    expiry_basis: str = "scheduled"
    duration_seconds: int
    entry_price_reference: float       # signal price -- the actual fill can differ
    stake: float
    stake_is_estimate: bool = True
    payout_pct: float
    potential_profit: float
    potential_loss: float
    waits_for_candle_open: bool = False
    candle_phase: Optional[str] = None  # early | mid | late
    valid_until: Optional[float] = None  # signal TTL -- past this, no entry
    will_auto_execute: bool = False
    blocked_reason: Optional[str] = None  # why auto-trade will NOT fire this


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = Field(default_factory=now_ts)
    asset: str
    direction: Direction
    confidence: float                    # 0..100, CALIBRATED (what's actually gated/shown)
    raw_confidence: Optional[int] = None  # pre-calibration confluence score, for transparency
    regime: Optional[str] = None       # trend | range | mixed — market condition when this fired
    timeframe: str
    duration: int
    payout: float
    price: float
    reasons: List[str] = Field(default_factory=list)
    strategy_votes: Dict[str, str] = Field(default_factory=dict)
    status: str = "new"                # new | approved | skipped | executed | expired
    expires_at: Optional[float] = None  # absolute epoch at which this signal is too stale to
                                         # enter on. Computed once at signal creation from the
                                         # SAME function the execution gate uses, so the entry
                                         # window the UI counts down is the real one -- see
                                         # orchestrator.signal_ttl_seconds().
    is_otc: bool = False               # broker-synthesized OTC pair vs real-market pair —
                                        # carried through for regime-affinity dampening (P3)
                                        # and calibration segmentation (P4)
    submitted_at: Optional[float] = None  # when this signal was actually handed to the
                                           # TradeEngine for execution (after any candle-
                                           # boundary wait / revalidation) — distinct from
                                           # created_at (when it was first generated).
    trade_plan: Optional["TradePlan"] = None  # exactly what auto-trade would do with this
                                           # signal -- entry time, expiry time, stake, and
                                           # whether it will actually fire. Computed at
                                           # signal finalization from the SAME functions the
                                           # executor uses, so a manual trader can mirror the
                                           # bot instead of guessing.
    decision_id: Optional[str] = None     # Decision Intelligence Engine (Phase 1): links to
                                           # the full SignalDecision record in DecisionStore
                                           # (supporting/rejecting evidence, base rate, regime
                                           # snapshot, confidence interval). None until the
                                           # decision engine is enabled; kept out of Signal
                                           # itself so every existing Signal consumer/persist
                                           # path stays unaffected.


class EvidenceItem(BaseModel):
    """One piece of evidence for or against a signal — a single strategy's
    vote, or a single gate's veto/warning, captured as structured data
    instead of being folded into a scalar score or a free-text log line.
    This is what makes "what evidence rejects it?" answerable after the
    fact, not just "what was the net score?" """
    source: str                        # strategy name, or gate name (e.g. "precision_gate",
                                        # "mtf_hhtf_veto", "signal_validation")
    direction: Optional[str] = None     # "call" | "put" | None (gates without a directional
                                        # opinion, e.g. a confluence-count veto)
    strength: float = 0.0               # this item's contribution weight/score, whatever
                                        # scale is natural to its source — not normalized
                                        # across sources, since comparing a strategy's vote
                                        # weight to a gate's veto isn't meaningful anyway
    kind: str                          # "supporting" | "rejecting"
    detail: str = ""                   # human-readable reason string (already existed as
                                        # free text in most sources; captured here verbatim)


class RegimeSnapshot(BaseModel):
    """Market regime context active when a decision was made — a frozen
    copy, not a live reference, so it stays accurate even after the live
    regime_tracker's state has moved on."""
    regime: str                        # trend | range | mixed
    adx: Optional[float] = None
    adx_percentile: Optional[float] = None   # this asset's own historical ADX percentile
                                              # rank, when adaptive mode is on
    volatility_ratio: Optional[float] = None


class SignalDecision(BaseModel):
    """Decision Intelligence Engine (Phase 1) record: every signal decision
    -- accepted OR rejected -- gets one of these, answering the questions
    a signal must answer before it's trusted with capital: why does this
    trade exist, what evidence supports/rejects it, what's the historical
    base rate, what regime is active, how reliable has this setup been
    recently, how uncertain is the estimate, and what was ultimately
    decided and why.

    Written to DecisionStore regardless of final_decision -- rejected
    signals are exactly as valuable to keep as accepted ones (see
    rejection_reconciliation.py, Phase 3), since without them there is no
    way to ever measure whether a gate is well-calibrated or needlessly
    conservative.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = Field(default_factory=now_ts)
    asset: str
    direction: Optional[Direction] = None   # None when no dominant direction ever emerged
    timeframe: str

    thesis: str = ""                        # short human-readable summary of why this
                                             # direction was favored (built from the
                                             # dominant reasons + regime + MTF context)

    supporting_evidence: List[EvidenceItem] = Field(default_factory=list)
    rejecting_evidence: List[EvidenceItem] = Field(default_factory=list)

    base_rate: Optional[float] = None       # calibrated win rate for this
                                             # (strategy-set, regime, confidence-bucket);
                                             # None until calibration has enough samples
    regime: Optional[RegimeSnapshot] = None
    recent_reliability: Optional[float] = None   # blended health-score-style reliability
                                                  # for the strategies that actually voted
    confidence_interval: Optional[List[float]] = None  # [lower, upper], Wilson bound,
                                                        # percent scale — surfaced, not just
                                                        # computed internally and discarded
    raw_confidence: Optional[int] = None
    calibrated_confidence: Optional[int] = None

    price: Optional[float] = None           # entry price at decision time -- needed to
                                             # resolve a rejected signal's hypothetical
                                             # outcome later (rejection_reconciliation.py)
    duration: Optional[int] = None          # seconds, same as Signal.duration
    payout: Optional[float] = None          # percent, same as Signal.payout
    raw_features: Optional[Dict[str, float]] = None   # confidence_model.FEATURE_NAMES
                                                       # values, name-keyed for readability --
                                                       # Phase 5's training-set join key
    shadow_confidence_model_prediction: Optional[float] = None  # Phase 5: the fitted
    # logistic model's win-probability estimate (0-100 scale, comparable to
    # calibrated_confidence), computed and logged ALONGSIDE the real
    # hand-tuned-formula confidence that actually got used -- never applied
    # to the trade itself until explicitly promoted out of shadow.

    regime_transition_state: Optional[str] = None          # Regime.value at decision time
    regime_transition_probability: Optional[float] = None   # 0-1
    regime_transition_confidence: Optional[float] = None     # 0-1 -- confidence in the regime label itself
    regime_stability_score: Optional[float] = None           # 0-1
    composite_confidence: Optional[float] = None  # blend of base_rate / regime confidence /
    # calibrated_confidence using TradingSettings.decision_historical_probability_weight /
    # decision_regime_weight / decision_calibration_weight -- informational only (Monitoring),
    # never used as a gate

    final_decision: str = "rejected"        # "accepted" | "rejected"
    rejection_reason: Optional[str] = None  # populated whenever final_decision == "rejected";
                                             # None for accepted signals
    parent_decision_id: Optional[str] = None  # links a later-stage decision (e.g. revalidation
                                               # right before execution) back to the original
                                               # scan-time SignalDecision.id -- DecisionStore is
                                               # append-only, so a signal's full lifecycle is
                                               # reconstructed by following this chain rather
                                               # than mutating one record in place.


class TradeFeatureSnapshot(BaseModel):
    """Module 1 (Loss Pattern Intelligence Engine): a feature snapshot
    captured at trade EXECUTION time (not close), using data already
    computed during signal generation/revalidation -- nothing here is
    recomputed. Captured identically for every trade regardless of
    eventual win/loss, so downstream analytics can weigh both outcomes,
    never losses alone.

    Every field is Optional with no required arguments, so a TradeRecord
    persisted before this existed deserializes with features=None --
    fully backward compatible, no migration needed.
    """
    weekday: Optional[int] = None            # 0=Monday .. 6=Sunday, UTC
    hour: Optional[int] = None               # 0-23, UTC
    session: Optional[str] = None            # "asian" | "london" | "overlap" | "new_york"
    is_otc: Optional[bool] = None
    adx_at_entry: Optional[float] = None
    atr_pct_at_entry: Optional[float] = None
    rsi_at_entry: Optional[float] = None
    volatility_percentile: Optional[float] = None   # this asset's own ATR% percentile rank (adaptive mode only, else None)
    volume_ratio_at_entry: Optional[float] = None    # vol_strength
    confluence_count: Optional[int] = None           # number of agreeing strategy votes
    quality_score: Optional[float] = None            # pre-calibration raw confidence (same value as TradeRecord.raw_confidence, kept here too for a self-contained snapshot)
    adaptive_state: Optional[Dict[str, float]] = None  # compact record of the adaptive threshold values actually in effect for this trade (only populated when adaptive mode was on)
    execution_latency_ms: Optional[float] = None


class TradeRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    signal_id: Optional[str] = None
    created_at: float = Field(default_factory=now_ts)
    asset: str
    direction: Direction
    amount: float
    duration: int
    is_demo: bool
    status: TradeStatus = TradeStatus.PENDING
    open_price: Optional[float] = None
    close_price: Optional[float] = None
    profit: float = 0.0
    payout: float = 0.0
    confidence: Optional[int] = None       # calibrated confidence used for the trade decision
    raw_confidence: Optional[int] = None   # pre-calibration confluence score
    regime: Optional[str] = None           # trend | range | mixed — market condition at entry
    strategies: List[str] = Field(default_factory=list)
    timeframe: Optional[str] = None
    broker_order_id: Optional[str] = None
    closed_at: Optional[float] = None
    decision_id: Optional[str] = None      # links back to the SignalDecision (Phase 1) that
                                            # produced this trade, if the decision engine was on
    note: Optional[str] = None
    features: Optional[TradeFeatureSnapshot] = None


class EngineState(BaseModel):
    provider: str
    connected: bool = False
    is_demo: bool = True
    mode: str = "manual"
    paused: bool = False
    pause_reason: Optional[str] = None
    balance: float = 0.0
    currency: str = "USD"
    starting_balance: float = 0.0
    daily_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    consecutive_losses: int = 0
    trades_today: int = 0
    active_trades: int = 0
    otp_required: bool = False
    otp_verifying: bool = False
    otp_message: Optional[str] = None
    last_update: float = Field(default_factory=now_ts)
    # Account mode -- "live" | "demo" | "tournament". Distinct from `mode`
    # above, which is the trading mode (auto/manual/off). Defaults to
    # "demo" to match the existing is_demo=True default -- an existing
    # deployment reading this field for the first time gets the account
    # mode its is_demo flag already implied, not a surprise value.
    account_mode: str = "demo"
    tournament_id: Optional[int] = None
    tournament_supported: bool = False  # whether this broker connection exposes tournament switching at all -- UI hides Tournament mode entirely when False
    # Pipeline lifecycle stage -- distinct from `connected` (a point-in-time
    # broker flag) and `paused` (the manual kill-switch). One of: stopped,
    # connecting, authenticating, restoring, running, reconnecting, stuck.
    # "stuck" means a background worker crash-looped past its auto-restart
    # budget and is waiting on a manual restart (see /api/pipeline/restart).
    stage: str = "stopped"

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return round((self.wins / total) * 100, 1) if total else 0.0
