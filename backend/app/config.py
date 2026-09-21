"""Application configuration and runtime-mutable settings.

Two layers:
- `Settings` (env-driven, immutable at runtime): secrets, provider choice, server.
- `RuntimeSettings` (JSON-persisted, mutated from the UI): trading + risk + UX prefs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_DIR = DATA_DIR / "users"
SETTINGS_FILE = DATA_DIR / "runtime_settings.json"  # legacy single-user path (pre-multi-user)
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _restrict_env_file_permissions() -> None:
    """High-severity fix: .env holds JWT_SECRET, SESSION_ENCRYPTION_KEY, and
    broker credentials, but was previously written with the OS's default
    permissions (typically 644 = world-readable). On a shared VPS, any other
    local user could read these secrets straight off disk. Owner-only
    (600) from here on -- called both right after every write and once at
    import time, so an already-deployed .env from before this fix gets
    locked down automatically too, without needing its contents touched."""
    try:
        if ENV_FILE.exists():
            os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass  # e.g. read-only filesystem in some container setups -- don't crash startup over this


_restrict_env_file_permissions()


def user_data_dir(user_id: str) -> Path:
    """Per-user scoped data directory: settings, trade history, pyquotex
    session cache. Isolates one user's data from another's on disk."""
    d = USERS_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_env_file() -> dict:
    """Parse the .env file into a {KEY: value} dict (ignores comments/blanks)."""
    data: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def update_env_file(updates: dict) -> None:
    """Update/append keys in .env, preserving existing lines and comments."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    done: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                done.add(key)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in done:
            out.append(f"{k}={v}")
    content = "\n".join(out) + "\n"
    # Create/truncate with 0o600 from the very first byte written, rather
    # than write_text() (which would briefly leave the file at the OS
    # default permissions before a subsequent chmod call could run).
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    _restrict_env_file_permissions()  # belt-and-suspenders in case the file already existed with looser perms


class Settings(BaseSettings):
    """Environment / boot configuration."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Database (PostgreSQL) ----
    # Standard SQLAlchemy URL, e.g.
    #   postgresql+psycopg://qat_app:password@localhost:5432/qat
    # Defaults to a local dev database so `uvicorn app.main:app` keeps working
    # out of the box for anyone who already has Postgres running locally;
    # production deployments MUST set this explicitly (see .env.example).
    database_url: str = "postgresql+psycopg://qat_app:qat_app@localhost:5432/qat"
    db_pool_size: int = 30
    db_max_overflow: int = 60
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_statement_timeout_ms: int = 30_000  # server-side guard against a runaway query holding a pooled connection

    market_provider: Literal["paper", "pyquotex"] = "paper"

    quotex_email: Optional[str] = None
    quotex_password: Optional[str] = None
    quotex_is_demo: bool = True
    quotex_ssid: Optional[str] = None        # paste browser session token to skip login/OTP
    quotex_user_agent: Optional[str] = None

    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


class RiskSettings(BaseModel):
    stake_mode: Literal["fixed", "percent"] = "fixed"
    stake_amount: float = 5.0           # used when stake_mode == fixed
    stake_percent: float = 2.0          # % of balance when stake_mode == percent
    min_payout: float = 75.0            # only trade assets paying at least this %
    confidence_threshold: int = 70      # 0-100 confluence score gate
    payout_aware_threshold: bool = True  # also require confidence to clear the payout's breakeven win-rate + margin
    payout_edge_margin: int = 8          # points required above raw breakeven (a genuine edge, not just break-even)
    latency_aware_threshold: bool = True   # widen the confidence bar when the pipeline itself is running slow (P5)
    latency_p95_ceiling_ms: float = 1500.0  # eval_start->signal_generated p95 above this = "degraded"
    latency_threshold_widen: int = 5        # points added to effective_threshold while degraded
    daily_loss_limit: float = 50.0      # stop for the day after losing this much
    max_consecutive_losses: int = 3     # auto-pause after N losses in a row
    max_trades_per_day: int = 30
    max_concurrent_trades: int = 3      # how many positions can be open at once

    # How many simultaneous trades on ONE asset. Was hardcoded to 2 (1 CALL +
    # 1 PUT) with no way to change it.
    max_trades_per_asset: int = 2
    # Lift the same-signal in-flight guard, so the same signal can be
    # submitted again while its first order is still being placed.
    #
    # Default OFF, and worth understanding before switching on. This guard is
    # not about preferring fewer trades -- it exists because while an order is
    # in flight nobody, including the broker client, knows whether it landed.
    # Submitting again in that window is not "two trades I chose", it is "one
    # or two trades, I'll find out later". Multiple positions on one asset are
    # already available through max_trades_per_asset, which does that with a
    # known outcome each time. Turn this on only if you specifically want the
    # unknown-count behaviour.
    allow_duplicate_orders: bool = False
    # When an order does not return in time, its outcome is unknown. By
    # default the engine stops placing new orders and pauses trading until
    # that one is reconciled against the broker.
    #
    # Set False to keep trading straight through it. The unknown order is
    # still logged and still shown in System Status -- nothing is hidden --
    # but it no longer holds anything up. This is the "manual trading like
    # before" switch: Enter Trade goes to the broker every time and you
    # reconcile positions yourself.
    block_trading_on_unknown_order: bool = True
    cooldown_seconds: int = 60          # wait after a loss before next entry
    stale_data_pause_seconds: int = 90  # if no candle received in this long (while otherwise "connected"), auto-pause new signals until fresh data resumes
    precision_asset_loss_streak_limit: int = 2      # consecutive losses on ONE asset before it's sidelined
    precision_asset_cooldown_seconds: int = 1800    # how long that asset sits out (30 min default)
    martingale_enabled: bool = False
    martingale_multiplier: float = 2.0
    martingale_max_steps: int = 2
    # HARD SAFETY CAP: no single trade's stake may exceed this fraction of the
    # CURRENT balance, no matter what stake_mode, confidence-based sizing, or
    # martingale compute.
    #
    # This did not exist before. stake_for() computed martingale's
    # (multiplier ** step) purely against the fixed stake_amount, with no
    # awareness of the account balance at all -- so a losing streak on a
    # small account could and did produce a stake far larger than the whole
    # account (observed: $430.80 on an account that had recently shown a
    # balance in the $3-13 range). Whether or not that particular order
    # reached the broker, a stake sized like that against a balance like that
    # is the definition of an account-wipe risk and must never be sent,
    # independent of any other bug.
    max_stake_pct_of_balance: float = 25.0
    confidence_based_sizing: bool = False  # N1: scale stake by signal confidence instead of a flat amount — opt-in
    confidence_size_floor: float = 0.6     # stake multiplier at borderline (threshold-level) confidence
    confidence_size_ceiling: float = 1.4   # stake multiplier at 100% confidence
    # Drawdown circuit breaker — pause when equity falls from peak
    drawdown_breaker_enabled: bool = True
    max_drawdown_pct: float = 15.0      # pause if balance drops this % from session peak
    max_drawdown_amount: float = 0.0    # optional fixed $ drawdown cap (0 = disabled)
    # Correlation guard — block correlated same-direction exposure
    correlation_guard_enabled: bool = True
    correlation_threshold: float = 0.85


class ValidationSettings(BaseModel):
    enabled: bool = True
    daily_run_hour_utc: int = 3         # 03:00 UTC daily auto-run (-1 to disable schedule)
    history_bars: int = 2000
    walk_forward_folds: int = 5
    max_concurrency: int = 2
    max_assets_per_run: int = 12
    starting_balance: float = 1000.0
    min_trades_for_score: int = 8
    active_score_min: int = 80
    trial_score_min: int = 55
    sync_strategy_manager: bool = True


class UXSettings(BaseModel):
    sound_enabled: bool = True
    sound_volume: float = 0.6
    notifications_enabled: bool = True
    fog_intensity: float = 0.6          # 0..1 visual fog strength
    reduced_motion: bool = False
    accent: str = "cyan"


class TelegramSettings(BaseModel):
    enabled: bool = False
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    send_signals: bool = True
    send_results: bool = True
    allow_commands: bool = True         # /enter /exit /stop /status from Telegram


class TradingSettings(BaseModel):
    mode: Literal["auto", "manual", "off"] = "manual"
    is_demo: bool = True                # demo-locked by default for safety -- kept for backward compatibility;
                                         # derived from account_mode below wherever both are set (account_mode wins)
    # --- Account Mode (Live / Demo / Tournament) ---
    # "tournament" is layered on top of demo mode at the broker protocol
    # level -- pyquotex has no separate tournament account type.
    # change_account(account_type, tournament_id) always uses REAL or
    # PRACTICE, with a nonzero tournament_id activating tournament
    # routing on top of PRACTICE. is_demo is kept in sync automatically
    # (tournament and demo both set is_demo=True) so every existing
    # consumer of is_demo elsewhere in this codebase keeps working
    # unchanged -- this is purely additive.
    account_mode: Literal["live", "demo", "tournament"] = "demo"
    tournament_id: Optional[int] = None  # manually-entered tournament ID -- pyquotex has no discovery API (see audit), so this is user-provided, matching how pyquotex's own examples/tests use it
    timeframe: str = "1m"
    duration_seconds: int = 60
    volatility_adjusted_duration: bool = False  # N2: scale expiry by current ATR% instead of a fixed duration — opt-in
    duration_low_vol_atr_pct: float = 0.08   # below this ATR%, market is calm -> widen duration
    duration_high_vol_atr_pct: float = 0.5   # above this ATR%, market is choppy/fast -> shorten duration
    duration_low_vol_mult: float = 1.3       # duration multiplier applied when calm
    duration_high_vol_mult: float = 0.7      # duration multiplier applied when volatile
    duration_min_seconds: int = 30           # never shrink below this regardless of volatility
    asset_whitelist: List[str] = Field(default_factory=list)  # empty = all open assets
    # --- Data sufficiency (accuracy) ------------------------------------- #
    # 55 bars is the point where indicators produce a NUMBER, not the point
    # where that number is trustworthy. Wilder-smoothed series (ADX, ATR, RSI)
    # and long EMAs are still dominated by their seed values at 55 bars: an
    # EMA(50) needs roughly 3x its period before the seed's weight decays
    # below ~5%, and ADX(14) stacks two smoothing passes on top of each other.
    # Evaluating a signal at 55 bars means grading a setup on indicators that
    # are still converging -- the output looks precise and isn't.
    #
    # 200 is chosen as ~4x the longest lookback in the indicator set. Raising
    # this makes FEWER assets eligible, on purpose: it trades signal count for
    # signals whose inputs have actually settled.
    # 120 is what Quotex actually serves for these assets -- measured, not
    # assumed. 200 was the theoretical target but every asset failed warm-up
    # at it, and a threshold the broker cannot satisfy is not a quality gate,
    # it is an outage. At 120 the convergence error is already ~13x smaller
    # than at 55 (ADX off by 0.20 instead of 2.61 in the measurement in
    # test_accuracy.py), so nearly all of the accuracy benefit survives.
    #
    # Raise this only if the warm-up status shows the broker delivering more.
    min_candles_for_signal: int = 120
    # Fetch a little deeper than the gate so an asset that lands a few bars
    # short doesn't sit failing warm-up forever at exactly the boundary.
    history_fetch_bars: int = 200

    # How the order is submitted to Quotex. These map to different protocol
    # shapes, and which one an account accepts is not documented anywhere --
    # it has to be established by trying:
    #
    #   "TIMER" -> optionType 100, `time` = duration in seconds
    #   "TIME"  -> optionType 3,   `time` = duration in seconds
    #
    # Default is TIMER because optionType 100 is the path most pyquotex
    # deployments use successfully. Switch to TIME if the broker ignores it.
    # Symptom of the wrong one: the orders/open frame goes out, the broker
    # never answers, and buy() ends in "Timeout waiting for buy confirmation"
    # -- Quotex does not reject a shape it does not understand, it just goes
    # quiet.
    order_time_mode: str = "TIMER"

    max_scan_candidates: int = 12   # top-N assets evaluated per scan cycle (highest payout first).
                                    # Raising this increases signal count WITHOUT touching any
                                    # quality gate -- it simply scans more of the open market.
    enabled_strategies: List[str] = Field(
        default_factory=lambda: [
            "trend_pullback", "ema_ribbon", "supertrend", "adx_di",
            "mean_reversion", "stoch_rsi", "macd_cross",
        ]
    )
    multi_timeframe_confirmation: bool = True  # require/reward higher-TF trend agreement
    strategy_weight_overrides: Dict[str, float] = Field(default_factory=dict)  # optional
    # per-strategy manual weight multiplier (e.g. {"ema_ribbon": 1.2}). Empty by
    # default = no change for any strategy. Applied multiplicatively alongside
    # the existing dynamic-ensemble multiplier in strategies.py's confluence
    # loop -- same mechanism already there for live-performance-based weighting,
    # just also settable by hand. A value of 1.0 (or an absent key) is a no-op.
    mtf_mode: str = "soft"  # "soft" = confidence nudge only (never blocks); "hard" = adds HHTF tier + hard veto on strong counter-trend
    candle_boundary_execution: bool = False  # wait for next candle open before firing, instead of firing mid-candle
    candle_boundary_max_wait: int = 30  # seconds — if the wait would exceed this, execute immediately instead of waiting
    signal_revalidation: bool = True  # re-run full evaluation on fresh data at candle open; skip the trade if it no longer confirms
    precision_mode_enabled: bool = False       # quality-over-quantity gate on top of the normal pipeline -- opt-in
    precision_min_agreeing: int = 3            # min strategies that must vote the winning direction
    precision_max_opposing: int = 0            # max strategies allowed to vote the opposite direction (counter-vote veto)
    precision_require_regime_alignment: bool = True  # at least one agreeing strategy must fit the current regime
    dynamic_ensemble_enabled: bool = False    # scale each strategy's vote weight by its own live win-rate -- opt-in
    dynamic_ensemble_floor: float = 0.7       # min weight multiplier for a currently-underperforming strategy
    dynamic_ensemble_ceiling: float = 1.3     # max weight multiplier for a currently-outperforming strategy
    dynamic_ensemble_regime_blend: float = 0.4   # blend factor [0,1] for regime-specific vs. global win-rate weighting -- only applied when regime_aware_strategy_intelligence_enabled=True below; that toggle being False reproduces the exact original (pure global win-rate) behavior regardless of this value
    shadow_mode_enabled: bool = False   # simulate trades against live prices instead of placing real orders -- validate changes risk-free

    # --- Dead-market filter (strategies.evaluate) --------------------------
    # These used to be fixed constants inside evaluate() with no way to tune
    # them without editing code. Same default values as before -- exposing
    # them here doesn't change behavior until you change a value.
    dead_market_atr_pct_floor: float = 0.03      # below this ATR%, market is too flat to trade at all
    dead_market_adx_threshold: float = 14.0      # ADX below this + BB width collapse = "no trend" chop
    dead_market_bb_ratio: float = 0.55           # BB width vs its 20-period MA ratio for the chop check above
    dead_market_bb_window: int = 20              # rolling window (in candles) for the BB-width baseline MA above
    dead_market_rsi_adx_ceiling: float = 20.0    # only apply the extreme-RSI whipsaw filter below this ADX
    dead_market_rsi_high: float = 85.0           # RSI above this (with low ADX) = reversion risk, skip
    dead_market_rsi_low: float = 15.0            # RSI below this (with low ADX) = reversion risk, skip

    # --- Confluence gate (strategies.evaluate) ------------------------------
    confluence_min_confirmations: int = 2        # min agreeing strategies required when there's any opposition
    confluence_min_directional_edge: float = 0.60  # winning_score / (winning+losing) floor when there's opposition

    # --- Regime classification (strategies._regime) ------------------------
    regime_trend_adx: float = 25.0   # smoothed ADX at/above this = "trend"
    regime_range_adx: float = 18.0   # smoothed ADX below this = "range" (between the two = "mixed")

    # --- Revalidation gate (engine.signal_validation, runs before every
    # execution when signal_revalidation=True, which is the default) --------
    reval_body_ratio_weak: float = 0.15       # candle body/range ratio below this = weak/unreliable candle
    reval_body_ratio_strong: float = 0.75     # above this = strong, more reliable candle
    reval_confluence_high: int = 4            # confluence factor count for "high" confidence bump
    reval_confluence_medium: int = 2          # confluence factor count for "medium" confidence bump
    reval_regime_adx_floor: float = 15.0      # ADX below this + falling >30% vs 50 bars ago = regime destabilizing
    reval_veto_age_ms: float = 5000.0         # setup invalid + signal older than this (ms) = hard veto
    reval_veto_adjustment: float = -20.0      # total confidence adjustment below this = hard veto
    reval_fresh_signal_ms: float = 1000.0     # signal younger than this always passes revalidation (fresh bypass)
    reval_call_setup_rsi_high: float = 85.0   # CALL setup-still-valid: reject if RSI at/above this (overbought exhaustion)
    reval_call_setup_rsi_low: float = 25.0    # CALL setup-still-valid: reject if RSI at/below this
    reval_put_setup_rsi_high: float = 75.0    # PUT setup-still-valid: reject if RSI at/above this
    reval_put_setup_rsi_low: float = 15.0     # PUT setup-still-valid: reject if RSI at/below this (oversold exhaustion)
    reval_confluence_call_rsi_low: float = 45.0   # CALL confluence: RSI must be inside [low, high] to count as a factor
    reval_confluence_call_rsi_high: float = 65.0
    reval_confluence_put_rsi_low: float = 35.0    # PUT confluence: RSI must be inside [low, high] to count as a factor
    reval_confluence_put_rsi_high: float = 55.0
    reval_timing_fresh_ms: float = 500.0      # below this age, zero timing decay
    reval_timing_decay_ms: float = 2000.0     # below this age, mild linear decay; above it, steeper decay to the veto age
    reval_adjustment_clip_min: float = -25.0  # floor for total revalidation confidence adjustment
    reval_adjustment_clip_max: float = 15.0   # ceiling for total revalidation confidence adjustment

    # --- Dynamic Weighted Ensemble win-rate curve (engine.strategy_manager.
    # performance_multiplier) -- NOT the separate mute/trial state machine,
    # which has its own internal MUTE_WIN_RATE/TRIAL_PASS_WIN_RATE constants
    # (kept as-is; muting a strategy entirely is a harder decision than
    # reweighting its vote, and isn't exposed here to avoid conflating the
    # two mechanisms). Only used when dynamic_ensemble_enabled=True. -------
    strategy_mute_win_rate: float = 30.0      # win rate at/below this -> multiplier = floor
    strategy_promote_win_rate: float = 70.0   # win rate at/above this -> multiplier = ceiling
    strategy_trial_win_rate: float = 50.0     # win rate at this midpoint -> multiplier = 1.0 (neutral)

    # --- Stage C: Adaptive Market Context Engine (engine.market_context) ---
    # Master switch is OFF by default -- with it off, MarketContextEngine is
    # never consulted and strategies.evaluate()'s dead-market ADX/ATR% gate
    # behaves exactly as Stage A's static config values (unchanged from the
    # original hardcoded 14.0 / 0.03). Turning this on replaces the ADX
    # trend-floor and ATR% volatility-floor with values computed from this
    # asset+timeframe's OWN recent history instead of one fixed number for
    # every asset, every timeframe, every session.
    adaptive_mode_enabled: bool = False
    adaptive_rolling_window: int = 200        # max recent candles' worth of samples kept per asset+timeframe+metric
    adaptive_min_samples: int = 30            # below this many samples, always use the Stage A static default (cold-start)
    adaptive_ema_alpha: float = 0.15          # smoothing on the published adaptive value (0=frozen, 1=no smoothing)
    adaptive_max_drift_pct: float = 0.05      # max fractional change in the published value per single update
    adaptive_use_zscore: bool = False         # False = percentile-rank based, True = mean/stddev z-score based
    adaptive_percentile: float = 0.2          # 20th percentile of recent history = the adaptive floor threshold
    adaptive_update_interval_s: float = 0.0   # min seconds between recompute passes per asset+timeframe (0 = every call)

    # --- Adaptive RSI (extends the Stage C dead-market gate; strategies.py
    # Condition 3 -- extreme-RSI-with-low-ADX whipsaw filter). Replaces the
    # fixed dead_market_rsi_high/dead_market_rsi_low (85.0/15.0) with bounds
    # derived from THIS asset+timeframe's own recent RSI distribution, same
    # opt-in gate as everything else in adaptive mode (adaptive_mode_enabled
    # must also be True; this doesn't have its own separate master switch).
    adaptive_rsi_high_percentile: float = 0.90   # this asset's own 90th-percentile RSI = adaptive overbought bound
    adaptive_rsi_low_percentile: float = 0.10    # this asset's own 10th-percentile RSI = adaptive oversold bound

    # --- Decision Intelligence Engine (engine.decision_engine) ---
    # Master switch is OFF by default. With it off, the orchestrator never
    # builds or persists a SignalDecision record -- pure instrumentation,
    # zero effect on which signals fire, their confidence, or any gate.
    decision_engine_enabled: bool = False
    # Historical-probability/regime/calibration weight fields feed a new
    # INFORMATIONAL field (SignalDecision.composite_confidence, visible in
    # Monitoring), never a gate. decision_min_confidence_threshold /
    # decision_min_evidence_count / decision_reject_weak_evidence are
    # different: decision_reject_weak_evidence is a REAL gate (see
    # orchestrator.py's _evaluate_asset_signal) -- off by default, and
    # requires decision_engine_enabled too since it has nothing to gate on
    # otherwise. With it off (the default), these two threshold fields are
    # inert, same as before.
    decision_min_confidence_threshold: int = Field(50, ge=0, le=100)
    decision_min_evidence_count: int = Field(1, ge=1, le=10)
    decision_reject_weak_evidence: bool = False   # REAL gate, off by default -- see orchestrator.py
    decision_historical_probability_weight: float = Field(0.34, ge=0.0, le=1.0)
    decision_regime_weight: float = Field(0.33, ge=0.0, le=1.0)
    decision_calibration_weight: float = Field(0.33, ge=0.0, le=1.0)

    # --- Drift Detection (engine.drift_detection) -- Phase 4 ---
    # Off by default. Page-Hinkley delta/threshold are literature-standard
    # starting points, NOT yet validated against this system's own observed
    # variance (see drift_detection.py module docstring) -- treat alerts as
    # provisional until that validation happens (Phase 6).
    drift_detection_enabled: bool = False
    # Higher drift_threshold = fewer false alarms but slower detection;
    # drift_delta = minimum magnitude of change worth accumulating evidence
    # for; drift_min_samples = floor before ANY detection is allowed to
    # fire for a (strategy, regime) cell.
    drift_delta: float = Field(0.005, ge=0.0001, le=0.5)
    drift_threshold: float = Field(25.0, ge=1.0, le=200.0)
    drift_min_samples: int = Field(20, ge=5, le=500)
    drift_cooldown_seconds: float = Field(3600.0, ge=0.0)  # suppress repeat alerts for the same
                                                            # (strategy, regime) cell within this window
    drift_auto_trial_mode: bool = True   # if False, alerts are still detected/logged/visible but
                                         # don't change any strategy's state ("detect only" mode)
    drift_auto_mute: bool = False        # if True, a severe alert (2x threshold) mutes directly
                                         # instead of moving to trial -- opt-in, off by default

    # --- Data-driven confidence model (engine.confidence_model) -- Phase 5 ---
    # Off by default. Shadow-only even when on: the fitted model's
    # prediction is computed and logged (SignalDecision.
    # shadow_confidence_model_prediction) alongside the real, hand-tuned-
    # formula confidence that actually trades -- never applied to a trade
    # until explicitly promoted out of shadow.
    confidence_model_enabled: bool = False
    confidence_model_shadow_mode: bool = True   # True (default) = exact original behavior: prediction
                                                 # computed and logged only. False = promoted out of
                                                 # shadow -- a trusted prediction is blended 50/50 into
                                                 # the live calibrated confidence (bounded, not a
                                                 # replacement). See orchestrator.py's confidence pipeline.
    confidence_model_min_training_samples: int = Field(200, ge=50, le=5000)
    confidence_model_prediction_threshold: int = Field(300, ge=50, le=5000)  # sample count above
                                                                              # which predictions are trusted enough to surface
    confidence_model_retrain_interval: int = Field(50, ge=10, le=2000)  # retrain after this many new closed trades
    confidence_model_l2_regularization: float = Field(1.0, ge=0.0, le=100.0)
    confidence_model_learning_rate: float = Field(0.3, gt=0.0, le=2.0)
    confidence_model_max_iterations: int = Field(1000, ge=50, le=20000)

    # --- Adaptive Market Regime Transition Detector (engine.regime_transition_detector) ---
    # Off by default. With it off, the orchestrator never instantiates or
    # calls the detector -- zero effect on any signal. When on: it
    # INFLUENCES confidence (a multiplier and a threshold bump), can
    # request a short entry cooldown, and can pause entries entirely
    # during a detected chaotic transition -- it never forces a direction
    # and never overrides an existing gate's accept/reject decision on a
    # signal that does fire. The detector itself is a from-scratch rewrite
    # (Welford's-algorithm running stats, real multi-timeframe confirmation,
    # reads indicators already computed by incremental.py rather than
    # recomputing them) with materially different, NOT yet
    # account-validated thresholds -- treat this flag the same as every
    # other Phase N flag above: off until you've watched it in shadow via
    # the logs/status() output and are comfortable with what it does on
    # this account's actual data.
    #
    # merge note: this flag (and the module actually checking it before
    # running) doesn't exist in the source this detector was merged from --
    # that version wired the detector into the live pipeline unconditionally,
    # with no way to disable it short of a code change. Every other engine
    # feature in this codebase ships behind an explicit off-by-default flag,
    # including the detector's own previous implementation, so this flag was
    # added back during the merge to restore that same guarantee rather than
    # ship a production trading system with a new, unvalidated,
    # signal-suppressing module that can't be turned off. See MERGE_REPORT.md.
    regime_transition_detector_enabled: bool = False

    # --- Strategy Performance Manager tuning (engine.strategy_manager) -- Phase 6 ---
    # Sample-size floors and breakeven-derived win-rate bars for the
    # mute/trial/reactivate state machine. strategy_rolling_window must
    # stay comfortably above strategy_min_sample (enforced defensively in
    # StrategyPerformanceManager.__init__ regardless of what's set here) --
    # otherwise results get capped before the mute check can ever fire.
    strategy_min_sample: int = Field(30, ge=5, le=1000)
    strategy_trial_size: int = Field(20, ge=5, le=500)
    strategy_rolling_window: int = Field(50, ge=10, le=2000)
    strategy_typical_payout_pct: float = Field(80.0, ge=1.0, le=100.0)
    strategy_base_cooldown_seconds: float = Field(21600.0, ge=0.0)    # 6h -- first cooldown before a trial
    strategy_max_cooldown_seconds: float = Field(259200.0, ge=0.0)    # 3 days -- cooldown cap
    adaptive_wilson_confidence_level: float = Field(
        0.95, ge=0.80, le=0.999,
    )  # confidence level for every Wilson-bound calculation in the codebase (strategy
       # mute/trial, calibration intervals, rejection reconciliation) -- 0.95 (z~=1.96)
       # matches what was already hardcoded everywhere; changing this raises/lowers the
       # evidence bar for ALL of those decisions at once, consistently.
    adaptive_learning_ewma_alpha: Optional[float] = Field(
        None, ge=0.0, le=1.0,
    )  # None (default) = exact original behavior: strategy_manager.py's rolling-window
       # mute check only. When set, an EWMA win-rate estimate is maintained ALONGSIDE the
       # rolling window as an additional, faster-reacting mute trigger -- can only mute a
       # strategy sooner, never prevents/delays the existing check. See StrategyState.ewma_win_rate.
    adaptive_learning_drift_weight: Optional[float] = Field(
        None, ge=0.0, le=1.0,
    )  # None (default) = exact original behavior: a drift alert is "severe" purely by
       # magnitude (drift_auto_trial_mode/drift_auto_mute above still control what happens
       # once severe is decided). When set, blends magnitude with the strategy's current
       # win-rate deficit as an ADDITIONAL way to reach "severe" -- can only add an escalation
       # path, never remove the existing magnitude-only one. See orchestrator.py's drift block.

    # --- Rejected Signal Reconciliation (engine.rejection_reconciliation) -- Phase 3 ---
    # Previously always ran whenever decision_engine_enabled was on, with a
    # hardcoded 3s poll interval and a hardcoded min_sample_size=30 at the
    # report endpoint. Decoupled here so it can be tuned/disabled independently.
    rejection_reconciliation_enabled: bool = True   # only takes effect when decision_engine_enabled
                                                     # is also on (reconciliation needs the DecisionStore
                                                     # records that flag produces)
    rejection_reconciliation_interval_seconds: float = Field(3.0, ge=0.5, le=300.0)
    rejection_min_sample_size: int = Field(30, ge=5, le=1000)

    # --- Confidence calibration tuning (engine.calibration) -- Phase 6 ---
    calibration_min_samples_total: int = Field(30, ge=5, le=2000)
    calibration_refresh_every_n_trades: int = Field(10, ge=1, le=1000)
    calibration_refresh_max_age_seconds: float = Field(3600.0, ge=60.0)
    calibration_holdout_validation_enabled: bool = True   # Phase 6's train/holdout Brier-score
                                                           # validation before adopting a refresh. Off
                                                           # reverts to always adopting a fresh refresh directly.
    calibration_rollback_on_worse_enabled: bool = True    # only meaningful when holdout validation is
                                                           # on -- if a candidate refresh scores worse,
                                                           # roll back instead of adopting it

    # --- Adaptive Volume (strategies.py evaluate() "General volume
    # confirmation" section). Stage A tunables first (identical defaults to
    # the values previously hardcoded there), then Stage C adaptive
    # percentiles for the same opt-in gate as everything above.
    vol_confirmation_low: float = 0.7            # vol_strength below this -> confidence penalty (static/cold-start default)
    vol_confirmation_high: float = 1.5           # vol_strength above this -> confidence bonus (static/cold-start default)
    vol_confirmation_low_penalty: float = 6.0    # confidence points subtracted for below-average volume
    vol_confirmation_high_bonus: float = 4.0     # confidence points added for strong volume confirmation
    adaptive_vol_low_percentile: float = 0.20    # this asset's own 20th-percentile vol_strength = adaptive low bound
    adaptive_vol_high_percentile: float = 0.80   # this asset's own 80th-percentile vol_strength = adaptive high bound

    # --- Adaptive Support/Resistance (strategies.py evaluate() "General
    # support/resistance proximity check"). The static proximity band is a
    # flat 0.15% distance regardless of the asset's typical volatility --
    # adaptive mode instead scales it to this asset+timeframe's own recent
    # median ATR%, since "close to a level" means something different for
    # a calm major pair than a volatile OTC one. Reuses the existing
    # "atr_pct" rolling history (no new observe() call needed).
    sr_proximity_pct: float = 0.15               # flat proximity band, percent of price (static/cold-start default)
    sr_proximity_penalty_base: float = 5.0        # base confidence penalty for being inside the proximity band
    adaptive_sr_atr_multiplier: float = 1.0       # adaptive proximity band = this * this asset's own median ATR%

    # --- Adaptive Confluence (strategies.py evaluate() confluence gate).
    # Scales confluence_min_directional_edge by this asset's own ADX
    # percentile rank -- a stronger-than-usual trend needs less strategy
    # agreement, a weaker one needs more. Reuses the "adx" history already
    # tracked for the dead-market gate.
    adaptive_confluence_edge_low: float = 0.55    # required edge at this asset's strongest recent trend (percentile rank 100)
    adaptive_confluence_edge_high: float = 0.70   # required edge at this asset's weakest recent trend (percentile rank 0)

    # --- Production Adaptive Settings UI: per-feature ON/OFF toggles ------
    # adaptive_mode_enabled (above) is the master switch. Every toggle below
    # only does anything when the master switch is also True; each one
    # independently falls back to that feature's static/legacy value when
    # turned off, without affecting any other adaptive feature. Internal
    # tuning knobs (rolling window, EMA alpha, percentile, z-score, drift
    # limit) are deliberately NOT exposed here -- those stay implementation
    # detail, set via the adaptive_* fields above with sensible defaults.
    adaptive_rsi_enabled: bool = True
    adaptive_adx_enabled: bool = True
    adaptive_atr_enabled: bool = True
    adaptive_volume_enabled: bool = True
    adaptive_sr_enabled: bool = True
    adaptive_confluence_enabled: bool = True
    adaptive_signal_validation_enabled: bool = True
    # Reserved: no adaptive integration exists yet for these two. The
    # toggles are here so the Settings UI has a stable place for them once
    # built, but flipping them currently has no effect on live behavior --
    # deliberately not wired to avoid touching precision_gate.py's strict
    # gate or risk.py's money-handling logic without a dedicated,
    # separately-tested implementation pass.
    adaptive_precision_gate_enabled: bool = True
    adaptive_risk_enabled: bool = True

    # Internal magnitudes for the two toggles above -- not shown in the
    # Settings UI (per "don't expose internal algorithm parameters"), same
    # pattern as adaptive_confluence_edge_low/high.
    adaptive_precision_agreeing_low: int = 2
    adaptive_precision_agreeing_high: int = 4
    adaptive_risk_stake_multiplier_low: float = 0.85
    adaptive_risk_stake_multiplier_high: float = 1.05
    adaptive_risk_cooldown_multiplier_low: float = 1.0
    adaptive_risk_cooldown_multiplier_high: float = 1.5
    # Regime-aware Strategy Intelligence: applies dynamic_ensemble_regime_blend
    # (an internal magnitude, not exposed) to performance_multiplier() --
    # only has an effect when dynamic_ensemble_enabled is ALSO True, since
    # it blends INTO that mechanism rather than being independent of it.
    regime_aware_strategy_intelligence_enabled: bool = False
    # Calibration: ON/OFF for the existing per-regime isotonic
    # ConfidenceCalibrator. OFF = use the pre-calibration confidence value
    # directly (the original, pre-Calibration-Engine behavior).
    calibration_enabled: bool = True
    analytics_min_sample_size: int = 10   # Analytics Engine (read-only): min closed trades in a bucket before its win rate is flagged "significant"

    # --- Pattern Risk Engine (see engine/pattern_risk_engine.py). Always
    # shadow-scores every trade (advisory report below); pattern_risk_
    # enforcement_enabled (further down) is a separate, off-by-default
    # opt-in for a real effect beyond the advisory report.
    pattern_risk_min_sample_size: int = 10

    # --- Recovery Engine (see engine/recovery_engine.py). Always computes
    # the advisory assessment (analytics_update broadcast); recovery_auto_
    # resume_enabled (further down) is a separate, off-by-default opt-in
    # for a real effect beyond the advisory report.
    recovery_min_shadow_sample: int = 20
    recovery_consistency_window: int = 20
    recovery_win_rate_baseline: float = 50.0
    recovery_score_threshold: float = 65.0

    # Real, opt-in effects layered on top of the always-on shadow scoring
    # above -- both off by default, and both bounded/conservative even when
    # on (enforcement only ever extends a cooldown, never blocks/resizes a
    # trade that already executed since pattern-risk scoring needs data
    # only available post-execution; auto-resume only ever acts on a pause
    # caused by the drawdown breaker specifically, never a manual
    # kill-switch/Telegram pause). See orchestrator.py for exactly where
    # each is applied.
    pattern_risk_enforcement_enabled: bool = False   # canary rollout (pattern_risk_rollout_pct)
    # that extends the asset's cooldown when recommend_reject fires -- requires Precision
    # Mode on too, since that's the only code path that reads asset cooldowns
    pattern_risk_rollout_pct: float = 0.0             # 0-100: fraction of recommend_reject-flagged trades enforcement applies to
    recovery_auto_resume_enabled: bool = False        # auto-calls RiskManager.resume() when paused
    # by the drawdown breaker specifically, and the assessment is both significant and recommends it


class MarketInitSettings(BaseModel):
    """Market Initialization Manager — bounded, deterministic history
    warm-up for every eligible asset before the scanner is allowed to
    evaluate it. See services/market_init.py for the state machine this
    configures. Adding this field is backward compatible: any
    runtime_settings.json saved before this existed simply doesn't have a
    "market_init" key, and pydantic fills in these defaults for it."""
    workers: int = 8                          # concurrent history-loading workers
    max_retries: int = 3                      # TOTAL attempts per asset before FAILED (not retries beyond the first — see market_init.py's module docstring)
    retry_backoff_base_seconds: float = 1.0   # attempt N's backoff = base * 2^(N-1): 1s, 2s, 4s for base=1.0
    recovery_interval_seconds: float = 60.0   # how often FAILED assets are automatically retried in the background
    history_timeout_seconds: float = 15.0     # per-attempt timeout for the whole get_candles call
    history_count: int = 200                  # candles requested per attempt. Kept >= trading.min_candles_for_signal to match
                                              # trading.history_fetch_bars: warm-up must load at least as
                                              # much history as the scan loop needs, or an asset reaches
                                              # READY and is then rejected on every evaluation for being
                                              # short -- warm-up would certify data the scanner refuses.
    gap_multiplier: float = 1.5               # same convention as the admin Candle Dashboard's gap detection
    gap_tolerance: int = 2                    # RCA F12: how many over-long gaps one history pull may contain
                                              # before the asset is rejected. Was effectively 0 -- the first
                                              # gap failed the whole pull even when 119 of 120 bars were
                                              # perfectly good, and pyquotex never pads the holes it leaves.
                                              # Duplicate / non-increasing timestamps are still a hard reject.
                                              # 0 restores the old fail-on-first-gap behaviour.


class MarketDataSettings(BaseModel):
    """Realtime market-data feature layer.

    Everything here is *additive*: it configures features computed on top of the
    existing OHLC engine. Disabling sentiment (`sentiment_enabled = false`) makes
    the signal path byte-for-byte what it was before this layer existed.

    Backward compatible by construction: a `runtime_settings.json` written before
    this class existed has no `market_data` key, and pydantic fills these defaults.
    """

    # ── price-action / microstructure features ─────────────────────────────
    features_enabled: bool = True             # compute the OHLC price-action columns
    tick_features_enabled: bool = True        # compute tick-activity features
    max_ticks_per_bar: int = 512              # bound per-bar tick memory (activity saturates)
    tick_baseline_bars: int = 40              # closed bars used as the activity baseline

    # ── trader sentiment (optional, bounded, never a signal source) ────────
    sentiment_enabled: bool = False           # OFF by default: opt-in only
    sentiment_max_age_seconds: float = 120.0  # older than this => ignored, not penalised
    sentiment_min_strength: float = 0.10      # |bias| below this => ignored (10 percentage pts)
    sentiment_max_confidence_adjustment: float = 4.0   # hard cap, in confidence points (3-5)
    sentiment_disagreement_penalty: Optional[float] = None  # default: half the cap

    # ── data quality ───────────────────────────────────────────────────────
    price_max_age_seconds: float = 90.0       # last tick older than this => stale_price
    min_bars_for_features: int = 20           # below this, features stay neutral

    # ── Market Evidence Score (bounded, independent confirmation) ───────────
    # A separate, additive confidence nudge derived from tick ACTIVITY and OHLC
    # price action. It never selects or reverses a direction, never bypasses
    # calibration / the effective-threshold gate / the precision gate, and uses
    # no volume of any kind -- PyQuotex publishes none.
    market_evidence_enabled: bool = True
    market_evidence_max_adjustment: float = 5.0     # hard cap in confidence points
    market_evidence_disagreement_penalty: float = 3.0
    # Activity component
    market_evidence_activity_confirmation_threshold: float = 0.25
    market_evidence_min_ticks_for_activity: int = 8       # minimum tick data quality
    market_evidence_min_activity_intensity: float = 0.40
    market_evidence_max_activity_intensity: float = 4.00
    # Price-action component
    market_evidence_price_action_threshold: float = 0.20
    market_evidence_headroom_min_atr: float = 0.35
    # Combination
    market_evidence_require_both_components: bool = True
    market_evidence_activity_weight: float = 0.45
    market_evidence_price_action_weight: float = 0.55


class RuntimeSettings(BaseModel):
    # Active market/execution provider. None => use env MARKET_PROVIDER.
    provider: Optional[Literal["paper", "pyquotex"]] = None
    trading: TradingSettings = Field(default_factory=TradingSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    ux: UXSettings = Field(default_factory=UXSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    market_init: MarketInitSettings = Field(default_factory=MarketInitSettings)
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)

    @classmethod
    def load(cls, user_id: Optional[str] = None) -> "RuntimeSettings":
        path = user_data_dir(user_id) / "runtime_settings.json" if user_id else SETTINGS_FILE
        if path.exists():
            try:
                inst = cls.model_validate_json(path.read_text(encoding="utf-8"))
                # One-time correction: the 200-bar default shipped briefly and
                # was saved into existing settings files, where it blocks ALL
                # warm-up because Quotex serves ~120. Changing the class
                # default alone would never reach an already-saved file, so a
                # value that is known-unreachable is pulled back to one that
                # works. Anything <= 120 is left alone -- this only undoes the
                # unreachable setting, it does not override a deliberate
                # choice, and a user who later raises it again keeps that.
                if int(getattr(inst.trading, "min_candles_for_signal", 120)) > 120:
                    import logging
                    logging.getLogger(__name__).warning(
                        "min_candles_for_signal was %s, which no asset can reach with this "
                        "broker's history depth — resetting to 120",
                        inst.trading.min_candles_for_signal,
                    )
                    inst.trading.min_candles_for_signal = 120
                    if int(getattr(inst.trading, "history_fetch_bars", 200)) > 200:
                        inst.trading.history_fetch_bars = 200
                    if int(getattr(inst.market_init, "history_count", 200)) > 200:
                        inst.market_init.history_count = 200
                    inst.save(user_id)
                return inst
            except Exception as exc:
                # BUG FIX: Log settings loading failures
                import logging
                logging.getLogger(__name__).warning("Failed to load settings for %s: %s", user_id, type(exc).__name__)
        inst = cls()
        inst.save(user_id)
        return inst

    def save(self, user_id: Optional[str] = None) -> None:
        path = user_data_dir(user_id) / "runtime_settings.json" if user_id else SETTINGS_FILE
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")


settings = Settings()
