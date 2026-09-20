"""The trading orchestrator — connects market data, engine, risk, execution,
broadcasting, and Telegram into one coherent live loop.

Flow per scan tick:
  1. Refresh balance + assets (payout/open filter).
  2. For each candidate asset, pull candles and run the confluence engine.
  3. Best qualifying signal -> risk gate -> (auto: execute) / (manual: emit).
  4. Track active trades; on resolution update risk + store + broadcast.
"""
from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .config import RuntimeSettings, settings as env_settings, user_data_dir
from .engine import strategies
from .engine import indicators as ta
from .engine.calibration import ConfidenceCalibrator
from .engine.correlation import CorrelationGuard
from .engine.decision_engine import build_decision, DecisionStore, compute_composite_confidence
from .engine.rejection_reconciliation import RejectedOutcomeStore
from .engine.drift_detection import DriftDetector
from .engine.confidence_model import ConfidenceModelStore, extract_features, build_training_set, FEATURE_NAMES
from .engine.regime_transition_detector import RegimeTransitionEngine, TimeframeSignal
from .engine.error_recovery import ErrorRecovery, RetryConfig
from .engine.task_supervisor import TaskSupervisor
from .engine.health_score import HealthScoreManager
from .engine.incremental import IndicatorCache
from .engine.market_features import (
    MarketFeatures,
    QualityReport,
    SentimentAdjustment,
    sentiment_confidence_adjustment,
)
from .services.realtime_state import RealtimeStateManager
from .engine.latency import LatencyTracker
from .engine.precision_gate import evaluate_precision
from .engine.queue_persistence import QueuePersistence
from .engine.shadow_mode import ShadowStore, ShadowTrade
from .engine.regime_tracker import RegimePerformanceTracker
from .engine.strategy_manager import StrategyPerformanceManager
from .engine.trade_engine import TradeEngine
from .engine.risk import RiskManager
from .engine.signal_validation import SignalQualityValidator
from .engine.pipeline_tracer import PipelineTracer, PipelineStage
from .engine.pipeline_events import PipelineEventBus
from .engine.production_metrics import ProductionMetrics
from .engine.market_context import MarketContextEngine, AdaptiveThresholdConfig
from .engine.analytics_engine import AnalyticsEngine
from .engine.pattern_risk_engine import PatternRiskEngine
from .engine.pattern_risk_validation import PatternRiskValidationStore, ValidationRecord, compute_validation_report
from .logging_setup import log_event
from .engine.recovery_engine import RecoveryEngine
from .schemas import Direction, EngineState, Signal, SignalDecision, TradePlan, TradeRecord, TradeFeatureSnapshot, TradeStatus, now_ts
from .services.candle_store import CandleStore
from .services.market import MarketProvider, OrderNotSent, build_provider, TIMEFRAMES
from .services.market_init import MarketInitManager, MARKET_INIT_SERVICE_NAME
from .services.telegram_bot import TelegramService
from .services.validation_service import ValidationService
from .session_manager import BrokerCredentials, session_manager
from .store import TradeStore
from .ws_hub import hub

RECONNECT_BASE_DELAY = 2.0    # seconds — first retry after a Quotex disconnect
RECONNECT_MAX_DELAY = 60.0    # seconds — backoff ceiling
WATCHDOG_INTERVAL = 5.0       # seconds — how often we check connection health
SIGNAL_RETENTION_SECONDS = 300  # keep terminal (expired/executed/rejected/skipped)
                                 # signals around for 5 min (UI history), then purge —
                                 # without this, self.signals grows unbounded for the
                                 # lifetime of the process (a slow memory leak on any
                                 # multi-hour/multi-day run).
_TERMINAL_SIGNAL_STATUSES = ("expired", "executed", "rejected", "skipped")
CANDLE_RETENTION_DAYS = 30.0        # High-severity fix: candles table previously had no retention at all
CANDLE_PRUNE_INTERVAL_SECONDS = 86400.0  # run the prune at most once/day per user, not every scan cycle

# BUG FIX: background-loop supervision. _scan_loop/_result_loop/_watchdog_loop/
# _shadow_loop/_rejection_reconciliation_loop each run as a bare fire-and-forget
# asyncio.create_task with nothing checking whether they're still alive. A
# single unhandled exception anywhere inside one (most dangerously
# _watchdog_loop, which previously had no try/except of its own at all)
# permanently kills that one Task -- the process stays up, every *other* loop
# keeps going, so nothing looks like a crash. This is the "pipeline silently
# stops, no fatal error shown, requires manual restart" failure mode. See
# engine/task_supervisor.py, which is the single mechanism that does this.
# These three are the tuning knobs handed to TaskSupervisor.supervise() in
# start() -- they came from the pipeline-fix change set and are preserved
# here verbatim; only the implementation they feed moved.
TASK_LOOP_RESTART_DELAY_SECONDS = 3.0        # base backoff before restarting a crashed loop
TASK_LOOP_MAX_RESTARTS = 8                   # restarts allowed within the window below...
TASK_LOOP_RESTART_WINDOW_SECONDS = 300.0     # ...before giving up and marking it "stuck" for manual restart
# Ceiling on ONE scan cycle's asset-evaluation phase. Deliberately far
# above a healthy cycle (all candidates run concurrently and normally
# finish in well under a second) -- this is a liveness bound, not a
# performance knob. Its only job is to make sure one asset that is slow
# for any reason cannot hold the whole cycle open: assets that finished
# are used, the stragglers are cancelled and simply skipped for THIS
# cycle, and the next cycle starts on time.
# The scan loop is event-driven (new-candle wakeup) with this as the
# backstop, so the next cycle is due at most this long after the last one.
SCAN_INTERVAL_BACKSTOP = 3.0
SCAN_CYCLE_BUDGET_SECONDS = 90.0
# SECONDARY safety net only (the root-cause fixes above are the primary
# remedy). If health["last_scan_at"] has not advanced in this long while
# the orchestrator believes it is running and connected, the scan loop is
# not progressing no matter what Task.done() says. Must be comfortably
# larger than SCAN_CYCLE_BUDGET_SECONDS or a legitimately slow-but-
# recovering cycle would be killed mid-flight.
SCAN_PROGRESS_STALL_SECONDS = 180.0
PROVIDER_CALL_TIMEOUT_SECONDS = 25.0         # hard ceiling on a single broker call from _scan_loop, so a
                                              # hung network call blocks that cycle for at most this long
                                              # instead of freezing the loop forever with no exception raised
# Single attempt at placing an order -- see the note at the call site for why
# this is never retried. Sized above the provider's own internal budget
# (3x10s prepare + 30s confirm) so the provider reports a definite outcome
# first, and below TradeEngine.ORDER_TIMEOUT_SECONDS (75s) so THIS layer is
# the one that answers rather than the outer unknown-outcome guard.
ORDER_PLACE_TIMEOUT_SECONDS = 65.0
# Pre-trade revalidation candle fetches. Deliberately short: this runs on the
# critical path immediately before an order, and the whole executor has to
# finish inside ORDER_TIMEOUT_SECONDS. Both fetches together must leave room
# for place_order.
REVALIDATION_FETCH_TIMEOUT_SECONDS = 8.0
PROVIDER_CONNECT_TIMEOUT_SECONDS = 60.0      # ceiling for the initial/reconnect login call specifically --
                                              # deliberately more generous than PROVIDER_CALL_TIMEOUT_SECONDS
                                              # since a real login can legitimately involve OTP waits and
                                              # pyquotex's own internal retry-polling (see services/market.py);
                                              # this is a backstop for a genuine hang, not a normal-latency trip

# Ceiling for ONE settlement check in _result_loop (RCA F8). Two layers matter
# here: the provider imposes its own CHECK_RESULT_TIMEOUT (30s) around
# pyquotex's check_win, and this is the outer backstop covering a provider that
# ignores it or hangs somewhere else entirely. It must stay well below
# pyquotex's internal 300s, because that call returns ("loss", 0.0) when it
# gives up -- a timeout must surface as "still pending", never as a loss.
RESULT_CHECK_TIMEOUT = 45.0

# How long a trade left open by a restart stays tracked while we wait for the
# broker to account for it (RCA F8). Past this it is marked `error` in the
# store -- visible as unresolved -- instead of being deleted. 30 minutes is far
# longer than any supported option duration, so a trade still unknown by then
# is not going to resolve on its own.
UNRECOVERABLE_TRADE_AFTER_SECONDS = 1800.0

logger = logging.getLogger("orchestrator")

def signal_ttl_seconds(timeframe: str, regime: Optional[str]) -> float:
    """How long after creation a signal is still enterable.

    Single source of truth. The codebase previously computed this in three
    different places with three different formulas -- tf*1.2 (min 30s) for
    the boundary-replacement check, tf*2.5 for the post-wait recheck, and
    tf*2.0*regime_multiplier at the execution gate. The UI showed none of
    them, so a signal could look fresh on screen while the execution gate
    had already expired it.
    """
    tf_seconds = TIMEFRAMES.get(timeframe, 60.0)
    base_ttl = tf_seconds * 2.0
    if regime == "trend":
        return base_ttl * 1.2      # trends stay valid a little longer
    if regime == "mixed":
        return base_ttl * 0.9      # stricter when the regime is unclear
    return base_ttl



# Higher timeframe consulted for multi-timeframe confirmation, keyed by the
# user's primary trading timeframe.
HTF_MAP = {
    "1m": "5m", "2m": "5m", "3m": "15m",
    "5m": "15m", "15m": "30m", "30m": "1h", "1h": "4h",
    "4h": "1day", "1day": "1day",  # 1day has no higher TF to confirm against
}
# Second-tier (highest) timeframe, only consulted when mtf_mode == "hard".
HHTF_MAP = {
    "1m": "15m", "2m": "15m", "3m": "30m",
    "5m": "1h",  "15m": "4h",  "30m": "4h", "1h": "1day",
    "4h": "1day", "1day": "1day",
}


def _candle_phase(timeframe: str) -> dict:
    """Where are we inside the current candle right now.

    Returns wait_secs (seconds until next candle open), phase ("early"/"mid"/
    "late"), and pct_done. Used by candle_boundary_execution to decide whether
    to wait for the next candle open before firing an order, or (if we're in
    the very early part of the candle) execute immediately instead of waiting
    almost a full extra period.
    """
    period = TIMEFRAMES.get(timeframe, 60.0)
    elapsed = time.time() % period
    pct_done = elapsed / period if period else 0.0
    wait_secs = max(0.0, period - elapsed)
    if pct_done < 0.15:
        phase = "early"
        wait_secs = 0.0  # close enough to the last boundary — execute now
    elif pct_done > 0.75:
        phase = "late"
    else:
        phase = "mid"
    return {"wait_secs": wait_secs, "elapsed": elapsed, "period": period,
            "phase": phase, "pct_done": pct_done}


class Orchestrator:
    """One instance per logged-in user. Nothing here is module-global state —
    provider connection, risk counters, active trades, and trade history are
    all owned by this instance, so N users running concurrently never share
    state (isolated market cache, isolated broker session, isolated risk)."""

    def __init__(self, user_id: str, runtime: RuntimeSettings) -> None:
        self.user_id = user_id
        self.runtime = runtime
        self.store = TradeStore(user_id)
        self._otp_future: Optional[asyncio.Future] = None
        self.provider: MarketProvider = build_provider(
            self._provider_name(), self._broker_settings(), self._otp_callback, self._sessions_dir()
        )
        if self._provider_name() == "pyquotex" and self.provider.name != "pyquotex":
            logger.warning(
                "[account] Provider set to 'pyquotex' but no credentials/SSID configured -- "
                "silently using '%s' instead. Tournament mode (and all Quotex-specific features) "
                "will be unavailable until credentials are added in Settings.", self.provider.name,
            )
        self.risk = RiskManager(runtime.risk)
        self.state = EngineState(provider=self.provider.name, is_demo=runtime.trading.is_demo,
                                 mode=runtime.trading.mode,
                                 tournament_supported=self.provider.supports_tournaments())
        self.signals: Dict[str, Signal] = {}
        self.active_trades: Dict[str, TradeRecord] = {}
        # Results recovered from the broker's own trade history at startup
        # (RCA F8), keyed by order id. `_bounded_check_result` drains this
        # first: after a restart the websocket slot that would have delivered
        # the outcome no longer exists, so waiting on `check_win` for these
        # orders can only burn its timeout and report "unknown" forever.
        self._recovered_results: Dict[str, dict] = {}
        self._last_execution_refusal: Optional[str] = None
        self.trade_engine = TradeEngine(
            self._do_execute_signal,
            on_unreconciled=self._on_order_unreconciled,
            allow_duplicates=lambda: bool(
                getattr(self.runtime.risk, "allow_duplicate_orders", False)),
            block_on_unknown=lambda: bool(
                getattr(self.runtime.risk, "block_trading_on_unknown_order", True)),
        )
        self.telegram = TelegramService()
        self.strategy_manager = StrategyPerformanceManager(
            user_data_dir(user_id),
            min_sample=int(getattr(runtime.trading, "strategy_min_sample", 30)),
            trial_size=int(getattr(runtime.trading, "strategy_trial_size", 20)),
            rolling_window=int(getattr(runtime.trading, "strategy_rolling_window", 50)),
            typical_payout_pct=float(getattr(runtime.trading, "strategy_typical_payout_pct", 80.0)),
            base_cooldown=float(getattr(runtime.trading, "strategy_base_cooldown_seconds", 21600.0)),
            max_cooldown=float(getattr(runtime.trading, "strategy_max_cooldown_seconds", 259200.0)),
            wilson_confidence_level=float(getattr(runtime.trading, "adaptive_wilson_confidence_level", 0.95)),
            ewma_alpha=getattr(runtime.trading, "adaptive_learning_ewma_alpha", None),
            drift_weight=getattr(runtime.trading, "adaptive_learning_drift_weight", None),
        )
        self.calibrator = ConfidenceCalibrator(
            min_samples_total=int(getattr(runtime.trading, "calibration_min_samples_total", 30)),
            refresh_every_n_trades=int(getattr(runtime.trading, "calibration_refresh_every_n_trades", 10)),
            refresh_max_age=float(getattr(runtime.trading, "calibration_refresh_max_age_seconds", 3600.0)),
            holdout_validation_enabled=bool(getattr(runtime.trading, "calibration_holdout_validation_enabled", True)),
            rollback_on_worse_enabled=bool(getattr(runtime.trading, "calibration_rollback_on_worse_enabled", True)),
        )
        self.regime_tracker = RegimePerformanceTracker()
        self.health_manager = HealthScoreManager(user_id)
        self.candle_store = CandleStore(user_id)
        self.decision_store = DecisionStore(str(user_data_dir(user_id) / "decisions"))
        self.rejected_outcome_store = RejectedOutcomeStore(str(user_data_dir(user_id) / "rejected_outcomes.json"))
        self.drift_detector = DriftDetector(
            str(user_data_dir(user_id) / "drift_detection.json"),
            delta=float(getattr(runtime.trading, "drift_delta", 0.005)),
            threshold=float(getattr(runtime.trading, "drift_threshold", 25.0)),
            min_samples=int(getattr(runtime.trading, "drift_min_samples", 20)),
            cooldown_seconds=float(getattr(runtime.trading, "drift_cooldown_seconds", 3600.0)),
        )
        self.confidence_model_store = ConfidenceModelStore(str(user_data_dir(user_id) / "confidence_model.json"))
        self.confidence_model_store.model.min_samples_to_train = int(getattr(runtime.trading, "confidence_model_min_training_samples", 200))
        self.confidence_model_store.model.min_samples_to_trust = int(getattr(runtime.trading, "confidence_model_prediction_threshold", 300))
        self._last_confidence_model_retrain_count = 0
        self.regime_transition_engine = RegimeTransitionEngine()
        self._regime_cooldown_until_bar: Dict[str, int] = {}
        self._regime_bar_counter: Dict[str, int] = {}
        self.correlation_guard = CorrelationGuard(
            self.candle_store,
            threshold=runtime.risk.correlation_threshold,
        )
        self.validation = ValidationService(
            user_id,
            get_runtime=lambda: self.runtime,
            get_provider=lambda: self.provider,
            get_calibrator=lambda: self.calibrator,
            get_regime_tracker=lambda: self.regime_tracker,
            get_strategy_manager=lambda: self.strategy_manager,
            health_manager=self.health_manager,
        )
        self.validation.correlation.threshold = runtime.risk.correlation_threshold
        self._live_regime: Dict[str, str] = {}  # asset -> current regime, for the status API
        self._indicators = IndicatorCache()
        # Realtime tick-activity + sentiment state, keyed by (asset, timeframe).
        # Constructed here rather than lazily so a settings reload cannot leave
        # the scan loop holding a manager sized for the old limits.
        _md = getattr(runtime, "market_data", None)
        self._realtime = RealtimeStateManager(
            max_ticks_per_bar=int(getattr(_md, "max_ticks_per_bar", 512)),
            baseline_bars=int(getattr(_md, "tick_baseline_bars", 40)),
        )
        self.latency = LatencyTracker()
        self.signal_validator = SignalQualityValidator(**self._reval_kwargs())  # Signal revalidation + quality checks
        # Per-user instances (NOT the module-level singletons in these files —
        # those are shared across every user's process and would leak/collide
        # trace + metrics data across tenants). Previously imported but never
        # actually called anywhere: dead observability code.
        self.pipeline_tracer = PipelineTracer(max_traces=500, on_stage=self._on_trace_stage)
        self._pipeline_events = PipelineEventBus()
        self.production_metrics = ProductionMetrics(max_history=5000)
        # Stage C: adaptive market-context engine, also per-user (own rolling
        # stats per asset+timeframe, never shared across tenants). Inert
        # unless TradingSettings.adaptive_mode_enabled=True (default False).
        self.market_context = MarketContextEngine(self._adaptive_cfg())
        # Read-only trade analytics (Loss Intelligence Modules 2+3+8). Never
        # called from anywhere in the scan/execute path -- computes stats
        # on demand from self.store's existing trade history, purely for
        # reporting. min_sample_size is the only tunable, since everything
        # else about this engine is presentational, not a trading decision.
        self.analytics_engine = AnalyticsEngine(
            min_sample_size=int(getattr(runtime.trading, "analytics_min_sample_size", 10))
        )
        # Pattern Risk Engine and Recovery Engine: read-only / advisory
        # only. Neither is called from anywhere in the scan/execute/pause
        # path -- both exist purely so a candidate trade or a paused
        # account's shadow performance can be scored on demand via the API,
        # ahead of a future, separately-reviewed stage that would actually
        # wire a (conservatively bounded) response into live decisions.
        self.pattern_risk_engine = PatternRiskEngine(
            self.analytics_engine, min_sample_size=int(getattr(runtime.trading, "pattern_risk_min_sample_size", 10))
        )
        # Shadow-validation harness: records what pattern_risk_engine WOULD
        # have recommended for every real trade, alongside its actual
        # outcome, so enforcement can be evaluated against evidence before
        # ever being turned on. See engine/pattern_risk_validation.py.
        self.pattern_risk_validation = PatternRiskValidationStore(user_id)
        self.recovery_engine = RecoveryEngine(
            min_shadow_sample=int(getattr(runtime.trading, "recovery_min_shadow_sample", 20)),
            consistency_window=int(getattr(runtime.trading, "recovery_consistency_window", 20)),
            win_rate_baseline=float(getattr(runtime.trading, "recovery_win_rate_baseline", 50.0)),
            score_threshold=float(getattr(runtime.trading, "recovery_score_threshold", 65.0)),
        )

        # Circuit breaker + retry around flaky broker calls (candle fetch,
        # order placement). get_balance is deliberately NOT wrapped here —
        # it already has bespoke disconnect-detection handling in _scan_loop
        # that this would interfere with.
        self.error_recovery = ErrorRecovery()
        self.error_recovery.retry_configs["get_candles"] = RetryConfig(
            max_retries=2,
            circuit_breaker=self.error_recovery.create_circuit_breaker(
                f"get_candles:{user_id}", failure_threshold=6, timeout_seconds=20.0,
            ),
        )
        self.error_recovery.retry_configs["place_order"] = RetryConfig(
            max_retries=1,  # orders are not free to retry blindly — 1 retry only
            circuit_breaker=self.error_recovery.create_circuit_breaker(
                f"place_order:{user_id}", failure_threshold=4, timeout_seconds=30.0,
            ),
        )
        self.error_recovery.retry_configs["get_payout"] = RetryConfig(
            max_retries=1,
            circuit_breaker=self.error_recovery.create_circuit_breaker(
                f"get_payout:{user_id}", failure_threshold=6, timeout_seconds=20.0,
            ),
        )
        # Market Initialization Manager's own history-warm-up calls get an
        # isolated retry/circuit-breaker config, deliberately separate from
        # "get_candles" above — its background traffic can be much
        # higher-volume than live scan traffic (every eligible asset, not
        # just the current top-12 candidates) and must never trip the
        # breaker the live scan loop's own get_candles calls depend on, or
        # vice versa. max_retries=0 is deliberate too: MarketInitManager
        # owns its own spec'd 1s/2s/4s retry schedule; a second, differently
        # -timed retry layer underneath it would just add uncontrolled delay.
        self.error_recovery.retry_configs[MARKET_INIT_SERVICE_NAME] = RetryConfig(
            max_retries=0,
            circuit_breaker=self.error_recovery.create_circuit_breaker(
                f"{MARKET_INIT_SERVICE_NAME}:{user_id}", failure_threshold=6, timeout_seconds=20.0,
            ),
        )

        # Durable signal queue — per-user storage path, survives a backend
        # crash/restart. On startup any leftover entries are logged and
        # cleared rather than blindly re-executed (see start()): a binary
        # options signal computed against pre-crash market state is not
        # safe to fire against current price/time without fresh evaluation.
        self.queue_persistence = QueuePersistence(
            storage_path=str(user_data_dir(user_id) / "signal_queue")
        )
        self.shadow_store = ShadowStore(
            storage_path=str(user_data_dir(user_id) / "shadow_trades.json")
        )

        self._last_candle_at: float = 0.0   # updated on every successful candle fetch
        self._last_scan_recovery_at: float = 0.0  # throttles watchdog scan-stall restarts
        self._asset_funnel: Dict[str, object] = {}  # asset-universe reduction diagnostics
        # SCAN-CYCLE TELEMETRY.
        #
        # "Pipeline supervision: OK" only means the task object is alive, and
        # a Live Pipeline entry is only written when a Signal is actually
        # PROPOSED (start_trace fires after eight gates in
        # _evaluate_asset_signal). So a genuinely quiet market and a wedged
        # scanner looked identical from the dashboard -- which is how a gate
        # that never released went unnoticed for minutes at a time.
        # These make a completed-but-signal-less cycle visible.
        self._data_stale: bool = False      # set/cleared by the watchdog, auto-recovers (no manual resume needed)
        self._last_candle_prune_at: float = 0.0  # throttles candle_store retention cleanup to ~once/day
        self._scan_task: Optional[asyncio.Task] = None
        self._result_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._shadow_task: Optional[asyncio.Task] = None
        self._rejection_reconciliation_task: Optional[asyncio.Task] = None
        self._reconnecting = False
        # BUG FIX (startup race): the watchdog's reconnect trigger only ever
        # checked `_reconnecting` (set solely by _reconnect_with_backoff), not
        # whether the *initial* connect from start()/_connect_provider() was
        # still in flight. PyQuotexProvider.connect() alone can take well over
        # WATCHDOG_INTERVAL (5s) on a normal login -- its own internal retry
        # loop for a delayed "authorized" ack polls up to 5x every 1.5s (see
        # services/market.py) on top of the network round trip and any OTP
        # wait. So on an ordinary, slightly slow login, the watchdog's very
        # first tick would see "not connected, not reconnecting" and fire a
        # *second*, concurrent provider.connect() via _reconnect_with_backoff
        # -- two coroutines rebuilding and reassigning self._client at once.
        # This flag closes that window; the watchdog now checks it too.
        self._connect_in_progress = False
        self._reconnect_backoff = RECONNECT_BASE_DELAY
        self._running = False
        # Guards against the watchdog loop firing an auto-reconnect while
        # switch_provider() is already mid-flight replacing self.provider --
        # without this, a provider swap and a watchdog-triggered reconnect
        # can run concurrently and race to set self.provider.
        self._switching_provider = False
        # Supervises the five core background loops below: restarts any of
        # them automatically (with backoff + full traceback logging) if they
        # ever exit from an exception instead of a deliberate stop(). See
        # engine/task_supervisor.py for why this exists.
        # on_stuck fires when a loop blows its restart budget: flips
        # state.stage -> "stuck" and prompts the manual Restart Pipeline
        # path, which is what the pipeline-fix change set's own inline
        # supervisor did before the two were merged into this one.
        self._supervisor = TaskSupervisor(
            owner_label=f"user:{self.user_id}", on_stuck=self._on_task_stuck,
        )
        self._assets_cache: List = []
        self._assets_ts = 0.0
        self._day = time.strftime("%Y-%m-%d")
        self.health = {
            "started_at": now_ts(),
            "last_scan_at": 0.0,
            "assets_scanned": 0,
            "candles_ok": False,
            "scan_errors": 0,
            "last_error": None,
            "watchdog_last_run_at": 0.0,
        }
        # MINOR CLEANUP: _reconnect_attempts/_next_reconnect_at/_reconnect_base_delay/
        # _reconnect_max_delay used to live here but nothing in the file ever read
        # them -- _reconnect_with_backoff() actually uses _reconnect_backoff (above)
        # together with the module-level RECONNECT_BASE_DELAY/RECONNECT_MAX_DELAY
        # constants. Dead fields, not a behavioral bug; removed rather than left
        # to confuse the next read of this constructor.

        # NOTE: the pipeline-fix change set's parallel `self._task_health`
        # dict lived here. It was removed during the merge -- TaskSupervisor
        # already tracks exactly this (liveness, restart counts, last error,
        # stuck) in SupervisedTaskStats, and pipeline_health() below now
        # reads from there. Keeping both would have meant two sources of
        # truth drifting apart.

        # If the persisted settings say we were in Tournament mode last
        # session, restore the tournament-scoped stores now -- otherwise
        # everything constructed above defaults to the unscoped Live/Demo
        # paths and a restart would silently lose the tournament's
        # isolated history. The actual broker-side account switch still
        # has to happen once connected (see start()/_connect_provider) --
        # this only restores which FILES this instance reads/writes to.
        if runtime.trading.account_mode == "tournament" and runtime.trading.tournament_id:
            self._rebuild_account_scoped_stores(self._account_scope_dir("tournament", runtime.trading.tournament_id))
            self.state.account_mode = "tournament"
            self.state.tournament_id = runtime.trading.tournament_id
        else:
            self.state.account_mode = runtime.trading.account_mode if runtime.trading.account_mode in ("live", "demo") else "demo"

        # Market Initialization Manager — bounded worker pool + per-asset
        # state machine that warms up market history before the scan loop
        # is allowed to evaluate an asset. Constructed last in __init__ so
        # every dependency it takes (provider, indicators, candle_store,
        # error_recovery) already exists. See services/market_init.py.
        self.market_init = MarketInitManager(
            user_id=user_id,
            # A getter, not self.provider: switch_provider() rebinds
            # self.provider, and a captured instance would go stale.
            get_provider=lambda: self.provider,
            indicators=self._indicators,
            candle_store=self.candle_store,
            error_recovery=self.error_recovery,
            get_timeframe=lambda: self.runtime.trading.timeframe,
            get_settings=lambda: self.runtime.market_init,
            get_min_bars=lambda: getattr(self.runtime.trading, "min_candles_for_signal", 200),
        )

    # -- per-user resource scoping ------------------------------------------ #
    def _sessions_dir(self) -> str:
        return str(user_data_dir(self.user_id) / "pyquotex_sessions")

    def _broker_settings(self):
        """Credentials for *this user's* broker connection — ONLY this
        user's own encrypted credentials, saved via /api/broker/credentials.

        This used to fall back to the shared .env QUOTEX_EMAIL/PASSWORD when
        a user hadn't saved their own — meant as a convenience for the old
        single-operator setup, but in a multi-user deployment that meant any
        user who hadn't entered their own Quotex login would silently be
        connected to whoever's account happened to be in .env. Removed:
        every user must explicitly save their own credentials, or they get
        the demo/paper data provider (see _provider_name/build_provider) —
        never another account's real broker connection.
        """
        creds = session_manager.get_credentials(self.user_id)
        if creds and (creds.quotex_email or creds.quotex_ssid):
            # RCA F4: `runtime.trading.is_demo` is the mode the user actually
            # selected (it drives the UI, TradeRecord.is_demo and
            # account-mode switching), so on a restart it must win over
            # whatever `broker_sessions.account_type` happens to say. Before
            # `switch_account_mode` also called `set_account_type`, those two
            # disagreed after any Demo <-> Live switch, and the provider came
            # back up on the WRONG account while everything else said
            # otherwise. Rows written before that fix are reconciled here.
            stored_is_demo = creds.quotex_is_demo
            selected_is_demo = bool(self.runtime.trading.is_demo)
            if stored_is_demo != selected_is_demo:
                log_event(
                    logger, logging.WARNING, "account_mode_reconciled",
                    user_id=self.user_id,
                    stored_account_type="REAL" if not stored_is_demo else "PRACTICE",
                    selected_account_mode=self.runtime.trading.account_mode,
                    reason="broker_sessions.account_type disagreed with the saved "
                           "account mode -- using the saved account mode",
                )
                creds.quotex_is_demo = selected_is_demo
            return creds
        return BrokerCredentials()  # empty -> build_provider falls back to paper/demo data

    def _persist_broker_session(self) -> None:
        """After a successful pyquotex connect, snapshot and encrypt the fresh
        ssid/cookies so a backend restart can reconnect without re-login."""
        getter = getattr(self.provider, "get_session_snapshot", None)
        if not getter:
            return
        ssid, cookies, user_agent = getter()
        if ssid:
            session_manager.save_session_token(self.user_id, ssid, cookies)

    @property
    def live_regime(self) -> Dict[str, str]:
        """Current detected regime per asset, from the most recent scan."""
        return dict(self._live_regime)

    # -- lifecycle ---------------------------------------------------------- #
    async def _recover_open_trades(self) -> None:
        """Re-adopt the trades that were open when the process last died.

        RCA F8. This used to be `_reconcile_open_trades`, and its entire body
        was:

            stale = [t.id for t in self.store.all()
                     if t.status in (TradeStatus.OPEN, TradeStatus.PENDING)]
            self.store.remove_many(stale)

        Deleting them is not a neutral "keep history accurate" choice -- it
        destroys the record of every position that was live across a restart,
        wins and losses alike. Whatever the broker did with those orders never
        reaches the risk counters, the strategy performance tracker, the
        win-rate calibration or the trade log, so the statistics are
        systematically skewed towards whatever happened to survive, and
        `daily_pnl` silently forgets the money. A restart during a losing
        streak also erased the streak, so the drawdown and
        consecutive-loss circuit breakers could be walked around by
        restarting.

        Now: ask the broker. `provider.recover_result()` reads the broker's own
        trade history, which is server-side and survives our restart, so most
        of these can be settled properly. Whatever it can settle is queued in
        `_recovered_results` and the trade is put back into `active_trades`,
        so it flows through the ordinary `_result_loop` settlement path --
        risk, strategy learning, balance, broadcast -- rather than a second,
        divergent bookkeeping path.

        Anything the broker does NOT recognise is kept and still tracked, not
        deleted: the order may genuinely still be running. It is only after
        `UNRECOVERABLE_TRADE_AFTER_SECONDS` that we stop pretending, and even
        then the trade is marked `error` in the store -- visibly unresolved --
        rather than removed.
        """
        stale = [t for t in self.store.all() if t.status in (TradeStatus.OPEN, TradeStatus.PENDING)]
        if not stale:
            return

        now = now_ts()
        recovered = 0
        resumed = 0
        abandoned = 0

        for trade in stale:
            oid = str(trade.id)
            try:
                result = await self.provider.recover_result(oid)
            except Exception:
                logger.debug("[recovery] recover_result(%s) raised", oid, exc_info=True)
                result = None

            if result:
                self._recovered_results[oid] = result
                self.active_trades[oid] = trade
                recovered += 1
                continue

            # TradeRecord stamps entry time in `created_at` -- there is no
            # `opened_at` field, and reading one with a getattr default would
            # silently yield 0 and make EVERY unrecovered trade look ancient.
            age = now - float(getattr(trade, "created_at", 0) or 0)
            if age <= UNRECOVERABLE_TRADE_AFTER_SECONDS:
                # The broker has no record yet. It may still be running, so
                # keep tracking it -- a live websocket can still settle it.
                self.active_trades[oid] = trade
                resumed += 1
            else:
                # Old AND unknown. Stop occupying a concurrency slot, but keep
                # the record so the gap is visible instead of silently erased.
                trade.status = TradeStatus.ERROR
                trade.closed_at = now
                self.store.update(trade)
                abandoned += 1

        if recovered or resumed or abandoned:
            log_event(
                logger, logging.INFO, "open_trades_reconciled",
                user_id=self.user_id,
                found=len(stale), recovered=recovered,
                resumed_tracking=resumed, marked_unresolved=abandoned,
                reason="trades open across a restart were settled from broker "
                       "history or kept for tracking -- none were deleted",
            )
            await hub.broadcast(self.user_id, "notice", {
                "message": (
                    f"Restart recovery: {recovered} trade(s) settled from broker history, "
                    f"{resumed} still being tracked, {abandoned} marked unresolved."
                )
            })

    async def start(self) -> None:
        if self._running:
            return
        await self._recover_open_trades()
        stale_pending = self.queue_persistence.get_pending()
        if stale_pending:
            logger.warning(
                "[queue_persistence] %d signal(s) recovered from a previous crash/restart for "
                "user=%s — discarding rather than re-executing, since market conditions have "
                "moved on since they were generated: %s",
                len(stale_pending), self.user_id,
                [f"{s.asset} {s.direction}" for s in stale_pending[:5]],
            )
            self.queue_persistence.clear_all()
        self._running = True
        # ONE supervision mechanism: TaskSupervisor (engine/task_supervisor.py).
        # The pipeline-fix change set's own inline _run_supervised() was
        # folded INTO that class during the merge (restart budget + "stuck"
        # verdict + on_stuck callback), rather than kept alongside it -- two
        # supervisors wrapping the same five loops would have started two
        # copies of every background task.
        self.state.stage = "connecting"
        _sup = dict(
            base_delay=TASK_LOOP_RESTART_DELAY_SECONDS,
            max_restarts=TASK_LOOP_MAX_RESTARTS,
            restart_window=TASK_LOOP_RESTART_WINDOW_SECONDS,
            # A loop returning while _running is still True is a bug, not a
            # graceful stop -- restart it instead of letting it vanish.
            is_active=lambda: self._running,
        )
        self._scan_task = self._supervisor.supervise("scan", self._scan_loop, **_sup)
        self._result_task = self._supervisor.supervise("result", self._result_loop, **_sup)
        self._watchdog_task = self._supervisor.supervise("watchdog", self._watchdog_loop, **_sup)
        self._shadow_task = self._supervisor.supervise("shadow", self._shadow_loop, **_sup)
        self._rejection_reconciliation_task = self._supervisor.supervise(
            "rejection_reconciliation", self._rejection_reconciliation_loop, **_sup
        )
        self.trade_engine.start()
        self._configure_telegram()
        self.telegram.command_handler = self._handle_command
        await self.validation.start()
        # Connect in the background so startup never blocks (e.g. on an OTP prompt).
        asyncio.create_task(self._connect_provider())
        # MarketInitManager waits for provider.connected itself (Phase 1 of
        # its own state machine) — safe to kick off now rather than after
        # the connect task resolves; it just spends that time polling.
        asyncio.create_task(self.market_init.start(self._enabled_asset_universe))
        await self.broadcast_state()

    async def _recover_stalled_scan(self, stall_age: float) -> None:
        """Restart ONLY the scan loop after real progress has stopped.

        Deliberately narrow. It does not restart the process, does not
        rebuild the Orchestrator, and does not touch the other four
        supervised loops, the Market Init warm-up manager, or any trading
        state -- signals, active trades, risk counters and warm-up asset
        states all survive untouched. The scan loop is stateless between
        cycles by construction, so re-entering it is safe.

        Guarded against repetition: a restart is not attempted again for
        SCAN_PROGRESS_STALL_SECONDS, so a genuinely broken scan loop
        produces one restart every few minutes with a logged reason, not
        a hot restart loop.
        """
        last_attempt = getattr(self, "_last_scan_recovery_at", 0.0)
        if now_ts() - last_attempt < SCAN_PROGRESS_STALL_SECONDS:
            return
        self._last_scan_recovery_at = now_ts()

        logger.error(
            "[watchdog] scan loop has made no progress for %.0fs (budget %.0fs) "
            "while connected -- the task is alive but wedged on an await. "
            "Restarting the scan loop only; all other loops, warm-up state and "
            "trading state are left untouched.",
            stall_age, SCAN_PROGRESS_STALL_SECONDS,
        )
        self.health["scan_stall_recoveries"] = self.health.get("scan_stall_recoveries", 0) + 1
        self.health["last_error"] = f"scan loop stalled {stall_age:.0f}s -- restarted"

        old_task = self._scan_task
        if old_task is not None and not old_task.done():
            old_task.cancel()
            try:
                # Bounded: if the wedged await also swallows cancellation we
                # must not block the watchdog itself waiting for it.
                await asyncio.wait_for(asyncio.shield(old_task), timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.debug("[watchdog] stalled scan task raised on cancel", exc_info=True)

        # Reset the progress marker so the fresh loop is not immediately
        # judged stalled by its own predecessor's timestamp.
        self.health["last_scan_at"] = now_ts()
        self._scan_task = self._supervisor.supervise(
            "scan", self._scan_loop,
            base_delay=TASK_LOOP_RESTART_DELAY_SECONDS,
            max_restarts=TASK_LOOP_MAX_RESTARTS,
            restart_window=TASK_LOOP_RESTART_WINDOW_SECONDS,
            is_active=lambda: self._running,
        )
        try:
            await hub.broadcast(self.user_id, "notice", {
                "message": f"Scan loop was stalled for {stall_age:.0f}s and has been restarted automatically.",
            })
        except Exception:
            logger.debug("[watchdog] failed to broadcast stall-recovery notice", exc_info=True)

    def _on_task_stuck(self, name: str) -> None:
        """Called by TaskSupervisor the first time a loop blows its restart
        budget. Kept sync (the supervisor calls it inline) -- it only flips
        state and schedules the user-facing notice."""
        if name in ("scan", "watchdog"):
            self.state.stage = "stuck"

        async def _notify() -> None:
            try:
                await hub.broadcast(self.user_id, "notice", {
                    "message": f"Pipeline component '{name}' is stuck and stopped auto-recovering — use Restart Pipeline.",
                })
                await self.broadcast_state()
            except Exception:
                logger.debug("[supervisor] failed to broadcast stuck notice for %s", name, exc_info=True)

        try:
            asyncio.create_task(_notify())
        except RuntimeError:
            # No running loop (shutdown); the log line from the supervisor
            # is enough on its own.
            pass

    def pipeline_health(self) -> dict:
        """Machine-readable snapshot for /api/pipeline/health and the
        Restart Pipeline button: per-loop liveness plus an overall `stuck`
        verdict.

        Reads TaskSupervisor's own stats -- the single source of truth for
        loop supervision after the merge. `scan_age_seconds` catches the
        OTHER kind of stuck: not a crashed task (which the supervisor
        already restarts) but a *hung* one -- an await that never raises and
        never returns, so last_scan_at simply stops advancing with nothing
        to catch. Past 60s with no scan heartbeat something is wedged even
        if every task still reports running.

        `watchdog_age_seconds` is the same idea for the watchdog loop, whose
        heartbeat (health["watchdog_last_run_at"]) came from the reliability
        change set -- a running-but-not-ticking watchdog means reconnect and
        stale-data detection are effectively dead even though nothing
        crashed."""
        now = now_ts()
        snap = self._supervisor.snapshot()
        tasks: Dict[str, dict] = {}
        any_stuck = False
        for name, st in snap.items():
            any_stuck = any_stuck or st["stuck"]
            tasks[name] = {
                "alive": st["running"],
                "stuck": st["stuck"],
                "restarts_recent": st["restarts_recent"],
                "restart_count": st["restart_count"],
                "last_error": st["last_error"],
                "last_crash_at": st["last_error_at"],
            }
        last_scan = self.health.get("last_scan_at") or 0.0
        scan_age = (now - last_scan) if last_scan else None
        wd_last = self.health.get("watchdog_last_run_at") or 0.0
        watchdog_age = (now - wd_last) if wd_last else None
        hung = scan_age is not None and scan_age > 60.0 and self.state.connected
        watchdog_hung = watchdog_age is not None and watchdog_age > WATCHDOG_INTERVAL * 5
        stuck = any_stuck or (not self._running) or hung or watchdog_hung
        return {
            "running": self._running,
            "stage": self.state.stage,
            "stuck": stuck,
            "scan_age_seconds": scan_age,
            "watchdog_age_seconds": watchdog_age,
            "tasks": tasks,
        }

    async def _connect_with_budget(self) -> bool:
        """provider.connect() under a timeout that EXCLUDES time spent waiting
        for the human to type the emailed PIN.

        BUG FIX (2026-08): all three connect sites wrapped connect() in
        `asyncio.wait_for(..., 60)` while the OTP callback nested inside it
        waits up to 300s for user input. Quotex's email alone often takes
        longer than 60s to arrive, so wait_for would cancel connect() mid-
        prompt: _otp_callback's `finally` ran, cleared otp_required, and the
        PIN dialog vanished on its own while the user was still reading the
        email. The failed attempt then triggered the reconnect path, which
        started a fresh login, which made Quotex email a NEW code and pop the
        dialog again -- so the code the user finally typed belonged to a login
        attempt that no longer existed and was always rejected. That is the
        "popup disappears and keeps coming back" loop.

        The 60s ceiling is still a real hang backstop; it just stops counting
        while a prompt is on screen. The OTP callback's own 300s timeout
        remains the limit on human input."""
        task = asyncio.ensure_future(self.provider.connect())
        deadline = now_ts() + PROVIDER_CONNECT_TIMEOUT_SECONDS
        while True:
            done, _pending = await asyncio.wait({task}, timeout=1.0)
            if task in done:
                return bool(task.result())
            if self.state.otp_required:
                # A prompt is on screen -- the clock is the user's, not ours.
                deadline = now_ts() + PROVIDER_CONNECT_TIMEOUT_SECONDS
                continue
            if now_ts() >= deadline:
                task.cancel()
                raise asyncio.TimeoutError()

    async def _reapply_account_mode(self) -> None:
        """Re-assert the persisted account mode on the freshly connected
        session. RCA F4.

        Why this exists: `_connect_provider` used to re-apply ONLY tournament
        routing. Demo/Live was assumed to be whatever the provider was built
        with, and the provider is built from `broker_sessions.account_type` --
        a column nothing updated on a mode switch. So after Demo -> Live and a
        restart (or any reconnect), the bot traded the DEMO account while the
        UI, `state.is_demo` and every `TradeRecord.is_demo` reported LIVE.

        Failure handling is asymmetric on purpose. Coming up on DEMO when the
        user asked for LIVE wastes nothing but produces a trade log that lies;
        coming up on LIVE when the user asked for DEMO risks real money. So a
        failed re-apply towards LIVE pauses trading outright instead of
        logging a warning and carrying on.
        """
        mode = (self.runtime.trading.account_mode or "demo").lower()
        tournament_id = self.runtime.trading.tournament_id

        if mode == "tournament" and not tournament_id:
            # Nothing to route into; fall through to plain demo rather than
            # leaving the session on an unknown account.
            mode = "demo"

        want_demo = mode in ("demo", "tournament")
        try:
            switched = await self.provider.switch_account(mode, tournament_id)
        except Exception as exc:
            switched = False
            logger.warning("[account] re-apply of %s on connect raised: %s", mode, exc)

        if switched:
            # Keep the in-memory flags honest even when the provider was built
            # from a stale account_type (rows written before set_account_type
            # existed). The provider's own switch_account() already updates its
            # _is_demo/_tournament_id; this covers providers that do not.
            self.state.is_demo = want_demo
            self.state.account_mode = mode
            self.state.tournament_id = tournament_id if mode == "tournament" else None
            if self.state.is_demo != bool(self.runtime.trading.is_demo):
                self.runtime.trading.is_demo = self.state.is_demo
                self.runtime.save(self.user_id)
            return

        log_event(
            logger, logging.ERROR, "account_mode_reapply_failed",
            user_id=self.user_id, account_mode=mode, tournament_id=tournament_id,
            reason="provider.switch_account() did not confirm the account mode "
                   "after connect -- refusing to trade on an unverified account",
        )
        if want_demo:
            # Wrong direction is harmless-but-wrong: tell the user, keep going.
            await hub.broadcast(self.user_id, "error", {
                "message": f"Could not confirm {mode.upper()} account routing after "
                           f"reconnect -- check the account indicator before trusting P&L.",
            })
        else:
            # LIVE could not be confirmed. Never place real orders on an
            # account we cannot verify.
            self.risk.pause(f"Account mode {mode.upper()} could not be verified after reconnect")
            self._sync_counters()
            await hub.broadcast(self.user_id, "error", {
                "message": "Trading PAUSED: the LIVE account could not be verified after "
                           "reconnect. Reconnect or re-save your Quotex credentials, then resume.",
            })

    async def _connect_provider(self) -> None:
        self.state.stage = "authenticating"
        self._connect_in_progress = True
        ok = False   # bound up-front: the finally below reads it, and a
                     # CancelledError skips both except clauses
        try:
            try:
                ok = await self._connect_with_budget()
            except asyncio.TimeoutError:
                ok = False
                logger.warning(
                    "[connect] provider.connect() exceeded %.0fs — treating as a failed "
                    "attempt so the watchdog's normal reconnect path can take over",
                    PROVIDER_CONNECT_TIMEOUT_SECONDS,
                )
                await hub.broadcast(self.user_id, "error", {"message": "Connect timed out — will retry automatically"})
            except Exception as exc:
                ok = False
                await hub.broadcast(self.user_id, "error", {"message": f"Connect failed: {exc}"})
        finally:
            self._connect_in_progress = False
            # The login attempt has resolved one way or the other -- close any
            # PIN dialog still on screen and say which way it went, instead of
            # letting it vanish silently mid-prompt.
            await self.clear_otp_prompt(
                ok=bool(ok),
                message=None if ok else "PIN rejected or login failed — a new code will be requested",
            )
        self.state.connected = ok
        self.state.provider = self.provider.name
        self.state.tournament_supported = self.provider.supports_tournaments()
        if ok:
            self.state.stage = "restoring"
            try:
                bal, cur = await self.provider.get_balance()
                self.state.balance = bal
                self.state.currency = cur
                self.state.starting_balance = bal
                self.risk.update_equity(bal)
            except Exception:
                pass
            await asyncio.to_thread(self._persist_broker_session)
            # RCA F4: re-apply the FULL persisted account mode on every
            # connect, not just tournament. The old code had a single
            # `if account_mode == "tournament"` branch, so a reconnect or a
            # restart after a Demo <-> Live switch came back up on whatever
            # account the provider happened to be constructed with -- with no
            # log line and no UI signal saying it had changed.
            await self._reapply_account_mode()
            self.state.stage = "running"
        await self.broadcast_state()

    async def _watchdog_loop(self) -> None:
        """Runs for the lifetime of this user's orchestrator. Detects a
        dropped Quotex/broker connection and kicks off backoff-reconnect —
        this is what makes the bot recover on its own instead of sitting
        disconnected until someone opens the app and notices.

        Also detects a *silently frozen* price feed (provider.connected can
        stay True while candle data itself has stopped updating — a
        different failure mode than an outright disconnect) and pauses new
        signal generation until fresh data resumes, auto-clearing on its
        own once it does (no manual intervention needed, unlike the
        risk-limit pauses in risk.py)."""
        while self._running:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                if not self._running:
                    return
                # This loop previously had no try/except at all: any single
                # unexpected exception below (a race with switch_provider()
                # replacing self.provider, a broadcast failure, an attribute
                # access on a mid-reinit provider) permanently ended the
                # ENTIRE watchdog task with nothing supervising it, silently
                # disabling auto-reconnect and stale-data detection for good
                # -- the bot would look "running but inactive" until someone
                # manually restarted the service. TaskSupervisor (see
                # start()) is the outer safety net; this per-iteration guard
                # is the first line, and recovers within the same iteration
                # instead of costing a full loop restart.
                self.health["watchdog_last_run_at"] = now_ts()
                # MERGE NOTE: three guards, not one. `_reconnecting` (base)
                # is set only by _reconnect_with_backoff. `_connect_in_progress`
                # (pipeline-fix) covers the INITIAL connect from
                # start()/_connect_provider(), which can legitimately outlast
                # WATCHDOG_INTERVAL on a slow login -- without it this loop's
                # very first tick fires a second, concurrent provider.connect().
                # `_switching_provider` (reliability-fixes) covers a manual
                # provider swap, where self.provider is briefly the old,
                # disconnected instance. All three must be checked; dropping
                # any one of them reopens a distinct double-connect race.
                if (
                    not self.provider.connected
                    and not self._reconnecting
                    and not self._connect_in_progress
                    and not self._switching_provider
                    and not self.state.otp_required
                ):
                    self.state.stage = "reconnecting"
                    asyncio.create_task(self._reconnect_with_backoff())

                # --- REAL-PROGRESS DETECTION (secondary safety net) ---
                # TaskSupervisor can only observe Task.done(). A loop wedged
                # on an await is not done, has not raised, and has not
                # returned, so it was reported healthy with zero restarts
                # while making no progress at all (proven in
                # backend/prove_root_cause.py, LINK 5). This checks the one
                # thing that cannot be faked: whether the scan loop's own
                # progress marker is still advancing.
                #
                # This is NOT the fix -- the bounded awaits above are. This
                # exists so that any FUTURE unbounded await on the scan path
                # degrades into an observable, recoverable stall instead of
                # a silent one. Every restart it triggers logs a concrete
                # reason and an age; it never fires "just in case".
                if self._running and self.state.connected and not self._data_stale:
                    last_scan = self.health.get("last_scan_at") or 0.0
                    if last_scan > 0:
                        stall_age = now_ts() - last_scan
                        if stall_age > SCAN_PROGRESS_STALL_SECONDS:
                            await self._recover_stalled_scan(stall_age)

                if self.provider.connected and self._last_candle_at > 0:
                    threshold = float(getattr(self.runtime.risk, "stale_data_pause_seconds", 90))
                    age = now_ts() - self._last_candle_at
                    was_stale = self._data_stale
                    self._data_stale = age > threshold
                    if self._data_stale and not was_stale:
                        logger.warning(
                            "[stale-data] No candles received in %.0fs (threshold %.0fs) — "
                            "pausing new signals until data resumes", age, threshold,
                        )
                        await hub.broadcast(self.user_id, "notice", {
                            "message": f"Market data appears frozen ({age:.0f}s since last candle) — signals paused"
                        })
                    elif was_stale and not self._data_stale:
                        logger.info("[stale-data] Candle data resumed after %.0fs — signals re-enabled", age)
                        await hub.broadcast(self.user_id, "notice", {"message": "Market data resumed — signals re-enabled"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[watchdog] iteration failed, continuing: %s", exc)

    async def _reconnect_with_backoff(self) -> None:
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            while self._running and not self.provider.connected:
                delay = self._reconnect_backoff
                # Jitter so many users' bots reconnecting after a shared
                # Quotex-side blip don't all retry on the exact same clock
                # tick (a reconnect storm hitting the login endpoint at
                # once). +/- up to 35% of the base delay, never negative.
                jittered_delay = delay + delay * random.uniform(0.0, 0.35)
                self.state.pause_reason = f"Reconnecting to {self.provider.name} in {int(jittered_delay)}s…"
                await self.broadcast_state()
                await asyncio.sleep(jittered_delay)
                if not self._running:
                    return
                try:
                    ok = await self._connect_with_budget()
                except asyncio.TimeoutError:
                    ok = False
                    self.health["last_error"] = f"connect timed out after {PROVIDER_CONNECT_TIMEOUT_SECONDS:.0f}s"
                    logger.warning("Reconnect attempt timed out after %.0fs", PROVIDER_CONNECT_TIMEOUT_SECONDS)
                except Exception as exc:
                    ok = False
                    self.health["last_error"] = str(exc)
                    logger.warning("Reconnect attempt failed: %s", type(exc).__name__)

                await self.clear_otp_prompt(
                    ok=bool(ok),
                    message=None if ok else "PIN rejected or login failed — a new code will be requested",
                )
                if ok:
                    self.state.connected = True
                    self.state.pause_reason = None
                    self.state.stage = "running"
                    self._reconnect_backoff = RECONNECT_BASE_DELAY
                    try:
                        bal, cur = await self.provider.get_balance()
                        self.state.balance = bal
                        self.state.currency = cur
                    except Exception as exc:
                        logger.warning("Failed to get balance after reconnect: %s", type(exc).__name__)
                    await asyncio.to_thread(self._persist_broker_session)
                    await hub.broadcast(self.user_id, "notice", {"message": f"Reconnected to {self.provider.name}"})
                    await self.broadcast_state()
                    # Re-validate (not fully re-fetch) previously-READY assets
                    # through the bounded worker pool, instead of letting the
                    # scan loop rediscover them cold, all at once, the moment
                    # it resumes — see MarketInitManager.handle_reconnect.
                    asyncio.create_task(self.market_init.handle_reconnect())
                    return
                # Exponential backoff with a ceiling, so a prolonged Quotex
                # outage doesn't hammer the broker's login endpoint.
                self._reconnect_backoff = min(self._reconnect_backoff * 2, RECONNECT_MAX_DELAY)
        finally:
            self._reconnecting = False

    async def stop(self) -> None:
        self._running = False
        self._supervisor.stop()
        # Stop the Market Initialization Manager first so it stops feeding
        # new history work in while the core loops are being torn down.
        # Bounded, like everything else in this teardown: its own stop()
        # cancels + gathers its worker pool, and a hung worker must not be
        # able to hang process shutdown.
        try:
            await asyncio.wait_for(self.market_init.stop(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning(
                "[shutdown] user=%s MarketInitManager.stop() did not finish "
                "within 10s -- abandoning it so shutdown can proceed.", self.user_id,
            )
        except Exception:
            logger.exception("[shutdown] user=%s MarketInitManager.stop() failed", self.user_id)
        tasks = [
            t for t in (
                self._scan_task, self._result_task, self._watchdog_task,
                self._shadow_task, self._rejection_reconciliation_task,
            ) if t
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            # Bounded wait so one stuck loop can't hang shutdown forever --
            # mirrors the systemd unit's own TimeoutStopSec for the process.
            _done, pending = await asyncio.wait(tasks, timeout=10)
            for t in pending:
                logger.warning(
                    "[shutdown] user=%s task %r did not stop within 10s of "
                    "cancellation -- abandoning it so shutdown can proceed.",
                    self.user_id, t.get_name(),
                )
        self.telegram.stop()
        await self.validation.stop()
        await self.trade_engine.stop()
        await self.provider.disconnect()

    def _configure_telegram(self) -> None:
        tg = self.runtime.telegram
        self.telegram.configure(
            enabled=tg.enabled,
            token=tg.bot_token or env_settings.telegram_bot_token,
            chat_id=tg.chat_id or env_settings.telegram_chat_id,
            allow_commands=tg.allow_commands,
        )

    def _provider_name(self) -> str:
        return self.runtime.provider or env_settings.market_provider

    # -- OTP / 2FA coordination -------------------------------------------- #
    async def _otp_callback(self, message: str) -> str:
        """Called by pyquotex when Quotex asks for the emailed PIN. Pauses,
        prompts the UI, and waits for the user to submit the code."""
        loop = asyncio.get_event_loop()
        self._otp_future = loop.create_future()
        self.state.otp_required = True
        self.state.otp_message = "Enter the PIN code Quotex emailed you"
        await hub.broadcast(self.user_id, "otp_required", {"message": self.state.otp_message})
        await self.broadcast_state()
        try:
            code = await asyncio.wait_for(self._otp_future, timeout=300)
        except asyncio.TimeoutError:
            self.state.otp_required = False
            self.state.otp_message = None
            self._otp_future = None
            await hub.broadcast(self.user_id, "otp_cleared", {"reason": "timed out"})
            await self.broadcast_state()
            raise RuntimeError("OTP entry timed out")
        # BUG FIX: this used to clear otp_required in a `finally`, i.e. the
        # instant the code was handed over -- so the dialog vanished before
        # Quotex had verified anything and the user got no feedback either
        # way. Keep it on screen in a verifying state; whoever finishes the
        # login clears it via clear_otp_prompt().
        self._otp_future = None
        self.state.otp_verifying = True
        self.state.otp_message = "Verifying PIN with Quotex…"
        await self.broadcast_state()
        return str(code)

    async def clear_otp_prompt(self, *, ok: bool, message: Optional[str] = None) -> None:
        """Close the PIN dialog once the login attempt has actually resolved."""
        if not (self.state.otp_required or self.state.otp_verifying):
            return
        self.state.otp_required = False
        self.state.otp_verifying = False
        self.state.otp_message = None
        await hub.broadcast(self.user_id, "otp_cleared",
                            {"ok": ok, "message": message or ("Connected" if ok else "PIN rejected or login failed")})
        await self.broadcast_state()

    def submit_otp(self, code: str) -> Tuple[bool, str]:
        """Returns (accepted, reason). Previously returned a bare bool, so a
        code submitted after the prompt had already been cancelled silently
        went nowhere with no explanation in the UI."""
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if not digits:
            return False, "PIN must be numeric"
        if not self._otp_future or self._otp_future.done():
            return False, ("No PIN prompt is waiting — the login attempt was cancelled or already "
                           "finished. Reconnect to request a new code.")
        self._otp_future.set_result(digits)
        return True, "submitted"

    def apply_settings(self, runtime: RuntimeSettings) -> None:
        prev_provider = self._provider_name()
        prev_demo = self.runtime.trading.is_demo
        self.runtime = runtime
        self.risk.settings = runtime.risk
        self.correlation_guard.threshold = runtime.risk.correlation_threshold
        self.validation.correlation.threshold = runtime.risk.correlation_threshold
        # Keep the Stage C adaptive engine's config in sync with live
        # Settings changes (e.g. flipping adaptive_mode_enabled on/off) --
        # this replaces the cfg object only; existing rolling history in
        # self.market_context is preserved, not wiped, since the underlying
        # market data is still valid regardless of the flag.
        self.market_context.cfg = self._adaptive_cfg()
        self.analytics_engine.min_sample_size = int(getattr(runtime.trading, "analytics_min_sample_size", 10))
        self.pattern_risk_engine.min_sample_size = int(getattr(runtime.trading, "pattern_risk_min_sample_size", 10))
        self.recovery_engine.min_shadow_sample = int(getattr(runtime.trading, "recovery_min_shadow_sample", 20))
        self.recovery_engine.consistency_window = int(getattr(runtime.trading, "recovery_consistency_window", 20))
        self.recovery_engine.win_rate_baseline = float(getattr(runtime.trading, "recovery_win_rate_baseline", 50.0))
        self.recovery_engine.score_threshold = float(getattr(runtime.trading, "recovery_score_threshold", 65.0))
        # Same for the Stage A revalidation-gate thresholds -- previously
        # these were only applied once at construction time (a gap: a user
        # changing reval_* settings wouldn't see it take effect until a
        # restart). Refreshing the instance attributes directly here avoids
        # rebuilding the whole SignalQualityValidator (which would also
        # reset its metrics_log history for no reason).
        for key, value in self._reval_kwargs().items():
            setattr(self.signal_validator, key, value)
        # Phase 6 / Admin Settings UI: strategy performance manager, confidence
        # calibrator, and drift detector tunables -- all settable live, no restart.
        self.strategy_manager.min_sample = int(getattr(runtime.trading, "strategy_min_sample", 30))
        self.strategy_manager.trial_size = int(getattr(runtime.trading, "strategy_trial_size", 20))
        self.strategy_manager.rolling_window = max(
            int(getattr(runtime.trading, "strategy_rolling_window", 50)), self.strategy_manager.min_sample + 1,
        )
        self.strategy_manager.typical_payout_pct = float(getattr(runtime.trading, "strategy_typical_payout_pct", 80.0))
        self.strategy_manager.base_cooldown = float(getattr(runtime.trading, "strategy_base_cooldown_seconds", 21600.0))
        self.strategy_manager.max_cooldown = float(getattr(runtime.trading, "strategy_max_cooldown_seconds", 259200.0))
        self.strategy_manager.wilson_confidence_level = float(getattr(runtime.trading, "adaptive_wilson_confidence_level", 0.95))
        self.strategy_manager.ewma_alpha = getattr(runtime.trading, "adaptive_learning_ewma_alpha", None)
        self.strategy_manager.drift_weight = getattr(runtime.trading, "adaptive_learning_drift_weight", None)
        self.calibrator.min_samples_total = int(getattr(runtime.trading, "calibration_min_samples_total", 30))
        self.calibrator.refresh_every_n_trades = int(getattr(runtime.trading, "calibration_refresh_every_n_trades", 10))
        self.calibrator.refresh_max_age = float(getattr(runtime.trading, "calibration_refresh_max_age_seconds", 3600.0))
        self.drift_detector.delta = float(getattr(runtime.trading, "drift_delta", 0.005))
        self.drift_detector.threshold = float(getattr(runtime.trading, "drift_threshold", 25.0))
        self.drift_detector.min_samples = int(getattr(runtime.trading, "drift_min_samples", 20))
        self.drift_detector.cooldown_seconds = float(getattr(runtime.trading, "drift_cooldown_seconds", 3600.0))
        self.confidence_model_store.model.min_samples_to_train = int(getattr(runtime.trading, "confidence_model_min_training_samples", 200))
        self.confidence_model_store.model.min_samples_to_trust = int(getattr(runtime.trading, "confidence_model_prediction_threshold", 300))
        self.calibrator.holdout_validation_enabled = bool(getattr(runtime.trading, "calibration_holdout_validation_enabled", True))
        self.calibrator.rollback_on_worse_enabled = bool(getattr(runtime.trading, "calibration_rollback_on_worse_enabled", True))
        self.state.mode = runtime.trading.mode
        self.state.is_demo = runtime.trading.is_demo
        self._configure_telegram()
        runtime.save(self.user_id)
        # If the provider (or demo/live for a real broker) changed, switch live.
        new_provider = self._provider_name()
        if new_provider != prev_provider or (new_provider != "simulated" and runtime.trading.is_demo != prev_demo):
            asyncio.create_task(self.switch_provider())

    async def switch_provider(self) -> None:
        """Tear down the current provider and connect the newly-selected one."""
        # Set before anything else, cleared in `finally`: while this is
        # True the watchdog loop will not also try to reconnect the
        # provider it sees mid-swap (self.provider is briefly the old,
        # disconnected instance here, then a fresh one further down) --
        # without this guard, the two could run concurrently and race to
        # set self.provider, or double-connect it in the pyquotex/Binance
        # case where connect() doesn't guard against being called twice.
        self._switching_provider = True
        try:
            name = self._provider_name()
            self.state.connected = False
            self.state.pause_reason = f"Switching to {name}…"
            await self.broadcast_state()
            try:
                await self.provider.disconnect()
            except Exception:
                pass
            # Reflect demo/live choice for real brokers (on this user's own
            # credentials object — never mutates the shared env fallback).
            broker_settings = self._broker_settings()
            broker_settings.quotex_is_demo = self.runtime.trading.is_demo
            self.provider = build_provider(name, broker_settings, self._otp_callback, self._sessions_dir())
            self.state.tournament_supported = self.provider.supports_tournaments()
            if name == "pyquotex" and self.provider.name != "pyquotex":
                logger.warning(
                    "[account] Provider set to 'pyquotex' but no credentials/SSID configured -- "
                    "silently using '%s' instead. Tournament mode will be unavailable until "
                    "credentials are added in Settings.", self.provider.name,
                )
            self.active_trades.clear()
            self.signals.clear()
            self._indicators.reset()
            try:
                # MERGE FIX: the pipeline-fix change set bounded the OTHER two
                # provider.connect() call sites (_connect_provider and
                # _reconnect_with_backoff) but not this one, because
                # reliability-fixes had rewritten this method in parallel and
                # the two edits never met. Left unbounded it is the worst of
                # the three: `_switching_provider` is True for the whole
                # duration, which deliberately blocks the watchdog from
                # reconnecting -- so a connect() that hangs instead of raising
                # wedges provider recovery permanently, with no exception and
                # no crash. Same ceiling and same failure handling as the
                # other two sites.
                ok = await self._connect_with_budget()
            except asyncio.TimeoutError:
                ok = False
                logger.warning(
                    "[account] provider.connect() during switch exceeded %.0fs — treating as "
                    "a failed switch so the watchdog's reconnect path can take over",
                    PROVIDER_CONNECT_TIMEOUT_SECONDS,
                )
                await hub.broadcast(self.user_id, "error", {"message": "Connect timed out — will retry automatically"})
            except Exception as exc:
                ok = False
                await hub.broadcast(self.user_id, "error", {"message": f"Connect failed: {exc}"})
            self.state.provider = self.provider.name
            self.state.connected = ok
            self.state.pause_reason = None if ok else f"{name} connection failed"
            if ok:
                try:
                    bal, cur = await self.provider.get_balance()
                    self.state.balance = bal
                    self.state.currency = cur
                    self.state.starting_balance = bal
                except Exception:
                    pass
                await asyncio.to_thread(self._persist_broker_session)
            await hub.broadcast(self.user_id, "notice", {"message": f"Provider: {self.provider.name} ({'connected' if ok else 'failed'})"})
            await self.broadcast_state()
        finally:
            self._switching_provider = False

    # -- helpers ------------------------------------------------------------ #
    def _maybe_rollover_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self.risk.reset_day()

    async def _eligible_assets(self) -> List:
        """Every open, whitelisted-if-set, min-payout-qualifying asset —
        the full universe MarketInitManager keeps warm in the background.
        `_candidate_assets` further narrows this to the scan loop's
        top-12-by-payout AND to assets MarketInitManager reports READY."""
        now = time.time()
        if now - self._assets_ts > 10 or not self._assets_cache:
            self._assets_cache = await self.provider.get_assets()
            self._assets_ts = now
        wl = self.runtime.trading.asset_whitelist
        min_payout = self.runtime.risk.min_payout
        # The gates themselves are UNCHANGED -- is_open and min_payout are
        # both intentional and neither is bypassed. What is added is a
        # record of which gate removed what, so "0 candidates" stops being
        # a single indistinguishable outcome.
        universe = self._assets_cache
        open_assets = [a for a in universe if a.is_open]
        payout_ok = [a for a in universe if a.payout >= min_payout]
        assets = [a for a in open_assets if a.payout >= min_payout]
        after_open_payout = len(assets)
        if wl:
            assets = [a for a in assets if a.symbol in wl]
        # BUG FIX: this used to REPLACE the dict, wiping the readiness/cap keys
        # written by _candidate_assets(). /api/status calls _eligible_assets()
        # directly, so the Candidate selection line reported "-0 not READY" even
        # with 26 assets stuck in warm-up. Merge instead.
        self._asset_funnel = {
            **getattr(self, "_asset_funnel", {}),
            "total_instruments": len(universe),
            "open_count": len(open_assets),
            "closed_count": len(universe) - len(open_assets),
            "payout_qualified_count": len(payout_ok),
            "open_and_payout_qualified_count": after_open_payout,
            "whitelist_count": len(assets) if wl else after_open_payout,
            "whitelist_active": bool(wl),
            "min_payout": min_payout,
        }
        # Mirror the funnel onto the provider snapshot so a single
        # get_asset_feed_status() call answers the whole question.
        feed = getattr(self.provider, "_asset_feed", None)
        if feed is not None:
            feed.payout_qualified_count = len(payout_ok)
            feed.open_and_payout_qualified_count = after_open_payout
            feed.whitelist_count = len(assets) if wl else after_open_payout
            feed.min_payout_used = min_payout
        return assets

    async def _enabled_asset_universe(self) -> List[str]:
        """Callback MarketInitManager polls to discover/refresh which
        symbols it should be warming up. Deliberately the *same*
        eligibility filter the scanner itself uses (via _eligible_assets)
        rather than a second, separately-maintained list."""
        return [a.symbol for a in await self._eligible_assets()]

    async def _candidate_assets(self) -> List:
        assets = await self._eligible_assets()
        # Scanner must never evaluate an asset before MarketInitManager
        # reports it READY (historical candles complete, validated, and
        # indicators warm) — this is the actual fix for the original bug.
        # A LOADING/RETRYING/FAILED asset is skipped this cycle, not
        # blocked on; it simply isn't in the list _scan_once() gathers over.
        eligible_before_ready = len(assets)
        assets = [a for a in assets if self.market_init.is_ready(a.symbol)]
        ready_count = len(assets)
        # Exclusion breakdown, so "52 eligible -> 9 selected" reads as three
        # explicit steps rather than 43 assets disappearing.
        excluded_by_readiness = eligible_before_ready - ready_count
        # Was a hardcoded 12: the single biggest throughput lever in the whole
        # pipeline was not configurable, so READY, payout-qualified assets were
        # silently dropped every cycle by a constant rather than by any gate.
        cap = max(1, int(getattr(self.runtime.trading, "max_scan_candidates", 12)))
        excluded_by_cap = max(0, ready_count - cap)
        assets.sort(key=lambda a: a.payout, reverse=True)
        candidates = assets[:cap]
        self._asset_funnel = {
            **getattr(self, "_asset_funnel", {}),
            "eligible_before_ready_gate": eligible_before_ready,
            "ready_count": ready_count,
            "candidates_excluded_by_readiness": excluded_by_readiness,
            "candidates_excluded_by_cap": excluded_by_cap,
            "final_candidate_count": len(candidates),
        }
        if eligible_before_ready and not ready_count:
            # A genuinely distinct state: the universe is fine, warm-up is
            # not. Previously indistinguishable from "no open assets".
            log_event(
                logger, logging.WARNING, "ZERO_CANDIDATES_WARMUP_PENDING",
                eligible=eligible_before_ready, ready=0,
                reason="eligible assets exist but none are READY",
            )
        return candidates

    #: A cycle older than this means the SCANNER ITSELF stopped, not that
    #: the market is quiet. Comfortably above SCAN_CYCLE_BUDGET_SECONDS so a
    #: legitimately slow cycle is never mislabelled.
    SCAN_STALL_THRESHOLD_SECONDS = 180.0

    def scan_state(self) -> str:
        """SCANNING | HEALTHY_NO_SIGNAL | DATA_STALE | STALLED | STOPPING.

        Deliberately keyed off CYCLE PROGRESS, never off signal output. A
        quiet market and a dead scanner produced identical dashboards
        before this existed, which is how a permanently-stuck gate stayed
        invisible for minutes at a time.
        """
        if not getattr(self, "_running", False):
            return "STOPPING"
        h = self.health
        if not h.get("scan_cycle_count"):
            return "STARTING"
        last_done = h.get("last_scan_completed_at") or 0.0
        if not last_done or (now_ts() - last_done) > self.SCAN_STALL_THRESHOLD_SECONDS:
            return "STALLED"
        if getattr(self, "_data_stale", False):
            # Cycles are still advancing; signals are correctly suppressed.
            return "DATA_STALE"
        if h.get("signals_this_cycle"):
            return "SCANNING"
        return "HEALTHY_NO_SIGNAL"

    def scan_telemetry(self) -> dict:
        """Per-cycle scanner state. Answers "is the scanner advancing?"
        without needing a Signal to have been produced."""
        h = self.health
        now = now_ts()
        last_attempt = h.get("last_scan_attempt_at") or 0.0
        last_done = h.get("last_scan_completed_at") or 0.0
        return {
            "scan_cycle_count": h.get("scan_cycle_count", 0),
            "last_scan_attempt_at": last_attempt or None,
            "last_scan_completed_at": last_done or None,
            "last_scan_age_seconds": round(now - last_done, 1) if last_done else None,
            "last_candidate_selection_at": h.get("last_candidate_selection_at"),
            "candidates_available": h.get("candidates_available", 0),
            "candidates_selected": h.get("candidates_selected", 0),
            "assets_evaluated": h.get("assets_evaluated", 0),
            "evaluation_failures": h.get("evaluation_failures", 0),
            "evaluation_timeouts": h.get("evaluation_timeouts", 0),
            "current_scan_duration": h.get("current_scan_duration"),
            "consecutive_scan_failures": h.get("consecutive_scan_failures", 0),
            "signals_proposed_last_cycle": h.get("signals_proposed_last_cycle", 0),
            "signals_generated": h.get("signals_generated", 0),
            "signals_this_cycle": h.get("signals_this_cycle", 0),
            "pipeline_snapshot_update_count": h.get("pipeline_snapshot_update_count", 0),
            "pipeline_snapshots_this_cycle": h.get("pipeline_snapshots_this_cycle", 0),
            "candidates_excluded_by_readiness": self._asset_funnel.get("candidates_excluded_by_readiness", 0),
            # This counts the 63-open -> 35-eligible step, NOT the
            # eligible -> scanned step it was being rendered against, which is
            # why the panel read "35 eligible -> 7 scanned (-28 below min
            # payout)". Reported separately so each number lands on the step
            # it actually belongs to.
            "candidates_excluded_by_payout": max(
                0,
                self._asset_funnel.get("open_count", 0)
                - self._asset_funnel.get("open_and_payout_qualified_count", 0),
            ),
            "instruments_excluded_by_payout": max(
                0,
                self._asset_funnel.get("open_count", 0)
                - self._asset_funnel.get("open_and_payout_qualified_count", 0),
            ),
            "candidates_excluded_by_cap": self._asset_funnel.get("candidates_excluded_by_cap", 0),
            "next_scan_due_at": (
                (h.get("last_scan_completed_at") or 0) + SCAN_INTERVAL_BACKSTOP
                if h.get("last_scan_completed_at") else None
            ),
            "stall_threshold_seconds": self.SCAN_STALL_THRESHOLD_SECONDS,
            "data_stale": bool(getattr(self, "_data_stale", False)),
            "state": self.scan_state(),
            "scan_gate_blocks": h.get("scan_gate_blocks") or {},
            "last_pipeline_snapshot_at": h.get("last_pipeline_snapshot_at"),
        }

    def asset_funnel(self) -> dict:
        """Where the asset universe is being reduced, stage by stage.

        Answers "why is the scanner seeing 0 assets?" without needing to
        reproduce the run: total -> open -> payout-qualified -> whitelisted
        -> READY -> top-12.
        """
        funnel = dict(getattr(self, "_asset_funnel", {}))
        feed = self.provider.get_asset_feed_status() if hasattr(
            self.provider, "get_asset_feed_status") else {}
        return {**feed, **funnel}

    # -- main loops --------------------------------------------------------- #
    def _cleanup_stale_signals(self) -> None:
        """Purge terminal signals older than SIGNAL_RETENTION_SECONDS from
        self.signals. Without this, every signal ever generated (including
        ones that never fired) stays in memory for the life of the process."""
        now = now_ts()
        stale_ids = [
            sid for sid, s in self.signals.items()
            if s.status in _TERMINAL_SIGNAL_STATUSES and (now - s.created_at) > SIGNAL_RETENTION_SECONDS
        ]
        for sid in stale_ids:
            del self.signals[sid]
            self.latency.drop(sid)
        if stale_ids:
            logger.debug("[cleanup] Purged %d stale signal(s), %d remaining", len(stale_ids), len(self.signals))
        self.queue_persistence.cleanup_stale(max_age_seconds=3600)

        now_wall = time.time()
        if now_wall - self._last_candle_prune_at >= CANDLE_PRUNE_INTERVAL_SECONDS:
            self._last_candle_prune_at = now_wall
            try:
                deleted = self.candle_store.prune_older_than(CANDLE_RETENTION_DAYS)
                if deleted:
                    logger.info("[cleanup] Pruned %d candle row(s) older than %.0f days", deleted, CANDLE_RETENTION_DAYS)
            except Exception as exc:
                logger.warning("[cleanup] candle_store prune failed: %s", exc)

    async def _scan_loop(self) -> None:
        await asyncio.sleep(1.0)
        while self._running:
            try:
                self._maybe_rollover_day()
                self._cleanup_stale_signals()
                if self.state.connected:
                    try:
                        # BUG FIX: bare `await` here had no timeout, so a
                        # broker call that hangs instead of raising (TCP
                        # connection gone half-open with no keepalive to
                        # notice, etc.) would freeze this entire loop forever
                        # with no exception for the except-block below to
                        # catch — a "stuck" pipeline that isn't even a crash.
                        # wait_for turns that into a plain TimeoutError, which
                        # the existing except Exception below already treats
                        # exactly like any other get_balance failure.
                        bal, cur = await asyncio.wait_for(
                            self.provider.get_balance(), timeout=PROVIDER_CALL_TIMEOUT_SECONDS
                        )
                    except Exception as exc:
                        # This almost always means the underlying broker
                        # connection/websocket actually died. Without this,
                        # provider._connected and state.connected both stay
                        # True forever, so _watchdog_loop's "if not
                        # provider.connected" check never fires and
                        # auto-reconnect never kicks in — the bot just sits
                        # here re-hitting the same error every cycle,
                        # looking "stopped" with no visible recovery.
                        self.health["last_error"] = f"get_balance failed: {type(exc).__name__}"
                        logger.warning("get_balance failed, marking disconnected: %s", exc)
                        self.state.connected = False
                        self.state.pause_reason = "Connection lost — reconnecting…"
                        try:
                            await self.provider.disconnect()
                        except Exception as disc_exc:
                            logger.debug("Disconnect during error cleanup failed: %s", type(disc_exc).__name__)
                        raise
                    self.state.balance = bal
                    self.state.currency = cur
                    self.risk.update_equity(bal)
                    if self.runtime.trading.mode != "off":
                        await self._scan_once()
                self.health["last_scan_at"] = now_ts()
                await self.broadcast_state()
                await hub.broadcast(self.user_id, "latency", self.latency.snapshot())
            except Exception as exc:  # never let the loop die
                self.health["scan_errors"] += 1
                self.health["last_error"] = str(exc)
                try:
                    # This broadcast used to be unguarded -- if it ever
                    # raised (a dead websocket, an unexpected serialization
                    # edge case), that new exception would escape this
                    # except block entirely and end the whole scan task:
                    # exactly the "silently stops, no fatal error shown"
                    # failure mode. Recording the error above already
                    # happened either way; the notification is best-effort.
                    await hub.broadcast(self.user_id, "error", {"message": str(exc)})
                except Exception:
                    logger.debug("[scan] failed to broadcast error notice", exc_info=True)
            # Event-driven wakeup: react the moment the provider signals fresh
            # data (websocket tick / kline push) instead of always waiting out
            # the full interval. The timeout is a safety-net backstop only —
            # covers balance/state refresh even if no new candle data arrives
            # (asset closed, thin market, provider hiccup, etc.).
            self.provider.new_data_event.clear()
            try:
                await asyncio.wait_for(self.provider.new_data_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

    async def _probe_data_resumed(self) -> None:
        """Bounded liveness probe used only while `_data_stale` is set.

        Fetches candles for a single candidate purely to answer "has the
        feed come back?". On success it refreshes `_last_candle_at`, which
        is what lets _watchdog_loop clear `_data_stale` on its next tick.

        Deliberately does NOT evaluate, score, validate, or emit anything:
        the stale gate's whole purpose is that no signal comes from frozen
        data, and that property is preserved. Failures are swallowed on
        purpose -- a failing probe simply means the feed is still down, and
        the next cycle tries again.
        """
        try:
            candidates = await self._candidate_assets()
            if not candidates:
                return
            tf = self.runtime.trading.timeframe
            candles = await asyncio.wait_for(
                self.provider.get_candles(candidates[0].symbol, tf, 5),
                timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
            )
            if candles:
                self._last_candle_at = now_ts()
                log_event(
                    logger, logging.INFO, "DATA_RESUMED_PROBE_OK",
                    asset=candidates[0].symbol, candles=len(candles),
                    note="stale flag will clear on the next watchdog tick",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[stale-data] resume probe failed: %s", type(exc).__name__)

    async def _scan_once(self) -> None:
        if self._data_stale:
            # ROOT-CAUSE FIX (scanner stops advancing after a few minutes).
            #
            # This gate is CORRECT and is kept: no signal may be generated
            # from frozen market data. The bug was that it was a one-way
            # latch. `_data_stale` is cleared by _watchdog_loop only when
            # `_last_candle_at` becomes recent again (orchestrator.py:787),
            # and the ONLY writer of `_last_candle_at` is line 2280 inside
            # _evaluate_asset_signal -- which this return skips. So once the
            # flag went true, the only code able to clear it was the code
            # the flag was blocking, and it stayed true forever.
            #
            # Everything else kept looking healthy, which is exactly what
            # production reported: _scan_loop still assigns
            # health["last_scan_at"] after this returns, so the progress
            # watchdog saw fresh progress and never restarted anything
            # ("no restarts"); `assets_scanned` kept its last value so the
            # UI still said "Scanning · 9 assets"; and /api/status's own
            # get_candles call is a separate path, so "Market data feed:
            # last 18s ago" stayed fresh while _last_candle_at did not.
            # Only scan_cycle_count and the Live Pipeline snapshots froze.
            #
            # The fix is a bounded probe on ONE candidate: enough to learn
            # whether data has resumed and refresh `_last_candle_at`, and
            # deliberately not enough to evaluate or emit a signal. The
            # gate's safety property is unchanged -- this path still
            # returns before any evaluation, ensemble, validation or risk
            # check runs.
            await self._probe_data_resumed()
            self.health["last_scan_attempt_at"] = now_ts()
            self.health["scan_cycle_count"] = self.health.get("scan_cycle_count", 0) + 1
            # BUG FIX: the data-stale path advanced the cycle count but never
            # last_scan_completed_at, so after 180s of correctly-suppressed
            # signals the panel reported STALLED ("the scanner is not
            # advancing") when the scanner was advancing exactly as designed.
            # DATA_STALE is the accurate state and scan_state() renders it.
            self.health["last_scan_completed_at"] = now_ts()
            self.health["scan_gate_blocks"] = {"data_stale": True}
            log_event(
                logger, logging.INFO, "SCAN_EARLY_RETURN",
                reason="data_stale",
                candle_age_s=round(now_ts() - self._last_candle_at, 1)
                if self._last_candle_at else None,
                probing="one candidate, no evaluation",
            )
            return
        tf = self.runtime.trading.timeframe
        htf = HTF_MAP.get(tf, tf)
        hhtf = HHTF_MAP.get(tf, tf)
        use_htf = self.runtime.trading.multi_timeframe_confirmation and htf != tf
        best: Optional[Signal] = None
        candidates = await self._candidate_assets()
        self.health["assets_scanned"] = len(candidates)

        started_trials = self.strategy_manager.maybe_start_trials()
        for name in started_trials:
            await hub.broadcast(self.user_id, "notice", {
                "message": f"'{name}' is back on trial after its cooldown — voting again on probation."
            })
        active_strategy_list = self.strategy_manager.active_strategies(self.runtime.trading.enabled_strategies)

        if self.calibrator.needs_refresh(len(self.store.all())):
            self.calibrator.refresh(self.store.all())
        if self.regime_tracker.needs_refresh(len(self.store.all())):
            self.regime_tracker.refresh(self.store.all())
        if getattr(self.runtime.trading, "confidence_model_enabled", False):
            all_trades = self.store.all()
            retrain_interval = int(getattr(self.runtime.trading, "confidence_model_retrain_interval", 50))
            if len(all_trades) - self._last_confidence_model_retrain_count >= retrain_interval:
                try:
                    decisions = self.decision_store.read_recent(limit=5000)
                    decisions_by_id = {d.id: d for d in decisions}
                    X, y = build_training_set(decisions_by_id, all_trades)
                    fitted = self.confidence_model_store.model.fit(
                        X, y,
                        l2=float(getattr(self.runtime.trading, "confidence_model_l2_regularization", 1.0)),
                        lr=float(getattr(self.runtime.trading, "confidence_model_learning_rate", 0.3)),
                        epochs=int(getattr(self.runtime.trading, "confidence_model_max_iterations", 1000)),
                    )
                    if fitted:
                        self.confidence_model_store.save()
                    self._last_confidence_model_retrain_count = len(all_trades)
                except Exception:
                    logger.debug("[confidence_model] retrain failed", exc_info=True)

        # ROOT-CAUSE FIX (isolation). This was a plain
        # `asyncio.gather(..., return_exceptions=True)`, which waits for
        # EVERY candidate. return_exceptions only covers coroutines that
        # RAISE -- a coroutine that hangs is not an exception, so one
        # wedged provider call left this gather pending forever and the
        # scan loop never reached its next iteration (proven in
        # backend/prove_root_cause.py, LINK 4).
        #
        # The per-call timeouts added elsewhere in this fix are the real
        # remedy; this is the structural backstop that guarantees the
        # CYCLE stays live regardless of what any single asset does.
        # Results from assets that finished are used exactly as before --
        # asset selection, ordering, payout and confidence logic are
        # untouched; a straggler is simply absent from this cycle, the
        # same as an asset whose fetch failed has always been.
        cycle_started = now_ts()
        self.health["last_scan_attempt_at"] = cycle_started
        self.health["scan_cycle_count"] = self.health.get("scan_cycle_count", 0) + 1
        self.health["candidates_available"] = self._asset_funnel.get("eligible_before_ready_gate", 0)
        self.health["candidates_selected"] = len(candidates)
        self.health["last_candidate_selection_at"] = cycle_started
        self.health["scan_gate_blocks"] = {}
        self.health["pipeline_snapshots_this_cycle"] = 0
        self.health["signals_this_cycle"] = 0
        eval_tasks = [
            asyncio.ensure_future(
                self._evaluate_asset_signal(asset, tf, htf, use_htf, active_strategy_list, hhtf=hhtf)
            )
            for asset in candidates
        ]
        eval_results = []
        if not eval_tasks:
            # BUG FIX: last_scan_completed_at was only written inside the
            # `if eval_tasks:` branch below, so a cycle with zero READY
            # candidates never recorded a completion and scan_state() latched
            # to STALLED after 180s -- a healthy loop reported as a dead one.
            self.health["assets_evaluated"] = 0
            self.health["evaluation_failures"] = 0
            self.health["evaluation_timeouts"] = 0
            self.health["current_scan_duration"] = round(now_ts() - cycle_started, 2)
            self.health["last_scan_completed_at"] = now_ts()
            self.health["scan_gate_blocks"] = {"no_ready_candidates": 1}
            log_event(
                logger, logging.INFO, "SCAN_CYCLE_NO_CANDIDATES",
                cycle=self.health["scan_cycle_count"],
                eligible=self._asset_funnel.get("eligible_before_ready_gate", 0),
                ready=self._asset_funnel.get("ready_count", 0),
                reason="no asset passed the READY gate this cycle",
            )
        if eval_tasks:
            _done, pending = await asyncio.wait(eval_tasks, timeout=SCAN_CYCLE_BUDGET_SECONDS)
            if pending:
                stalled = [
                    candidates[i].symbol for i, t in enumerate(eval_tasks) if t in pending
                ]
                logger.warning(
                    "[scan] %d/%d asset evaluation(s) exceeded the %.0fs cycle budget and "
                    "were cancelled for this cycle: %s -- the cycle continues with the "
                    "%d that completed.",
                    len(pending), len(eval_tasks), SCAN_CYCLE_BUDGET_SECONDS,
                    ", ".join(stalled[:8]) + (" ..." if len(stalled) > 8 else ""),
                    len(eval_tasks) - len(pending),
                )
                self.health["scan_stalled_assets"] = len(pending)
                for t in pending:
                    t.cancel()
                # Drain the cancellations so no orphan task is left behind.
                await asyncio.gather(*pending, return_exceptions=True)
            else:
                self.health["scan_stalled_assets"] = 0
            # Preserve the original ordering rather than asyncio.wait()'s
            # set ordering, so cycle-to-cycle behaviour stays deterministic.
            failures = 0
            for t in eval_tasks:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None:
                    failures += 1
                eval_results.append(exc if exc is not None else t.result())
            self.health["assets_evaluated"] = len(eval_results)
            self.health["evaluation_failures"] = failures
            self.health["evaluation_timeouts"] = self.health.get("scan_stalled_assets", 0)
            self.health["current_scan_duration"] = round(now_ts() - cycle_started, 2)
            self.health["last_scan_completed_at"] = now_ts()
            self.health["consecutive_scan_failures"] = (
                0 if failures < len(eval_tasks) else
                self.health.get("consecutive_scan_failures", 0) + 1
            )
            signals_proposed = sum(
                1 for r in eval_results
                if isinstance(r, tuple) and len(r) > 1 and r[1] is not None
            )
            self.health["signals_proposed_last_cycle"] = signals_proposed
            if not signals_proposed:
                # The single most useful line for telling a quiet market
                # apart from a wedged one: which gate consumed the cycle.
                log_event(
                    logger, logging.INFO, "SCAN_CYCLE_NO_SIGNAL",
                    cycle=self.health["scan_cycle_count"],
                    candidates=len(candidates), evaluated=len(eval_results),
                    failures=failures,
                    duration_s=self.health["current_scan_duration"],
                    gate_blocks=self.health.get("scan_gate_blocks") or {},
                )
        winning_latency_key: Optional[str] = None
        for item in eval_results:
            if isinstance(item, Exception) or item is None:
                continue
            self.health["candles_ok"] = True
            symbol, sig, regime, latency_key = item
            self._live_regime[symbol] = regime
            if sig is not None and (best is None or sig.confidence > best.confidence):
                # A new leader dethrones the old one — drop the old leader's
                # temp latency record (it lost) and keep this one's.
                if winning_latency_key is not None:
                    self.latency.drop(winning_latency_key)
                best = sig
                winning_latency_key = latency_key
            elif latency_key is not None:
                self.latency.drop(latency_key)
        if best is None:
            return
        if winning_latency_key is not None:
            self.latency.rekey(winning_latency_key, best.id)

        now = now_ts()
        # Was tf*1.2 (min 30s) here vs tf*2.0*regime at the execution gate --
        # the de-dupe could retire a signal the executor still considered live.
        ttl_seconds = signal_ttl_seconds(best.timeframe, best.regime)

        # De-dupe: skip if we already have a live/pending signal for this asset.
        for s in self.signals.values():
            if s.asset != best.asset or s.status not in ("new", "approved"):
                continue
            if now - s.created_at > ttl_seconds:
                s.status = "expired"
                continue
            self.latency.drop(best.id)
            return

        self.signals[best.id] = best
        # Attach the concrete plan BEFORE broadcasting, so the card the user
        # sees already answers "when, how much, and will it actually fire?"
        try:
            best.trade_plan = self.build_trade_plan(best)
        except Exception:
            logger.exception("[trade-plan] could not build a plan for %s", best.id)
        await hub.broadcast(self.user_id, "signal", best.model_dump())
        if self.runtime.telegram.send_signals:
            await self.telegram.send(self._fmt_signal(best))

        gate = self.risk.can_trade(now_ts(), len(self.active_trades),
                                   max_concurrent=self.runtime.risk.max_concurrent_trades,
                                   balance=self.state.balance)
        if self.runtime.trading.mode == "auto":
            if gate.allowed:
                td = self.runtime.trading
                if getattr(td, "candle_boundary_execution", False):
                    phase_info = _candle_phase(best.timeframe)
                    wait_secs = phase_info["wait_secs"]
                    max_wait = float(getattr(td, "candle_boundary_max_wait", 30))

                    if wait_secs <= max_wait:
                        if wait_secs > 0.1:
                            logger.info(
                                "[boundary] %s %s conf=%d phase=%s pct=%.0f%% — waiting %.1fs for candle open",
                                best.asset, best.timeframe, best.confidence,
                                phase_info["phase"], phase_info["pct_done"] * 100, wait_secs,
                            )
                            await hub.broadcast(self.user_id, "notice", {
                                "message": (
                                    f"Boundary wait {best.asset} {best.direction.value.upper()} "
                                    f"conf={best.confidence}% — {wait_secs:.1f}s to next candle…"
                                )
                            })
                            await asyncio.sleep(wait_secs)

                        sig_now = self.signals.get(best.id)
                        if sig_now is None or sig_now.status not in ("new", "approved"):
                            logger.info("[boundary] Signal %s replaced/cancelled — skipping", best.id)
                            return

                        if getattr(td, "signal_revalidation", True):
                            revalid = await self._revalidate_signal(sig_now, htf, hhtf, use_htf)
                            if revalid is None:
                                logger.warning("[revalidation] %s fetch failed — using original signal", best.id)
                            elif not revalid:
                                sig_now.status = "expired"
                                logger.info("[revalidation] Signal %s invalidated at candle open — skipping", best.id)
                                await hub.broadcast(self.user_id, "notice", {
                                    "message": (
                                        f"Signal {best.asset} {best.direction.value.upper()} "
                                        f"invalidated at candle open — skipped"
                                    )
                                })
                                return
                            else:
                                logger.info("[revalidation] Signal %s confirmed at candle open conf=%d", best.id, sig_now.confidence)

                        # Was tf*2.5 -- a third, more lenient formula, so a
                        # signal could survive this check and then be expired
                        # moments later by the execution gate's tf*2.0.
                        ttl_check = signal_ttl_seconds(best.timeframe, best.regime)
                        if now_ts() - sig_now.created_at > ttl_check:
                            sig_now.status = "expired"
                            log_event(logger, logging.INFO, "signal_expired",
                                      signal_id=best.id, asset=best.asset, timeframe=best.timeframe,
                                      age_seconds=round(now_ts() - sig_now.created_at, 1), ttl_seconds=ttl_check,
                                      reason="ttl_exceeded_at_candle_boundary")
                            return
                        gate = self.risk.can_trade(
                            now_ts(), len(self.active_trades),
                            max_concurrent=self.runtime.risk.max_concurrent_trades,
                            balance=self.state.balance,
                        )
                        if not gate.allowed:
                            self.state.paused = self.risk.paused
                            self.state.pause_reason = self.risk.pause_reason or gate.reason
                            return
                    else:
                        logger.debug(
                            "[boundary] TF=%s wait=%.1fs > max=%ds — executing immediately",
                            best.timeframe, wait_secs, max_wait,
                        )
                # BUG FIX: this used to `await` the order all the way down to
                # the broker. A pyquotex call that hangs instead of raising
                # therefore froze the whole scan cycle -- health["last_scan_at"]
                # stopped advancing while the task object stayed alive, so the
                # supervisor reported every loop healthy and the panel showed
                # STALLED with no restarts. The return value was never used
                # here; TradeEngine still serializes and de-dups execution in
                # its own single-consumer task, so ordering is unchanged.
                self.trade_engine.submit_background(best.id)
            else:
                self.state.paused = self.risk.paused
                self.state.pause_reason = self.risk.pause_reason or gate.reason

    def _on_order_unreconciled(self, signal_id: str) -> None:
        """An order did not return within the engine's timeout, so whether it
        reached the broker is UNKNOWN. Pause trading rather than continuing to
        fire orders past a call whose outcome nobody knows, and make the state
        visible instead of leaving it in the logs."""
        self.health["last_error"] = f"order {signal_id} unreconciled"
        if not getattr(self.runtime.risk, "block_trading_on_unknown_order", True):
            # Operator has turned the block off. Record it loudly and keep
            # going -- the order still shows in System Status, it just does
            # not stop anything.
            logger.warning(
                "[orchestrator] order %s outcome UNKNOWN. Trading NOT paused "
                "(risk.block_trading_on_unknown_order is off). Reconcile this position "
                "at the broker yourself.", signal_id,
            )
            return
        self.risk.pause(f"Order {signal_id} outcome UNKNOWN -- check the account before resuming")
        self.state.paused = True
        self.state.pause_reason = self.risk.pause_reason
        logger.error(
            "[orchestrator] trading paused: order %s has an unknown outcome. It was NOT retried. "
            "Verify the position at the broker, then clear it from System Status.",
            signal_id,
        )

    def build_trade_plan(self, sig: Signal) -> TradePlan:
        """Resolve a signal into the concrete trade auto-trade would place:
        entry time, expiry time, stake, and whether it will actually fire.

        Every number here comes from the SAME functions the executor calls --
        _candle_phase() for entry timing, RiskManager.stake_for() for size,
        signal_ttl_seconds() for the entry window. Nothing is re-derived or
        approximated, so the panel and the bot cannot disagree.

        Added because the panel showed a signal with no way to tell WHEN it
        would enter, for HOW MUCH, or -- when nothing happened -- WHY not.
        The answer lived only in the logs."""
        td = self.runtime.trading
        now = now_ts()

        waits = False
        phase = None
        entry_at = now
        if getattr(td, "candle_boundary_execution", False):
            info = _candle_phase(sig.timeframe)
            phase = info["phase"]
            max_wait = float(getattr(td, "candle_boundary_max_wait", 30))
            if 0.1 < info["wait_secs"] <= max_wait:
                waits = True
                entry_at = now + info["wait_secs"]

        stake_mult, _ = self._adaptive_risk_multipliers(sig.asset, getattr(sig, "timeframe", None))
        stake = self.risk.stake_for(self.state.balance, confidence=sig.confidence,
                                    adaptive_multiplier=stake_mult)
        payout = float(sig.payout or 0.0)

        gate = self.risk.can_trade(now, len(self.active_trades),
                                   max_concurrent=self.runtime.risk.max_concurrent_trades,
                                   balance=self.state.balance)
        blocked = None
        if td.mode != "auto":
            blocked = f"Mode is '{td.mode}' — auto-trade only fires in Auto mode"
        elif not gate.allowed:
            blocked = gate.reason
        elif self.trade_engine.blocked:
            blocked = ("Execution is blocked: a previous order's outcome is unknown and must be "
                       "checked against the broker")
        elif getattr(td, "shadow_mode_enabled", False):
            blocked = "Shadow mode — the trade is simulated, no real order is placed"

        return TradePlan(
            direction=sig.direction.value,
            asset=sig.asset,
            entry_at=entry_at,
            # Same instant as entry_at. A separate field so the manual Enter
            # Trade button and the auto-executor are structurally reading the
            # ONE schedule this method computed, rather than each deriving
            # their own idea of "now" from a plan that may have been fetched
            # moments apart.
            scheduled_entry_at=entry_at,
            entry_in_seconds=round(max(0.0, entry_at - now), 1),
            expiry_at=entry_at + float(sig.duration),
            expiry_basis="scheduled",  # this method never has a broker-confirmed timestamp
            duration_seconds=int(sig.duration),
            entry_price_reference=float(sig.price),
            stake=stake,
            stake_is_estimate=True,
            payout_pct=payout,
            potential_profit=round(stake * payout / 100.0, 2),
            potential_loss=stake,
            waits_for_candle_open=waits,
            candle_phase=phase,
            valid_until=sig.expires_at,
            will_auto_execute=blocked is None,
            blocked_reason=blocked,
        )

    async def _revalidate_signal(
        self,
        sig: Signal,
        htf: str,
        hhtf: str,
        use_htf: bool,
    ) -> Optional[bool]:
        """Re-run a full strategy+MTF evaluation at the candle boundary on
        freshly fetched candle data.

        Returns True (still confirmed), False (invalidated by fresh data), or
        None (couldn't complete evaluation — caller falls through and uses the
        original signal rather than blocking on a provider hiccup).
        """
        tf = sig.timeframe
        threshold = self.runtime.risk.confidence_threshold
        try:
            candles = await asyncio.wait_for(
                self.provider.get_candles(sig.asset, tf, 120),
                timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("[revalidation] get_candles failed for %s/%s: %s", sig.asset, tf, exc)
            return None

        edf, warm = self._indicators.get_enriched(f"{sig.asset}|{tf}", candles)
        if not warm or edf is None:
            return None
        edf_eval = edf.iloc[:-1] if len(edf) > 1 else edf

        mtf_mode = getattr(self.runtime.trading, "mtf_mode", "soft")
        htf_bias = None
        mtf_ctx = None
        if use_htf:
            try:
                htf_candles = await asyncio.wait_for(
                    self.provider.get_candles(sig.asset, htf, 120),
                    timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
                )
                htf_edf, htf_warm = self._indicators.get_enriched(f"{sig.asset}|{htf}", htf_candles)
                htf_eval = (htf_edf.iloc[:-1] if htf_warm and len(htf_edf) > 1 else htf_edf) if htf_warm else None
            except Exception:
                htf_eval = None

            if mtf_mode == "hard":
                hhtf_eval = None
                if hhtf and hhtf != htf:
                    try:
                        hhtf_candles = await asyncio.wait_for(
                            self.provider.get_candles(sig.asset, hhtf, 60),
                            timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
                        )
                        hhtf_edf, hhtf_warm = self._indicators.get_enriched(f"{sig.asset}|{hhtf}", hhtf_candles)
                        hhtf_eval = (hhtf_edf.iloc[:-1] if hhtf_warm and len(hhtf_edf) > 1 else hhtf_edf) if hhtf_warm else None
                    except Exception:
                        hhtf_eval = None
                mtf_ctx = strategies.build_mtf_context(htf_eval, hhtf_eval)
            else:
                htf_bias, _ = strategies.htf_trend_bias(htf_eval) if htf_eval is not None else (None, 0.0)

        active_strats = self.strategy_manager.active_strategies(
            self.runtime.trading.enabled_strategies, regime=getattr(sig, "regime", None),
        )
        per_asset_strats = self.health_manager.filter_strategies(sig.asset, tf, active_strats)
        if not per_asset_strats:
            return None

        try:
            res = strategies.evaluate(edf_eval, per_asset_strats, pre_enriched=True,
                                       htf_bias=htf_bias, mtf=mtf_ctx,
                                       is_otc=getattr(sig, "is_otc", False),
                                       strategy_weights=self._dynamic_strategy_weights(per_asset_strats),
                                       **self._evaluate_gate_kwargs(asset=sig.asset, timeframe=tf, observe=False))
        except Exception as exc:
            logger.warning("[revalidation] evaluate() error for %s: %s", sig.asset, exc)
            return None

        if res.direction is None:
            logger.info("[revalidation] %s: fresh eval returned no direction (%s)", sig.id, res.reasons[:1])
            return False
        if res.direction != sig.direction:
            logger.info("[revalidation] %s: direction flipped %s→%s at candle open",
                         sig.id, sig.direction.value, res.direction.value)
            return False

        contributing = [n for n, d in res.votes.items() if d == res.direction.value]
        regime_adj = self.regime_tracker.blended_adjustment(contributing, res.regime)
        adjusted = max(0, min(100, res.confidence + regime_adj))
        fresh_conf = self.calibrator.calibrate(adjusted, regime=res.regime) if getattr(self.runtime.trading, "calibration_enabled", True) else adjusted

        sig.confidence = fresh_conf
        sig.raw_confidence = adjusted
        sig.regime = res.regime

        if fresh_conf < threshold - 5:
            logger.info("[revalidation] %s: fresh conf=%d < threshold=%d-5 — invalidated",
                        sig.id, fresh_conf, threshold)
            return False
        return True

    async def _collect_market_features(
        self,
        asset_symbol: str,
        tf: str,
        edf,
        direction_value: Optional[str] = None,
    ) -> tuple[MarketFeatures, SentimentAdjustment]:
        """Build the realtime feature bundle and the bounded sentiment nudge.

        Deliberately total: every branch returns a usable bundle, because this
        runs inside the scan loop and a sentiment/tick failure must never
        prevent a signal that OHLC alone would have produced. When sentiment is
        disabled (the default) no provider call is even made and the adjustment
        is exactly 0.0, so the signal path is unchanged from before this layer
        existed.
        """
        md = getattr(self.runtime, "market_data", None)
        report = QualityReport()
        features = MarketFeatures(asset=asset_symbol, timeframe=tf, quality=report)

        try:
            if edf is not None and len(edf) > 0:
                last = edf.iloc[-1]
                features.candle_ts = float(last.get("timestamp", 0.0) or 0.0)
                features.price = float(last.get("close", 0.0) or 0.0)
                features.indicators = edf
            else:
                report.add("insufficient_history")
        except Exception:
            report.add("malformed_ohlc")

        period = float(TIMEFRAMES.get(tf, 60) or 60)

        # ── tick activity ───────────────────────────────────────────────────
        tick_enabled = bool(getattr(md, "tick_features_enabled", True))
        if tick_enabled:
            try:
                ticks = await self.provider.get_realtime_ticks(asset_symbol)
            except Exception:
                ticks = []
            try:
                features.tick = self._realtime.observe_ticks(
                    asset_symbol, tf, ticks, period_seconds=period, report=report,
                )
                last_ts = self._realtime.last_tick_ts(asset_symbol, tf)
                max_age = float(getattr(md, "price_max_age_seconds", 90.0))
                if last_ts and (time.time() - last_ts) > max_age:
                    report.add("stale_price")
            except Exception:
                report.add("malformed_tick")

        # ── sentiment (optional, bounded) ───────────────────────────────────
        sent_enabled = bool(getattr(md, "sentiment_enabled", False))
        max_age = float(getattr(md, "sentiment_max_age_seconds", 120.0))
        if sent_enabled:
            try:
                payload = await self.provider.get_realtime_sentiment(asset_symbol)
            except Exception:
                payload = None
            try:
                features.sentiment = self._realtime.update_sentiment(
                    asset_symbol, tf, payload, max_age_seconds=max_age,
                )
            except Exception:
                pass
            if not features.sentiment.available:
                report.add("missing_sentiment")
            elif features.sentiment.stale:
                report.add("stale_sentiment")
        else:
            features.sentiment = self._realtime.sentiment(
                asset_symbol, tf, max_age_seconds=max_age,
            )

        try:
            adjustment = sentiment_confidence_adjustment(
                features.sentiment,
                direction=direction_value,
                enabled=sent_enabled,
                max_age_seconds=max_age,
                min_strength=float(getattr(md, "sentiment_min_strength", 0.10)),
                max_adjustment=float(getattr(md, "sentiment_max_confidence_adjustment", 4.0)),
                disagreement_penalty=(
                    None if getattr(md, "sentiment_disagreement_penalty", None) is None
                    else float(md.sentiment_disagreement_penalty)
                ),
            )
        except Exception:
            adjustment = SentimentAdjustment(0.0, False, "error")
        return features, adjustment

    @staticmethod
    def _market_features_log_fields(
        features: Optional["MarketFeatures"],
        sentiment_adj: Optional[SentimentAdjustment],
        base_confidence: Optional[float] = None,
        final_confidence: Optional[float] = None,
        effective_threshold: Optional[float] = None,
    ) -> dict:
        """Structured observability fields for the realtime feature layer.

        Carries asset / timeframe / candle ts / price / tick_activity /
        sentiment buy-sell-bias-stale / data_quality / base_confidence /
        sentiment_adjustment / final_confidence / effective_threshold so a
        signal can be audited end to end from one log line.

        Credentials, cookies, session paths and tokens are never included: the
        field list is explicit and built only from feature values.
        """
        out: dict = {}
        if features is not None:
            try:
                out.update(features.flat())
            except Exception:
                pass
        if sentiment_adj is not None:
            try:
                out.update(sentiment_adj.as_dict())
            except Exception:
                pass
        if base_confidence is not None:
            out["base_confidence"] = base_confidence
        if final_confidence is not None:
            out["final_confidence"] = final_confidence
        if effective_threshold is not None:
            out["effective_threshold"] = effective_threshold
        return out

    def _effective_confidence_threshold(self, payout_pct: float) -> float:
        """Breakeven win-rate for a given payout, plus a real-edge margin,
        floored at the user's flat confidence_threshold. e.g. 80% payout
        needs ~55.6% wins just to break even (1 / (1 + payout_fraction)) —
        low-payout assets need a higher bar before firing. Shared by both
        signal generation (_evaluate_asset_signal) and the fresh re-check
        right before order placement (_do_execute_signal, P2).

        Also widens the bar (P5) when the pipeline's own eval_start->
        signal_generated latency is measurably degraded — previously
        LatencyTracker computed real p95/avg/max per hop and broadcast it to
        the UI, but nothing in the trading path actually read it: a network
        hiccup or broker slowdown left the bot trading exactly as before,
        including "signal age" timing-decay checks computed against an
        assumption of normal latency that was no longer true. This is a
        soft widen (a few extra confidence points required), not a hard
        pause, so a genuinely strong signal can still clear it."""
        threshold = self.runtime.risk.confidence_threshold
        if getattr(self.runtime.risk, "payout_aware_threshold", True) and payout_pct and payout_pct > 0:
            breakeven_pct = 100.0 / (1.0 + payout_pct / 100.0)
            margin = float(getattr(self.runtime.risk, "payout_edge_margin", 8))
            threshold = max(threshold, breakeven_pct + margin)

        if getattr(self.runtime.risk, "latency_aware_threshold", True):
            hop = self.latency.snapshot().get("eval_start->signal_generated")
            ceiling = float(getattr(self.runtime.risk, "latency_p95_ceiling_ms", 1500.0))
            if hop and hop.get("samples", 0) >= 10 and hop.get("p95_ms", 0.0) > ceiling:
                widen = int(getattr(self.runtime.risk, "latency_threshold_widen", 5))
                threshold += widen
        return threshold

    def _reval_kwargs(self) -> Dict[str, float]:
        """Pulls the revalidation-gate thresholds from this user's
        TradingSettings so signal_validator behavior is tunable per-user
        instead of fixed constants inside signal_validation.py. Falls back
        to SignalQualityValidator's own defaults (identical values) if a
        field is missing (e.g. an older config loaded before these existed).
        Called once at Orchestrator construction time -- self.runtime must
        already be set (it is, at line ~105, well before this is used)."""
        t = self.runtime.trading
        return {
            "body_ratio_weak": float(getattr(t, "reval_body_ratio_weak", 0.15)),
            "body_ratio_strong": float(getattr(t, "reval_body_ratio_strong", 0.75)),
            "confluence_high": int(getattr(t, "reval_confluence_high", 4)),
            "confluence_medium": int(getattr(t, "reval_confluence_medium", 2)),
            "regime_adx_floor": float(getattr(t, "reval_regime_adx_floor", 15.0)),
            "veto_age_ms": float(getattr(t, "reval_veto_age_ms", 5000.0)),
            "veto_adjustment": float(getattr(t, "reval_veto_adjustment", -20.0)),
            "fresh_signal_ms": float(getattr(t, "reval_fresh_signal_ms", 1000.0)),
            "call_setup_rsi_high": float(getattr(t, "reval_call_setup_rsi_high", 85.0)),
            "call_setup_rsi_low": float(getattr(t, "reval_call_setup_rsi_low", 25.0)),
            "put_setup_rsi_high": float(getattr(t, "reval_put_setup_rsi_high", 75.0)),
            "put_setup_rsi_low": float(getattr(t, "reval_put_setup_rsi_low", 15.0)),
            "confluence_call_rsi_low": float(getattr(t, "reval_confluence_call_rsi_low", 45.0)),
            "confluence_call_rsi_high": float(getattr(t, "reval_confluence_call_rsi_high", 65.0)),
            "confluence_put_rsi_low": float(getattr(t, "reval_confluence_put_rsi_low", 35.0)),
            "confluence_put_rsi_high": float(getattr(t, "reval_confluence_put_rsi_high", 55.0)),
            "timing_fresh_ms": float(getattr(t, "reval_timing_fresh_ms", 500.0)),
            "timing_decay_ms": float(getattr(t, "reval_timing_decay_ms", 2000.0)),
            "adjustment_clip_min": float(getattr(t, "reval_adjustment_clip_min", -25.0)),
            "adjustment_clip_max": float(getattr(t, "reval_adjustment_clip_max", 15.0)),
        }

    def _adaptive_cfg(self) -> AdaptiveThresholdConfig:
        """Builds the Stage C AdaptiveThresholdConfig from this user's
        TradingSettings. Called at construction and again from
        apply_settings() so toggling adaptive_mode_enabled (or any other
        adaptive_* field) via the Settings API takes effect immediately,
        without a backend restart -- consistent with how every other
        TradingSettings field in this file already behaves."""
        t = self.runtime.trading
        return AdaptiveThresholdConfig(
            enabled=bool(getattr(t, "adaptive_mode_enabled", False)),
            rolling_window=int(getattr(t, "adaptive_rolling_window", 200)),
            min_samples=int(getattr(t, "adaptive_min_samples", 30)),
            ema_alpha=float(getattr(t, "adaptive_ema_alpha", 0.15)),
            max_drift_per_update=float(getattr(t, "adaptive_max_drift_pct", 0.05)),
            use_zscore=bool(getattr(t, "adaptive_use_zscore", False)),
            percentile=float(getattr(t, "adaptive_percentile", 0.2)),
            update_interval_s=float(getattr(t, "adaptive_update_interval_s", 0.0)),
            rsi_high_percentile=float(getattr(t, "adaptive_rsi_high_percentile", 0.90)),
            rsi_low_percentile=float(getattr(t, "adaptive_rsi_low_percentile", 0.10)),
            vol_low_percentile=float(getattr(t, "adaptive_vol_low_percentile", 0.20)),
            vol_high_percentile=float(getattr(t, "adaptive_vol_high_percentile", 0.80)),
            sr_atr_multiplier=float(getattr(t, "adaptive_sr_atr_multiplier", 1.0)),
            confluence_edge_low=float(getattr(t, "adaptive_confluence_edge_low", 0.55)),
            confluence_edge_high=float(getattr(t, "adaptive_confluence_edge_high", 0.70)),
            rsi_enabled=bool(getattr(t, "adaptive_rsi_enabled", True)),
            adx_enabled=bool(getattr(t, "adaptive_adx_enabled", True)),
            atr_enabled=bool(getattr(t, "adaptive_atr_enabled", True)),
            volume_enabled=bool(getattr(t, "adaptive_volume_enabled", True)),
            sr_enabled=bool(getattr(t, "adaptive_sr_enabled", True)),
            confluence_enabled=bool(getattr(t, "adaptive_confluence_enabled", True)),
            signal_validation_enabled=bool(getattr(t, "adaptive_signal_validation_enabled", True)),
            precision_gate_enabled=bool(getattr(t, "adaptive_precision_gate_enabled", True)),
            risk_enabled=bool(getattr(t, "adaptive_risk_enabled", True)),
            precision_agreeing_low=int(getattr(t, "adaptive_precision_agreeing_low", 2)),
            precision_agreeing_high=int(getattr(t, "adaptive_precision_agreeing_high", 4)),
            risk_stake_multiplier_low=float(getattr(t, "adaptive_risk_stake_multiplier_low", 0.85)),
            risk_stake_multiplier_high=float(getattr(t, "adaptive_risk_stake_multiplier_high", 1.05)),
            risk_cooldown_multiplier_low=float(getattr(t, "adaptive_risk_cooldown_multiplier_low", 1.0)),
            risk_cooldown_multiplier_high=float(getattr(t, "adaptive_risk_cooldown_multiplier_high", 1.5)),
        )

    def _build_feature_snapshot(
        self, sig: Signal, current_df, contributing: List[str], latency_breakdown: Optional[Dict[str, float]],
    ) -> Optional[TradeFeatureSnapshot]:
        """Module 1 (Loss Pattern Intelligence Engine, Trade Feature Store):
        captures a snapshot of market + adaptive-system state at execution
        time, purely from data already computed elsewhere this same
        execution pass -- no new indicator computation, no new market_context
        observation (this only READS what strategies.evaluate()'s dead-market
        gate already observed this scan). Called for EVERY executed trade
        regardless of eventual win/loss -- the outcome isn't known yet at
        this point, so there's no way for this to only capture one side.

        Never raises: any failure here must not be able to block a trade
        from executing, so this returns whatever partial snapshot it could
        build (or None) rather than propagating an exception.
        """
        try:
            dt = datetime.fromtimestamp(sig.created_at, tz=timezone.utc)
            weekday = dt.weekday()
            hour = dt.hour
            session = ta.session_name(hour)

            row = current_df.iloc[-1] if current_df is not None and len(current_df) else None
            adx_at_entry = float(row["adx"]) if row is not None and "adx" in row and row["adx"] == row["adx"] else None
            atr_pct_at_entry = (
                ta.atr_pct(float(row["atr"]), float(row["close"]))
                if row is not None and "atr" in row and "close" in row and row["atr"] == row["atr"]
                else None
            )
            rsi_at_entry = float(row["rsi"]) if row is not None and "rsi" in row and row["rsi"] == row["rsi"] else None
            volume_ratio_at_entry = (
                float(row["vol_strength"]) if row is not None and "vol_strength" in row and row["vol_strength"] == row["vol_strength"] else None
            )

            volatility_percentile = None
            adaptive_state: Dict[str, float] = {}
            mc = self.market_context
            if getattr(mc.cfg, "enabled", False):
                if atr_pct_at_entry is not None:
                    volatility_percentile = mc.percentile_rank(sig.asset, sig.timeframe, "atr_pct", atr_pct_at_entry)
                gate_kwargs = self._evaluate_gate_kwargs(asset=sig.asset, timeframe=sig.timeframe, observe=False)
                for key in ("dead_market_adx_threshold", "dead_market_atr_pct_floor", "dead_market_rsi_high", "dead_market_rsi_low"):
                    if key in gate_kwargs:
                        adaptive_state[key] = float(gate_kwargs[key])
                if getattr(self.runtime.trading, "precision_mode_enabled", False):
                    adaptive_state["precision_min_agreeing"] = float(getattr(self.runtime.trading, "precision_min_agreeing", 3))
                stake_mult, cooldown_mult = self._adaptive_risk_multipliers(sig.asset, sig.timeframe)
                adaptive_state["risk_stake_multiplier"] = float(stake_mult)
                adaptive_state["risk_cooldown_multiplier"] = float(cooldown_mult)

            execution_latency_ms = None
            if latency_breakdown:
                total = sum(v for v in latency_breakdown.values() if v is not None)
                execution_latency_ms = round(total, 2) if total else None

            return TradeFeatureSnapshot(
                weekday=weekday,
                hour=hour,
                session=session,
                is_otc=sig.is_otc,
                adx_at_entry=adx_at_entry,
                atr_pct_at_entry=atr_pct_at_entry,
                rsi_at_entry=rsi_at_entry,
                volatility_percentile=volatility_percentile,
                volume_ratio_at_entry=volume_ratio_at_entry,
                confluence_count=len(contributing),
                quality_score=float(sig.raw_confidence) if sig.raw_confidence is not None else None,
                adaptive_state=adaptive_state or None,
                execution_latency_ms=execution_latency_ms,
            )
        except Exception:
            logger.warning("[feature-snapshot] failed to build snapshot for %s -- trade will have features=None", sig.asset, exc_info=True)
            return None

    def _adaptive_risk_multipliers(self, asset: str, timeframe: str) -> Tuple[float, float]:
        """Returns (stake_multiplier, cooldown_multiplier) for Adaptive Risk.
        Both default to 1.0 -- an exact no-op, reproducing legacy behavior
        bit-for-bit -- unless Adaptive Mode AND the Adaptive Risk toggle are
        both on, AND there's enough history for this asset+timeframe's own
        ADX percentile rank (cold-start also falls back to (1.0, 1.0)).

        Reuses the "adx" history strategies.evaluate() already observes --
        read-only, no new observe() call. A stronger-than-usual trend for
        this asset gets a modest stake up-size and the base cooldown; a
        weaker one gets a modest stake trim and an extended cooldown. Both
        multipliers are tightly bounded (configured low/high pairs, default
        0.85-1.05 for stake and 1.0-1.5 for cooldown) so this can only nudge
        risk, never override the hard safety circuit breakers (daily loss
        limit, max consecutive losses, drawdown breaker), which are
        deliberately NOT made adaptive and stay fixed regardless of this
        toggle.
        """
        t = self.runtime.trading
        if not (bool(getattr(t, "adaptive_mode_enabled", False)) and bool(getattr(t, "adaptive_risk_enabled", True))):
            return 1.0, 1.0
        mc = self.market_context
        if not getattr(mc.cfg, "enabled", False) or not getattr(mc.cfg, "risk_enabled", True):
            return 1.0, 1.0
        last_adx = mc.last_value(asset, timeframe, "adx")
        if last_adx is None:
            return 1.0, 1.0
        rank = mc.percentile_rank(asset, timeframe, "adx", last_adx)
        if rank is None:
            return 1.0, 1.0
        frac = rank / 100.0  # 0 = weakest trend seen for this asset, 1 = strongest
        stake_low = float(getattr(t, "adaptive_risk_stake_multiplier_low", 0.85))
        stake_high = float(getattr(t, "adaptive_risk_stake_multiplier_high", 1.05))
        cooldown_low = float(getattr(t, "adaptive_risk_cooldown_multiplier_low", 1.0))
        cooldown_high = float(getattr(t, "adaptive_risk_cooldown_multiplier_high", 1.5))
        stake_mult = stake_low + frac * (stake_high - stake_low)
        cooldown_mult = cooldown_high - frac * (cooldown_high - cooldown_low)
        return stake_mult, cooldown_mult

    def _evaluate_gate_kwargs(self, asset: Optional[str] = None, timeframe: Optional[str] = None, observe: bool = True) -> Dict[str, float]:
        """Pulls the dead-market / confluence gate thresholds from this
        user's TradingSettings so they're tunable per-user instead of fixed
        constants inside strategies.evaluate(). Falls back to the same
        defaults evaluate() itself uses if a field is missing (e.g. an
        older config loaded before these were added)."""
        t = self.runtime.trading
        return {
            "dead_market_atr_pct_floor": float(getattr(t, "dead_market_atr_pct_floor", 0.03)),
            "dead_market_adx_threshold": float(getattr(t, "dead_market_adx_threshold", 14.0)),
            "dead_market_bb_ratio": float(getattr(t, "dead_market_bb_ratio", 0.55)),
            "dead_market_bb_window": int(getattr(t, "dead_market_bb_window", 20)),
            "dead_market_rsi_adx_ceiling": float(getattr(t, "dead_market_rsi_adx_ceiling", 20.0)),
            "dead_market_rsi_high": float(getattr(t, "dead_market_rsi_high", 85.0)),
            "dead_market_rsi_low": float(getattr(t, "dead_market_rsi_low", 15.0)),
            "confluence_min_confirmations": int(getattr(t, "confluence_min_confirmations", 2)),
            "confluence_min_directional_edge": float(getattr(t, "confluence_min_directional_edge", 0.60)),
            "regime_trend_adx": float(getattr(t, "regime_trend_adx", 25.0)),
            "regime_range_adx": float(getattr(t, "regime_range_adx", 18.0)),
            # Stage C: only passed through when adaptive mode is actually on
            # AND we have an asset/timeframe to key it by -- evaluate()
            # treats a None market_context exactly like Stage A behavior.
            "market_context": self.market_context if (
                asset and timeframe and getattr(self.market_context.cfg, "enabled", False)
            ) else None,
            "context_asset": asset,
            "context_timeframe": timeframe,
            "market_context_observe": observe,
            "vol_confirmation_low": float(getattr(t, "vol_confirmation_low", 0.7)),
            "vol_confirmation_high": float(getattr(t, "vol_confirmation_high", 1.5)),
            "vol_confirmation_low_penalty": float(getattr(t, "vol_confirmation_low_penalty", 6.0)),
            "vol_confirmation_high_bonus": float(getattr(t, "vol_confirmation_high_bonus", 4.0)),
            "sr_proximity_pct": float(getattr(t, "sr_proximity_pct", 0.15)),
            "sr_proximity_penalty_base": float(getattr(t, "sr_proximity_penalty_base", 5.0)),
        }

    def _dynamic_strategy_weights(self, strategy_names: List[str], regime: Optional[str] = None) -> Optional[Dict[str, float]]:
        """Builds the {name: multiplier} dict evaluate() uses for the Dynamic
        Weighted Ensemble (opt-in) AND any manual per-strategy weight
        override (TradingSettings.strategy_weight_overrides, e.g.
        {"ema_ribbon": 1.2}) -- combined multiplicatively when both apply.
        Returns None only when NEITHER mechanism has anything to contribute
        (a true no-op for evaluate(), each vote keeps its plain fixed weight)."""
        t = self.runtime.trading
        overrides = getattr(t, "strategy_weight_overrides", None) or {}
        dynamic_on = bool(getattr(t, "dynamic_ensemble_enabled", False))
        if not dynamic_on and not overrides:
            return None
        floor = float(getattr(t, "dynamic_ensemble_floor", 0.7))
        ceiling = float(getattr(t, "dynamic_ensemble_ceiling", 1.3))
        low_wr = float(getattr(t, "strategy_mute_win_rate", 30.0))
        mid_wr = float(getattr(t, "strategy_trial_win_rate", 50.0))
        high_wr = float(getattr(t, "strategy_promote_win_rate", 70.0))
        regime_blend = float(getattr(t, "dynamic_ensemble_regime_blend", 0.4)) if getattr(t, "regime_aware_strategy_intelligence_enabled", False) else 0.0
        result = {}
        for name in strategy_names:
            dynamic_mult = self.strategy_manager.performance_multiplier(
                name, floor=floor, ceiling=ceiling, low_wr=low_wr, mid_wr=mid_wr, high_wr=high_wr,
                regime=regime, regime_blend=regime_blend,
            ) if dynamic_on else 1.0
            result[name] = dynamic_mult * float(overrides.get(name, 1.0))
        return result

    def _adaptive_duration(self, base_duration: int, atr_pct: float) -> int:
        """N2: previously duration_seconds was a single static value
        regardless of current volatility — same expiry window whether the
        market was calm or violently choppy. Widen it in calm conditions
        (a genuine move needs more time to develop) and shorten it in high
        volatility (less time exposed to a violent reversal before expiry).
        Opt-in (default False); disabled returns base_duration unchanged."""
        if not getattr(self.runtime.trading, "volatility_adjusted_duration", False):
            return base_duration
        t = self.runtime.trading
        low_cut = float(getattr(t, "duration_low_vol_atr_pct", 0.08))
        high_cut = float(getattr(t, "duration_high_vol_atr_pct", 0.5))
        low_mult = float(getattr(t, "duration_low_vol_mult", 1.3))
        high_mult = float(getattr(t, "duration_high_vol_mult", 0.7))
        min_secs = int(getattr(t, "duration_min_seconds", 30))

        if atr_pct <= 0:
            mult = 1.0
        elif atr_pct < low_cut:
            mult = low_mult
        elif atr_pct > high_cut:
            mult = high_mult
        else:
            mult = 1.0
        return max(min_secs, int(round(base_duration * mult)))

    def _on_trace_stage(self, signal_id: str, event: str, payload: Dict[str, Any]) -> None:
        """Callback from PipelineTracer -- fires synchronously inside
        start_trace/mark_stage/end_trace, so this only ever schedules the
        actual (async) WS broadcast rather than awaiting it inline. A slow
        or disconnected client can never block a trace call."""
        try:
            asyncio.create_task(hub.broadcast(self.user_id, "pipeline_trace_update", {
                "signal_id": signal_id, "event": event, **payload,
            }))
        except Exception:
            pass

    def _pipeline_snapshot(self, asset: str, tf: str, **fields) -> None:
        """Update and broadcast one asset's continuous pipeline snapshot.
        Every value passed in is already computed by the caller -- this
        function does not calculate anything itself. Never raises."""
        try:
            snap = self._pipeline_events.update_snapshot(asset, tf, **fields)
            if snap is not None:
                # Counted HERE, at the one choke point all thirteen callers
                # funnel through, so the telemetry tracks actual Live
                # Pipeline publications -- PASSED, REJECTED, BLOCKED,
                # FAILED alike.
                #
                # It was previously incremented inside start_trace(), which
                # only fires when a Signal is generated. That made
                # pipeline_snapshot_update_count a duplicate of
                # signals_generated and left it flat during a quiet market,
                # i.e. it reported "no pipeline activity" precisely when the
                # scanner was working normally and rejecting everything.
                self.health["last_pipeline_snapshot_at"] = now_ts()
                self.health["pipeline_snapshot_update_count"] = (
                    self.health.get("pipeline_snapshot_update_count", 0) + 1
                )
                self.health["pipeline_snapshots_this_cycle"] = (
                    self.health.get("pipeline_snapshots_this_cycle", 0) + 1
                )
                asyncio.create_task(hub.broadcast(self.user_id, "pipeline_snapshot", snap.to_dict()))
        except Exception:
            pass

    @staticmethod
    def _indicator_snapshot(row) -> Dict[str, float]:
        """Reads already-computed indicator columns off an enriched
        DataFrame row (IndicatorCache's cached tail) -- no recomputation.
        Silently skips any column not present rather than erroring, since
        which indicators exist depends on which strategies are active."""
        wanted = ("ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr", "adx",
                  "supertrend", "vwap", "bb_upper", "bb_lower", "bb_mid", "psar",
                  "stoch_rsi", "volume", "support", "resistance", "trend_strength")
        out: Dict[str, float] = {}
        for col in wanted:
            try:
                val = row.get(col)
                if val is not None and val == val:  # NaN-safe (NaN != NaN)
                    out[col] = float(val)
            except Exception:
                continue
        return out

    def _persist_decision(self, decision: Optional["SignalDecision"], asset_symbol: str) -> None:
        """Persists a SignalDecision to DecisionStore and, if it's a
        rejection with enough data to resolve a hypothetical outcome later
        (price/duration/payout set), registers it with
        RejectedOutcomeStore too (Phase 3). Wrapped defensively -- this is
        instrumentation and must never affect the actual trading path."""
        if decision is None:
            return
        try:
            self.decision_store.append(decision)
        except Exception:
            logger.debug("[decision_engine] append failed for %s", asset_symbol, exc_info=True)
            return
        if decision.final_decision == "rejected":
            try:
                self.rejected_outcome_store.track(decision)
            except Exception:
                logger.debug("[rejection_reconciliation] track failed for %s", asset_symbol, exc_info=True)

    def _account_scope_dir(self, account_mode: str, tournament_id: Optional[int]) -> Optional[Path]:
        """Returns the isolated storage directory for this account scope,
        or None for 'live'/'demo' -- which deliberately keep using the
        exact same (unscoped) paths they always have, so switching
        between Live and Demo changes nothing about existing behavior.
        Only Tournament gets a new, separate scope, keyed by tournament
        ID so two different tournaments never share state either."""
        if account_mode == "tournament" and tournament_id:
            d = user_data_dir(self.user_id) / "tournaments" / str(int(tournament_id))
            d.mkdir(parents=True, exist_ok=True)
            return d
        return None

    def _rebuild_account_scoped_stores(self, scope_dir: Optional[Path]) -> None:
        """(Re)constructs every engine whose state must be isolated per
        account scope, pointing at either the default (unscoped, scope_dir
        =None) paths or a tournament's own directory. Deliberately mirrors
        __init__'s construction calls rather than __init__ calling this
        method itself -- __init__ is the proven, already-tested startup
        path and touching it to share this code was a larger risk than
        the small duplication here. Engines NOT listed here (calibrator,
        regime_tracker, analytics_engine, pattern_risk_engine,
        recovery_engine) are stateless on disk and entirely derived from
        self.store.all() / self.shadow_store -- correctly isolated for
        free once those two are rebuilt, as long as they're refreshed
        immediately after (done in switch_account_mode below)."""
        uid = self.user_id
        t = self.runtime.trading
        if scope_dir is not None:
            self.store = TradeStore(uid, scope_dir=scope_dir)
            self.strategy_manager = StrategyPerformanceManager(
                scope_dir,
                min_sample=int(getattr(t, "strategy_min_sample", 30)),
                trial_size=int(getattr(t, "strategy_trial_size", 20)),
                rolling_window=int(getattr(t, "strategy_rolling_window", 50)),
                typical_payout_pct=float(getattr(t, "strategy_typical_payout_pct", 80.0)),
                base_cooldown=float(getattr(t, "strategy_base_cooldown_seconds", 21600.0)),
                max_cooldown=float(getattr(t, "strategy_max_cooldown_seconds", 259200.0)),
                wilson_confidence_level=float(getattr(t, "adaptive_wilson_confidence_level", 0.95)),
                ewma_alpha=getattr(t, "adaptive_learning_ewma_alpha", None),
                drift_weight=getattr(t, "adaptive_learning_drift_weight", None),
            )
            self.decision_store = DecisionStore(str(scope_dir / "decisions"))
            self.rejected_outcome_store = RejectedOutcomeStore(str(scope_dir / "rejected_outcomes.json"))
            self.drift_detector = DriftDetector(
                str(scope_dir / "drift_detection.json"),
                delta=float(getattr(t, "drift_delta", 0.005)),
                threshold=float(getattr(t, "drift_threshold", 25.0)),
                min_samples=int(getattr(t, "drift_min_samples", 20)),
                cooldown_seconds=float(getattr(t, "drift_cooldown_seconds", 3600.0)),
            )
            self.confidence_model_store = ConfidenceModelStore(str(scope_dir / "confidence_model.json"))
            self.confidence_model_store.model.min_samples_to_train = int(getattr(t, "confidence_model_min_training_samples", 200))
            self.confidence_model_store.model.min_samples_to_trust = int(getattr(t, "confidence_model_prediction_threshold", 300))
            self.pattern_risk_validation = PatternRiskValidationStore(uid, scope_dir=scope_dir)
            self.queue_persistence = QueuePersistence(storage_path=str(scope_dir / "signal_queue"))
            self.shadow_store = ShadowStore(storage_path=str(scope_dir / "shadow_trades.json"))
        else:
            # Back to the exact original, unscoped paths -- identical to
            # what __init__ constructs at startup.
            self.store = TradeStore(uid)
            self.strategy_manager = StrategyPerformanceManager(
                user_data_dir(uid),
                min_sample=int(getattr(t, "strategy_min_sample", 30)),
                trial_size=int(getattr(t, "strategy_trial_size", 20)),
                rolling_window=int(getattr(t, "strategy_rolling_window", 50)),
                typical_payout_pct=float(getattr(t, "strategy_typical_payout_pct", 80.0)),
                base_cooldown=float(getattr(t, "strategy_base_cooldown_seconds", 21600.0)),
                max_cooldown=float(getattr(t, "strategy_max_cooldown_seconds", 259200.0)),
                wilson_confidence_level=float(getattr(t, "adaptive_wilson_confidence_level", 0.95)),
                ewma_alpha=getattr(t, "adaptive_learning_ewma_alpha", None),
                drift_weight=getattr(t, "adaptive_learning_drift_weight", None),
            )
            self.decision_store = DecisionStore(str(user_data_dir(uid) / "decisions"))
            self.rejected_outcome_store = RejectedOutcomeStore(str(user_data_dir(uid) / "rejected_outcomes.json"))
            self.drift_detector = DriftDetector(
                str(user_data_dir(uid) / "drift_detection.json"),
                delta=float(getattr(t, "drift_delta", 0.005)),
                threshold=float(getattr(t, "drift_threshold", 25.0)),
                min_samples=int(getattr(t, "drift_min_samples", 20)),
                cooldown_seconds=float(getattr(t, "drift_cooldown_seconds", 3600.0)),
            )
            self.confidence_model_store = ConfidenceModelStore(str(user_data_dir(uid) / "confidence_model.json"))
            self.confidence_model_store.model.min_samples_to_train = int(getattr(t, "confidence_model_min_training_samples", 200))
            self.confidence_model_store.model.min_samples_to_trust = int(getattr(t, "confidence_model_prediction_threshold", 300))
            self.pattern_risk_validation = PatternRiskValidationStore(uid)
            self.queue_persistence = QueuePersistence(storage_path=str(user_data_dir(uid) / "signal_queue"))
            self.shadow_store = ShadowStore(storage_path=str(user_data_dir(uid) / "shadow_trades.json"))
        self._last_confidence_model_retrain_count = 0

    async def switch_account_mode(self, account_mode: str, tournament_id: Optional[int] = None) -> Tuple[bool, str]:
        """Switches between Live / Demo / Tournament. Returns (success,
        message). Safe to call at any time; refuses (rather than doing
        something unsafe) if there's an open trade, since an in-flight
        order belongs to the account it was placed on and must not get
        resolved against a different account's now-active TradeStore."""
        account_mode = (account_mode or "demo").lower()
        if account_mode not in ("live", "demo", "tournament"):
            return False, f"Unknown account mode: {account_mode}"

        if self.active_trades:
            logger.warning("[account] Switch to %s refused for %s: %d open trade(s)", account_mode, self.user_id, len(self.active_trades))
            return False, f"Cannot switch accounts with {len(self.active_trades)} open trade(s) -- wait for them to resolve first."

        if account_mode == "tournament":
            if not tournament_id:
                return False, "Tournament mode requires a tournament ID."
            if not self.provider.supports_tournaments():
                log_event(logger, logging.WARNING, "tournament_unavailable", user_id=self.user_id, provider=self.provider.name,
                          reason="provider does not support tournament accounts")
                return False, f"'{self.provider.name}' does not support tournament accounts."

        previous_mode = self.state.account_mode
        previous_tournament = self.state.tournament_id
        ok = await self.provider.switch_account(account_mode, tournament_id)
        if not ok:
            log_event(logger, logging.WARNING, "tournament_unavailable", user_id=self.user_id,
                      account_mode=account_mode, tournament_id=tournament_id,
                      reason="provider.switch_account returned False")
            return False, "The broker rejected this account switch (invalid/expired tournament ID, or not currently connected)."

        scope_dir = self._account_scope_dir(account_mode, tournament_id)
        self._rebuild_account_scoped_stores(scope_dir)
        # Force an immediate rebuild of the derived (stateless-on-disk)
        # engines from the NEWLY-scoped trade history, rather than waiting
        # for their normal periodic refresh -- otherwise they'd keep
        # serving the PREVIOUS account's curve/weights for a while.
        self.calibrator.refresh(self.store.all())
        self.regime_tracker.refresh(self.store.all())

        # Recompute live session counters from the new account's own
        # history instead of carrying over the previous account's numbers.
        closed = [t for t in self.store.all() if t.status in ("win", "loss", "draw")]
        wins = sum(1 for t in closed if t.status == "win")
        losses = sum(1 for t in closed if t.status == "loss")
        draws = sum(1 for t in closed if t.status == "draw")
        try:
            balance, currency = await self.provider.get_balance()
        except Exception:
            balance, currency = self.state.balance, self.state.currency

        self.state.account_mode = account_mode
        self.state.tournament_id = tournament_id if account_mode == "tournament" else None
        self.state.is_demo = account_mode in ("demo", "tournament")
        self.state.balance = balance
        self.state.currency = currency
        self.state.starting_balance = balance
        self.state.daily_pnl = 0.0
        self.state.wins, self.state.losses, self.state.draws = wins, losses, draws
        self.state.consecutive_losses = 0
        self.state.last_update = now_ts()

        self.runtime.trading.account_mode = account_mode
        self.runtime.trading.tournament_id = tournament_id if account_mode == "tournament" else None
        self.runtime.trading.is_demo = self.state.is_demo
        self.runtime.save(self.user_id)

        # RCA F4: also persist the account type WHERE A RESTART READS IT FROM.
        # runtime_settings.json drives the UI and TradeRecord.is_demo, but
        # `build_provider()` is fed by `broker_sessions.account_type` via
        # `_broker_settings()`. Updating only the former meant a Demo -> Live
        # switch survived in the UI and in the trade log, while the next
        # restart rebuilt the provider against the DEMO account -- real orders
        # silently became paper ones (and a Live -> Demo switch would have
        # placed REAL orders on restart). `set_account_type` touches only that
        # column, so the stored email/password/user-agent are untouched.
        try:
            persisted = await asyncio.to_thread(
                session_manager.set_account_type, self.user_id, self.state.is_demo
            )
            if not persisted:
                log_event(
                    logger, logging.WARNING, "account_type_not_persisted",
                    user_id=self.user_id, account_mode=account_mode,
                    reason="no broker_sessions row for this user -- the account "
                           "mode will not survive a restart until credentials are saved",
                )
        except Exception:
            # A DB failure must not undo a switch the broker already accepted
            # (the connection is now genuinely on the new account), but it
            # must be loud: the mode will silently revert on the next restart.
            log_event(
                logger, logging.ERROR, "account_type_persist_failed",
                user_id=self.user_id, account_mode=account_mode,
                reason="failed to update broker_sessions.account_type -- the "
                       "account mode will NOT survive a restart",
                exc_info=True,
            )
            await hub.broadcast(self.user_id, "notice", {
                "message": f"Switched to {account_mode.upper()}, but the account mode could not "
                           f"be saved -- it will revert after a restart. Re-save your Quotex "
                           f"credentials in Settings.",
            })

        log_event(
            logger, logging.INFO, "account_switched",
            user_id=self.user_id, previous_mode=previous_mode, previous_tournament_id=previous_tournament,
            new_mode=account_mode, new_tournament_id=tournament_id, balance=balance,
        )
        if account_mode == "tournament":
            log_event(logger, logging.INFO, "tournament_selected", user_id=self.user_id, tournament_id=tournament_id, balance=balance)

        await hub.broadcast(self.user_id, "state", self.state.model_dump())
        await hub.broadcast(self.user_id, "notice", {
            "message": f"Switched to {account_mode.upper()}" + (f" (tournament {tournament_id})" if account_mode == "tournament" else "")
        })
        return True, f"Switched to {account_mode}" + (f" (tournament {tournament_id})" if account_mode == "tournament" else "")

    async def _evaluate_asset_signal(
        self,
        asset,
        tf: str,
        htf: str,
        use_htf: bool,
        active_strategy_list: List[str],
        hhtf: Optional[str] = None,
    ) -> Optional[tuple[str, Optional[Signal], str, str]]:
        if getattr(self.runtime.trading, "precision_mode_enabled", False):
            cooldown_reason = self.risk.asset_on_cooldown(asset.symbol, now_ts())
            if cooldown_reason:
                self._pipeline_snapshot(asset.symbol, tf, stage="rejected", stage_reason=f"cooldown: {cooldown_reason}")
                return None
        latency_key = f"tmp:{asset.symbol}"
        self._pipeline_snapshot(asset.symbol, tf, stage="running", ws_connected=self.state.connected)
        try:
            # Fetch depth follows the configured gate. Was a hardcoded 120,
            # which could not satisfy a 200-bar sufficiency threshold no
            # matter how long the session ran.
            want_bars = int(getattr(self.runtime.trading, "history_fetch_bars", 300))
            candles = await self.error_recovery.execute_with_retry(
                self.provider.get_candles, "get_candles", asset.symbol, tf, want_bars,
                # Bounds EACH attempt. Without it the retry/breaker
                # machinery below is blind to a hang, because it is
                # entirely exception-driven and a hang raises nothing.
                timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Previously this exception propagated silently up through
            # asyncio.gather(return_exceptions=True) and just got skipped for
            # this asset with no log line. Now it's retried twice first, and
            # only logged/dropped if the breaker is open or retries exhaust.
            logger.warning("[get_candles] %s/%s failed after retries: %s", asset.symbol, tf, exc)
            self.latency.drop(latency_key)
            self._pipeline_snapshot(asset.symbol, tf, stage="failed", stage_reason=f"candle_fetch: {exc}")
            return None
        self.latency.start(latency_key, asset.symbol)  # checkpoint: provider_receive
        self._last_candle_at = now_ts()  # feeds the watchdog's stale-data detection (below)
        # Persist live candles for validation / correlation (non-blocking write).
        try:
            self.candle_store.upsert_many(asset.symbol, tf, candles)
        except Exception:
            pass
        per_asset_strats = self.health_manager.filter_strategies(
            asset.symbol, tf, active_strategy_list,
        )
        if not per_asset_strats:
            self.latency.drop(latency_key)
            self._pipeline_snapshot(asset.symbol, tf, stage="rejected", stage_reason="no active strategies (health/manual muted)")
            return asset.symbol, None, "mixed", latency_key
        # ACCURACY GATE: the scan loop previously used get_enriched's built-in
        # 55-bar floor -- the point where the indicator functions stop
        # returning NaN, NOT the point where their output is trustworthy. On a
        # broker session returning short history this meant grading real setups
        # on an EMA(50) still dominated by its seed and an ADX(14) that had
        # barely finished smoothing. Now the configured threshold applies, and
        # an asset short of it is skipped with the actual bar count recorded so
        # the pipeline says WHY rather than "not warm".
        min_bars = max(55, int(getattr(self.runtime.trading, "min_candles_for_signal", 200)))
        edf, warm = self._indicators.get_enriched(
            f"{asset.symbol}|{tf}", candles, min_bars=min_bars)
        if not warm or edf is None:
            self.latency.drop(latency_key)
            self.health.setdefault("scan_gate_blocks", {})
            self.health["scan_gate_blocks"]["insufficient_history"] = (
                self.health["scan_gate_blocks"].get("insufficient_history", 0) + 1
            )
            self._pipeline_snapshot(
                asset.symbol, tf, stage="rejected",
                stage_reason=(f"insufficient history: {len(candles)} bars, "
                              f"{min_bars} required for converged indicators"),
            )
            return None
        # Evaluate on the latest *closed* candle (never the still-forming one).
        edf_eval = edf.iloc[:-1] if len(edf) > 1 else edf
        row_last = edf.iloc[-1]
        self._pipeline_snapshot(
            asset.symbol, tf,
            last_candle_ts=float(row_last.get("timestamp", 0)) or None,
            open=float(row_last.get("open", 0)) or None, high=float(row_last.get("high", 0)) or None,
            low=float(row_last.get("low", 0)) or None, close=float(row_last.get("close", 0)) or None,
            indicators=self._indicator_snapshot(row_last),
        )

        # N3: now that we can read the current regime cheaply (before the
        # full evaluate() call), apply the regime-specific soft-exclusion on
        # top of the already health/manual-filtered strategy list. This is
        # additive — a strategy only gets excluded here if it's already
        # passed the global mute check above and still has a poor
        # regime-specific track record with enough of its own samples.
        current_regime = strategies.current_regime(
            edf_eval,
            trend_adx=float(getattr(self.runtime.trading, "regime_trend_adx", 25.0)),
            range_adx=float(getattr(self.runtime.trading, "regime_range_adx", 18.0)),
        ) if len(edf_eval) >= 1 else "mixed"
        per_asset_strats = self.strategy_manager.active_strategies(per_asset_strats, regime=current_regime)
        if not per_asset_strats:
            self.latency.drop(latency_key)
            self._pipeline_snapshot(asset.symbol, tf, stage="rejected", stage_reason=f"no strategies active for regime={current_regime}", regime=current_regime)
            return asset.symbol, None, current_regime, latency_key

        htf_bias = None
        htf_strength = 0.0
        mtf_ctx = None
        mtf_mode = getattr(self.runtime.trading, "mtf_mode", "soft")
        if use_htf:
            try:
                # ROOT-CAUSE FIX (secondary): these two HTF/HHTF fetches were
                # the only provider calls on the scan path with NO protection
                # at all -- no error_recovery wrapper, no timeout. The
                # try/except around them catches exceptions, but a half-open
                # socket does not raise, so a hang here stalled the asset,
                # then gather(), then the whole scan cycle. Bounded to the
                # same ceiling as the primary candle fetch. The existing
                # except path already treats a failure as "no HTF bias",
                # which is exactly the right degradation.
                htf_candles = await asyncio.wait_for(
                    self.provider.get_candles(asset.symbol, htf, 120),
                    timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
                )
                htf_edf, htf_warm = self._indicators.get_enriched(f"{asset.symbol}|{htf}", htf_candles)
                htf_eval = None
                if htf_warm:
                    htf_eval = htf_edf.iloc[:-1] if len(htf_edf) > 1 else htf_edf

                if mtf_mode == "hard":
                    hhtf_eval = None
                    if hhtf and hhtf != htf:
                        try:
                            hhtf_candles = await asyncio.wait_for(
                                self.provider.get_candles(asset.symbol, hhtf, 60),
                                timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
                            )
                            hhtf_edf, hhtf_warm = self._indicators.get_enriched(f"{asset.symbol}|{hhtf}", hhtf_candles)
                            if hhtf_warm:
                                hhtf_eval = hhtf_edf.iloc[:-1] if len(hhtf_edf) > 1 else hhtf_edf
                        except Exception:
                            hhtf_eval = None
                    mtf_ctx = strategies.build_mtf_context(htf_eval, hhtf_eval)
                elif htf_eval is not None:
                    htf_bias, htf_strength = strategies.htf_trend_bias(htf_eval)
            except Exception:
                htf_bias, mtf_ctx = None, None

        self.latency.mark(latency_key, "eval_start")
        res = strategies.evaluate(edf_eval, per_asset_strats, pre_enriched=True,
                                   htf_bias=htf_bias, mtf=mtf_ctx,
                                   is_otc=getattr(asset, "is_otc", False),
                                   strategy_weights=self._dynamic_strategy_weights(per_asset_strats, regime=current_regime),
                                   **self._evaluate_gate_kwargs(asset=asset.symbol, timeframe=tf))

        # Adaptive Market Regime Transition Detector: off by default (see
        # TradingSettings.regime_transition_detector_enabled) -- same
        # opt-in posture as decision_engine_enabled/drift_detection_enabled/
        # confidence_model_enabled below. When on, every signal passes
        # through it before the confidence pipeline runs. It never forces a
        # direction -- only adjusts confidence/threshold/timing (and can
        # pause/cooldown entry entirely during a detected chaotic
        # transition), and is designed to be near-neutral (multiplier ~1.0,
        # no cooldown, never paused) during genuinely stable conditions.
        regime_transition_result = None
        regime_transition_enabled = bool(getattr(self.runtime.trading, "regime_transition_detector_enabled", False))
        if regime_transition_enabled and res.direction is not None:
            try:
                mtf_signals = {"current": TimeframeSignal(res.direction.value, res.confidence / 100.0)}
                if htf_bias is not None:
                    mtf_signals["htf"] = TimeframeSignal(htf_bias.value, htf_strength)
                if mtf_ctx is not None:
                    if getattr(mtf_ctx, "htf", None) is not None:
                        mtf_signals["htf"] = TimeframeSignal(mtf_ctx.htf.value, getattr(mtf_ctx, "htf_strength", 0.5))
                    if getattr(mtf_ctx, "hhtf", None) is not None:
                        mtf_signals["hhtf"] = TimeframeSignal(mtf_ctx.hhtf.value, getattr(mtf_ctx, "hhtf_strength", 0.5))
                regime_transition_result = self.regime_transition_engine.update(
                    f"{asset.symbol}|{tf}", edf_eval.iloc[-1].to_dict(), mtf=mtf_signals,
                )
            except Exception:
                logger.debug("[regime_transition] update failed for %s — signal proceeds unadjusted", asset.symbol, exc_info=True)
                regime_transition_result = None

            if regime_transition_result is not None:
                rt_key = f"{asset.symbol}|{tf}"
                self._regime_bar_counter[rt_key] = self._regime_bar_counter.get(rt_key, 0) + 1
                if regime_transition_result.paused:
                    log_event(logger, logging.INFO, "signal_skipped_regime_paused", asset=asset.symbol,
                              regime=regime_transition_result.regime.value, reason=regime_transition_result.reason)
                    # THE OTHER HALF OF THE FROZEN-LIVE-PIPELINE BUG.
                    # These two regime gates were the ONLY early returns in
                    # this function that published no snapshot -- the other
                    # eleven all call _pipeline_snapshot before returning.
                    # So an asset blocked here vanished from the Live
                    # Pipeline entirely rather than appearing as BLOCKED,
                    # which is why the panel froze at 17-18 minutes old while
                    # scan cycles kept advancing and every other card stayed
                    # green.
                    self._pipeline_snapshot(
                        asset.symbol, tf, stage="blocked",
                        stage_reason=f"regime_paused:{regime_transition_result.reason}",
                        regime=regime_transition_result.regime.value,
                    )
                    self.latency.drop(latency_key)
                    return asset.symbol, None, res.regime, latency_key
                # ARM ONCE, THEN COUNT DOWN.
                #
                # This used to re-arm unconditionally on every evaluation:
                #     cooldown_until[k] = bar_counter[k] + cooldown_candles
                # while bar_counter[k] had just been incremented by 1. So the
                # deadline moved forward by exactly as much as the counter
                # did, and `bar_counter < cooldown_until` stayed true FOREVER
                # for as long as the transition detector reported any
                # non-zero cooldown. Replayed over 12 cycles: 12/12 blocked,
                # deadline 4,5,6,...,15 chasing counter 1,2,3,...,12.
                #
                # The asset then never reached start_trace() at line ~2467,
                # so it produced no Signal and no Live Pipeline entry -- while
                # the scan loop itself kept running, market data stayed fresh
                # and Pipeline supervision reported OK. That is exactly the
                # reported symptom: everything green, Live Pipeline frozen.
                #
                # The cooldown is now armed on the transition that requests
                # it and left alone, so the counter actually reaches it.
                # Intent (pause after a regime transition) is unchanged; only
                # the never-releasing part is.
                if (
                    regime_transition_result.cooldown_candles > 0
                    and rt_key not in self._regime_cooldown_until_bar
                ):
                    self._regime_cooldown_until_bar[rt_key] = (
                        self._regime_bar_counter[rt_key] + regime_transition_result.cooldown_candles
                    )
                if self._regime_bar_counter[rt_key] < self._regime_cooldown_until_bar.get(rt_key, 0):
                    self.health["scan_gate_blocks"]["regime_cooldown"] = (
                        self.health["scan_gate_blocks"].get("regime_cooldown", 0) + 1
                    )
                    remaining = (self._regime_cooldown_until_bar[rt_key]
                                 - self._regime_bar_counter[rt_key])
                    log_event(logger, logging.DEBUG, "signal_delayed_regime_cooldown", asset=asset.symbol,
                              bars_remaining=remaining)
                    # Same fix as the paused gate above: publish BLOCKED so
                    # the asset stays visible in the Live Pipeline with the
                    # reason and the remaining countdown, instead of
                    # disappearing.
                    self._pipeline_snapshot(
                        asset.symbol, tf, stage="blocked",
                        stage_reason=f"regime_cooldown:{remaining}_left",
                        regime=res.regime,
                    )
                    self.latency.drop(latency_key)
                    return asset.symbol, None, res.regime, latency_key
                # Elapsed -- clear it so a LATER transition can arm a fresh
                # cooldown. Without this the "arm once" guard above would
                # mean an asset only ever gets one cooldown for the life of
                # the process.
                self._regime_cooldown_until_bar.pop(rt_key, None)
                # Confidence influence (never direction) -- applied to the RAW
                # confidence res.confidence produced, before the existing
                # regime_tracker adjustment/calibration pipeline runs, so it
                # composes with those exactly like any other input rather than
                # overriding them.
                res.confidence = int(round(res.confidence * regime_transition_result.recommended_confidence_multiplier))

        decision_enabled = bool(getattr(self.runtime.trading, "decision_engine_enabled", False))
        decision = None
        live_confidence_model_prediction = None  # set below only when confidence_model_shadow_mode
        # is explicitly False (promoted out of shadow) -- stays None (fully inert) at the default True
        if decision_enabled:
            try:
                adx_val = float(edf_eval.iloc[-1]["adx"]) if "adx" in edf_eval.columns else None
                vol_ratio_val = float(edf_eval.iloc[-1]["vol_ratio"]) if "vol_ratio" in edf_eval.columns else None
                decision = build_decision(
                    res, asset=asset.symbol, timeframe=tf,
                    calibrator=self.calibrator, health_manager=self.health_manager,
                    regime_adx=adx_val, volatility_ratio=vol_ratio_val,
                )
                decision.price = float(edf["close"].iloc[-1])
                decision.payout = float(getattr(asset, "payout", 0) or 0)
                decision.duration = int(self.runtime.trading.duration_seconds)
                if regime_transition_result is not None:
                    decision.regime_transition_state = regime_transition_result.regime.value
                    decision.regime_transition_probability = regime_transition_result.transition_probability
                    decision.regime_transition_confidence = regime_transition_result.regime_confidence
                    decision.regime_stability_score = regime_transition_result.stability_score
                try:
                    decision.composite_confidence = compute_composite_confidence(
                        decision,
                        historical_weight=float(getattr(self.runtime.trading, "decision_historical_probability_weight", 0.34)),
                        regime_weight=float(getattr(self.runtime.trading, "decision_regime_weight", 0.33)),
                        calibration_weight=float(getattr(self.runtime.trading, "decision_calibration_weight", 0.33)),
                    )
                except Exception:
                    logger.debug("[decision_engine] composite_confidence failed for %s", asset.symbol, exc_info=True)
                if getattr(self.runtime.trading, "confidence_model_enabled", False) and res.raw_features:
                    feat_vec = [res.raw_features.get(n, 0.0) for n in FEATURE_NAMES]
                    shadow_p = self.confidence_model_store.model.predict_proba(feat_vec)
                    if shadow_p is not None:
                        decision.shadow_confidence_model_prediction = round(shadow_p * 100, 1)
                        # Promotion out of shadow: only when the operator has
                        # explicitly set confidence_model_shadow_mode=False
                        # (default True = current behavior, this never runs).
                        # A trusted prediction (model.predict_proba already
                        # returns None below its own min_samples_to_trust
                        # floor) becomes a bounded, blended nudge to the live
                        # confidence below -- not a replacement -- matching
                        # every other "adjustment, not override" pattern in
                        # this pipeline (regime transition's multiplier,
                        # decision evidence gate, etc).
                        if not bool(getattr(self.runtime.trading, "confidence_model_shadow_mode", True)):
                            live_confidence_model_prediction = round(shadow_p * 100, 1)
            except Exception:
                logger.debug("[decision_engine] build_decision failed for %s — instrumentation only, continuing", asset.symbol, exc_info=True)
                decision = None

        if not res.direction:
            self._persist_decision(decision, asset.symbol)
            self.latency.drop(latency_key)
            self._pipeline_snapshot(asset.symbol, tf, stage="rejected", stage_reason="no direction consensus", regime=res.regime)
            return asset.symbol, None, res.regime, latency_key

        # Decision Intelligence evidence gate: OFF by default
        # (decision_reject_weak_evidence=False), and requires
        # decision_engine_enabled too since it has nothing to gate on
        # otherwise. When both are on, a signal whose supporting-evidence
        # count or decision-engine confidence estimate falls below the
        # configured floor is rejected here -- same shape as every other
        # gate in this function (precision gate, confidence-threshold gate
        # below), not a special case. Uses decision.composite_confidence
        # (Decision Intelligence's own blended estimate, already computed
        # above) in preference to raw/calibrated confidence, since this is
        # specifically the Decision Intelligence system's own gate.
        if (decision is not None and decision_enabled
                and bool(getattr(self.runtime.trading, "decision_reject_weak_evidence", False))):
            min_evidence = int(getattr(self.runtime.trading, "decision_min_evidence_count", 1))
            min_conf = float(getattr(self.runtime.trading, "decision_min_confidence_threshold", 50))
            evidence_count = len(decision.supporting_evidence)
            decision_conf = decision.composite_confidence if decision.composite_confidence is not None else (
                decision.calibrated_confidence if decision.calibrated_confidence is not None else decision.raw_confidence
            )
            weak_evidence = evidence_count < min_evidence
            weak_confidence = decision_conf is not None and decision_conf < min_conf
            if weak_evidence or weak_confidence:
                reason = (f"Decision evidence gate: {evidence_count} supporting evidence item(s) "
                          f"< required {min_evidence}" if weak_evidence else
                          f"Decision evidence gate: confidence {decision_conf:.1f} < required {min_conf:.1f}")
                decision.final_decision = "rejected"
                decision.rejection_reason = reason
                self._persist_decision(decision, asset.symbol)
                self.latency.drop(latency_key)
                self._pipeline_snapshot(asset.symbol, tf, stage="rejected", stage_reason=reason, regime=res.regime)
                logger.debug("[decision_engine] %s rejected by evidence gate: %s", asset.symbol, reason)
                return asset.symbol, None, res.regime, latency_key

        contributing = [name for name, d in res.votes.items() if d == res.direction.value]
        regime_adj = self.regime_tracker.blended_adjustment(contributing, res.regime)
        adjusted_confidence = max(0, min(100, res.confidence + regime_adj))

        # ── Realtime feature layer: bounded, optional sentiment nudge ───────
        # Applied AFTER regime adjustment and BEFORE calibration, so the
        # calibrator still sees one combined number and the existing threshold
        # gate below is completely unchanged. `res.direction` is already chosen
        # by the OHLC/indicator/confluence engine at this point — sentiment can
        # only shade confidence by a capped few points, never pick or reverse
        # the direction. With sentiment disabled (default) this is exactly 0.0.
        base_confidence = adjusted_confidence
        try:
            features, sentiment_adj = await self._collect_market_features(
                asset.symbol, tf, edf_eval, direction_value=res.direction.value,
            )
        except Exception:
            features, sentiment_adj = None, SentimentAdjustment(0.0, False, "error")
        if sentiment_adj.adjustment:
            # `adjusted_confidence` is what Signal.raw_confidence is built from
            # below, so the nudge is visible in the record without touching the
            # Signal construction or the threshold gate.
            adjusted_confidence = max(0, min(100, adjusted_confidence + sentiment_adj.adjustment))

        calibrated_confidence = self.calibrator.calibrate(adjusted_confidence, regime=res.regime) if getattr(self.runtime.trading, "calibration_enabled", True) else adjusted_confidence
        if live_confidence_model_prediction is not None:
            calibrated_confidence = int(round(max(0.0, min(100.0, (calibrated_confidence + live_confidence_model_prediction) / 2.0))))
            if decision is not None:
                decision.calibrated_confidence = calibrated_confidence
        effective_threshold = self._effective_confidence_threshold(float(getattr(asset, "payout", 0) or 0))
        if regime_transition_result is not None:
            effective_threshold += regime_transition_result.recommended_threshold_adjustment
        if calibrated_confidence < effective_threshold:
            if decision is not None:
                decision.final_decision = "rejected"
                decision.rejection_reason = f"Calibrated confidence {calibrated_confidence} below effective threshold {effective_threshold:.0f}"
            self._persist_decision(decision, asset.symbol)
            self.latency.drop(latency_key)
            self._pipeline_snapshot(asset.symbol, tf, stage="rejected",
                                     stage_reason=f"confidence {calibrated_confidence} < threshold {effective_threshold}",
                                     regime=res.regime)
            return asset.symbol, None, res.regime, latency_key

        pg = None
        if getattr(self.runtime.trading, "precision_mode_enabled", False):
            t = self.runtime.trading
            adaptive_on = bool(getattr(t, "adaptive_mode_enabled", False)) and bool(getattr(t, "adaptive_precision_gate_enabled", True))
            pg = evaluate_precision(
                res.direction.value, res.votes, res.regime,
                min_agreeing=int(getattr(t, "precision_min_agreeing", 3)),
                max_opposing=int(getattr(t, "precision_max_opposing", 0)),
                require_regime_alignment=bool(getattr(t, "precision_require_regime_alignment", True)),
                market_context=self.market_context if adaptive_on else None,
                context_asset=asset.symbol,
                context_timeframe=tf,
            )
            if not pg.passed:
                logger.debug("[precision] %s: %s", asset.symbol, pg.reason)
                if decision is not None:
                    decision.final_decision = "rejected"
                    decision.rejection_reason = f"precision_gate: {pg.reason}"
                    if pg.evidence is not None:
                        decision.rejecting_evidence.append(pg.evidence)
                self._persist_decision(decision, asset.symbol)
                self.latency.drop(latency_key)
                self._pipeline_snapshot(asset.symbol, tf, stage="rejected", stage_reason=pg.reason, regime=res.regime)
                return asset.symbol, None, res.regime, latency_key

        row_last = edf.iloc[-1]
        atr_pct = (float(row_last["atr"]) / float(row_last["close"]) * 100.0) if row_last["close"] else 0.0
        adaptive_duration = self._adaptive_duration(self.runtime.trading.duration_seconds, atr_pct)
        if decision is not None:
            decision.final_decision = "accepted"
            decision.rejection_reason = None
            decision.calibrated_confidence = calibrated_confidence
            decision.duration = int(adaptive_duration)
            if pg is not None and pg.evidence is not None:
                decision.supporting_evidence.append(pg.evidence)
            try:
                decision.composite_confidence = compute_composite_confidence(
                    decision,
                    historical_weight=float(getattr(self.runtime.trading, "decision_historical_probability_weight", 0.34)),
                    regime_weight=float(getattr(self.runtime.trading, "decision_regime_weight", 0.33)),
                    calibration_weight=float(getattr(self.runtime.trading, "decision_calibration_weight", 0.33)),
                )
            except Exception:
                logger.debug("[decision_engine] composite_confidence recompute failed for %s", asset.symbol, exc_info=True)
            self._persist_decision(decision, asset.symbol)
        sig = Signal(
            asset=asset.symbol,
            direction=res.direction,
            confidence=calibrated_confidence,
            raw_confidence=adjusted_confidence,
            regime=res.regime,
            timeframe=tf,
            duration=adaptive_duration,
            payout=asset.payout,
            price=float(edf["close"].iloc[-1]),
            reasons=res.reasons,
            strategy_votes=res.votes,
            is_otc=getattr(asset, "is_otc", False),
            decision_id=decision.id if decision is not None else None,
        )
        # Stamp the entry window so the UI can count it down instead of
        # guessing -- same function the execution gate uses.
        sig.expires_at = sig.created_at + signal_ttl_seconds(sig.timeframe, sig.regime)
        self.latency.mark(latency_key, "signal_generated")
        log_event(
            logger, logging.INFO, "signal_generated",
            signal_id=sig.id, asset=sig.asset, timeframe=sig.timeframe, direction=sig.direction.value,
            confidence=sig.confidence, raw_confidence=sig.raw_confidence, regime=sig.regime,
            strategies=list(sig.strategy_votes.keys()), payout=sig.payout, is_otc=sig.is_otc,
            reason="cleared_evaluate_gate_and_confidence_threshold",
            **self._market_features_log_fields(features, sentiment_adj, base_confidence,
                                               calibrated_confidence, effective_threshold),
        )
        # Observability: start this signal's pipeline trace and record it in
        # production metrics. Earlier stages (data fetch, indicator calc)
        # happened above but are folded into STRATEGY_EVAL/CONFIDENCE_SCORE
        # here since we only have a signal_id to key traces by from this
        # point on — everything downstream (revalidation, risk, execution)
        # gets full per-stage tracing in _do_execute_signal.
        self.pipeline_tracer.start_trace(sig.id, sig.asset, tf, sig.direction.value)
        # Signal-specific, deliberately NOT the snapshot counter: a healthy
        # cycle can publish many snapshots and generate zero signals.
        self.health["signals_generated"] = self.health.get("signals_generated", 0) + 1
        self.health["signals_this_cycle"] = self.health.get("signals_this_cycle", 0) + 1
        self.pipeline_tracer.mark_stage(
            sig.id, PipelineStage.STRATEGY_EVAL,
            data={"votes": res.votes, "regime": res.regime, "reasons": res.reasons[:3]},
        )
        self.pipeline_tracer.mark_stage(
            sig.id, PipelineStage.MARKET_CONTEXT,
            data={"regime": res.regime, "is_otc": sig.is_otc},
        )
        self.pipeline_tracer.mark_stage(
            sig.id, PipelineStage.CONFIDENCE_SCORE,
            data={"raw": adjusted_confidence, "calibrated": calibrated_confidence, "threshold": effective_threshold},
        )
        if getattr(self.runtime.trading, "precision_mode_enabled", False):
            self.pipeline_tracer.mark_stage(
                sig.id, PipelineStage.PRECISION_GATE,
                data={"passed": True, "agreeing": pg.agreeing, "opposing": pg.opposing, "reason": pg.reason},
            )
        self._pipeline_snapshot(asset.symbol, tf, stage="passed", stage_reason="signal generated",
                                 regime=res.regime)
        self.production_metrics.record_signal(
            sig.id, sig.asset, sig.direction.value, sig.confidence, sig.created_at, contributing,
        )
        return asset.symbol, sig, res.regime, latency_key

    async def execute_signal(self, signal_id: str) -> Optional[TradeRecord]:
        """Public entry point — goes through the per-user TradeEngine queue
        so a manual REST call and the auto-scan loop firing at the same
        moment (or a double-tap) can never race each other into placing two
        orders. See engine/trade_engine.py."""
        return await self.trade_engine.submit(signal_id)

    async def _execute_shadow(self, sig: Signal) -> None:
        """Shadow Mode path — records what WOULD have been traded, without
        ever calling the broker's place_order. Resolution happens later in
        _shadow_loop by comparing entry vs. exit price, same as how binary
        options actually settle."""
        contributing = [name for name, d in sig.strategy_votes.items() if d == sig.direction.value]
        trade = ShadowTrade(
            id=sig.id,
            asset=sig.asset,
            direction=sig.direction.value,
            entry_price=sig.price,
            confidence=sig.confidence,
            regime=sig.regime,
            strategies=contributing,
            opened_at=now_ts(),
            duration=float(sig.duration),
            payout=sig.payout,
        )
        self.shadow_store.add(trade)
        sig.status = "executed"
        logger.info("[shadow] %s %s @ %.5f conf=%d dur=%ds -- simulated, no real order placed",
                    sig.asset, sig.direction.value.upper(), sig.price, sig.confidence, sig.duration)
        await hub.broadcast(self.user_id, "shadow_trade", {
            "asset": sig.asset, "direction": sig.direction.value, "confidence": sig.confidence,
            "entry_price": sig.price, "duration": sig.duration,
        })
        return None

    async def _shadow_loop(self) -> None:
        """Runs continuously alongside the real _scan_loop/_result_loop when
        shadow_mode_enabled — resolves any pending ShadowTrade whose
        duration has elapsed by fetching a fresh price and comparing it to
        entry, same win/loss rule a real binary option uses."""
        while self._running:
            await asyncio.sleep(3.0)
            # The per-trade fetch below already has its own try/except (a
            # single bad candle fetch just retries next cycle) -- this
            # outer one guards the rest of the iteration (pending_due(),
            # resolve(), broadcast()), none of which used to be protected,
            # so any of them raising would silently end this task for
            # good with nothing left to resolve shadow trades ever again.
            try:
                if not getattr(self.runtime.trading, "shadow_mode_enabled", False):
                    continue
                due = self.shadow_store.pending_due(now_ts())
                for t in due:
                    try:
                        candles = await self.error_recovery.execute_with_retry(
                            self.provider.get_candles, "get_candles", t.asset, self.runtime.trading.timeframe, 2,
                        )
                        if not candles:
                            continue
                        exit_price = float(candles[-1].close)
                    except Exception as exc:
                        logger.debug("[shadow] resolve fetch failed for %s: %s -- will retry next cycle", t.asset, exc)
                        continue
                    # MERGE NOTE: resolve()+broadcast get their OWN try/except
                    # (from the pipeline-fix change set) in addition to the
                    # outer per-iteration guard below (from the reliability
                    # change set). Both are kept deliberately: the inner one
                    # means one bad record costs one item instead of the whole
                    # batch of `due`, and the outer one means nothing in this
                    # iteration can ever end the task.
                    try:
                        resolved = self.shadow_store.resolve(t.id, exit_price)
                        if resolved:
                            logger.info("[shadow] Resolved %s %s: entry=%.5f exit=%.5f -> %s",
                                        resolved.asset, resolved.direction.upper(), resolved.entry_price,
                                        exit_price, resolved.status)
                            await hub.broadcast(self.user_id, "shadow_result", {
                                "asset": resolved.asset, "status": resolved.status,
                                "simulated_profit": resolved.simulated_profit,
                                "stats": self.shadow_store.stats(),
                            })
                    except Exception as exc:
                        logger.debug("[shadow] resolve/broadcast failed for %s: %s", t.asset, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[shadow] iteration failed, continuing: %s", exc, exc_info=True)

    async def _rejection_reconciliation_loop(self) -> None:
        """Runs continuously alongside _shadow_loop when both
        decision_engine_enabled and rejection_reconciliation_enabled are on
        — resolves any pending RejectedOutcome whose duration has elapsed,
        same mechanism as _shadow_loop (entry vs. exit price at expiry).
        This is what makes a gate's cost measurable: see
        rejection_reconciliation.py."""
        while self._running:
            interval = float(getattr(self.runtime.trading, "rejection_reconciliation_interval_seconds", 3.0))
            await asyncio.sleep(max(0.5, interval))
            # See the matching comment in _shadow_loop: this outer
            # try/except protects pending_due()/resolve(), which
            # previously had no protection at all, unlike the per-item
            # candle fetch below.
            try:
                if not getattr(self.runtime.trading, "decision_engine_enabled", False):
                    continue
                if not getattr(self.runtime.trading, "rejection_reconciliation_enabled", True):
                    continue
                due = self.rejected_outcome_store.pending_due(now_ts())
                for o in due:
                    try:
                        candles = await self.error_recovery.execute_with_retry(
                            self.provider.get_candles, "get_candles", o.asset, self.runtime.trading.timeframe, 2,
                        )
                        if not candles:
                            continue
                        exit_price = float(candles[-1].close)
                    except Exception as exc:
                        logger.debug("[rejection_reconciliation] resolve fetch failed for %s: %s -- will retry next cycle", o.asset, exc)
                        continue
                    # MERGE NOTE: same two-layer arrangement as _shadow_loop --
                    # inner guard (pipeline-fix) so one bad record costs one
                    # item, outer guard (reliability) so the task itself can
                    # never die. Both kept.
                    try:
                        resolved = self.rejected_outcome_store.resolve(o.decision_id, exit_price)
                        if resolved:
                            logger.debug("[rejection_reconciliation] Resolved %s (%s, %s): entry=%.5f exit=%.5f -> %s",
                                         resolved.asset, resolved.rejected_by, resolved.regime, resolved.entry_price,
                                         exit_price, resolved.status)
                    except Exception as exc:
                        logger.debug("[rejection_reconciliation] resolve failed for %s: %s", o.asset, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[rejection_reconciliation] iteration failed, continuing: %s", exc, exc_info=True)

    async def _do_execute_signal(self, signal_id: str) -> Optional[TradeRecord]:
        """Thin observability wrapper around _do_execute_signal_impl: whatever
        the inner call does (execute, expire, reject, error), this makes sure
        the signal's pipeline trace is always closed out and its execution
        outcome is always recorded in production_metrics — a single choke
        point instead of instrumenting every one of the inner function's
        early-return branches individually."""
        try:
            return await self._do_execute_signal_impl(signal_id)
        finally:
            sig_after = self.signals.get(signal_id)
            status = sig_after.status if sig_after else "unknown"
            self._finish_signal_tracking(signal_id, success=(status == "executed"), reason=status)

    def _finish_signal_tracking(self, signal_id: str, success: bool, reason: str) -> None:
        trace = self.pipeline_tracer.end_trace(signal_id)
        if trace:
            bugs = self.pipeline_tracer.detect_bugs(trace)
            if bugs:
                logger.warning("[trace] %s pipeline issues detected: %s", signal_id, bugs)
        queue_wait_ms = 0.0
        latency_snap = self.latency.snapshot().get("trade_request->broker_ack")
        if latency_snap:
            queue_wait_ms = float(latency_snap.get("avg_ms", 0.0) or 0.0)
        self.production_metrics.record_execution(
            signal_id, executed_at=now_ts(), success=success,
            queue_wait_ms=queue_wait_ms, reason=reason,
        )

    async def _revalidation_candles(self, asset: str, timeframe: str):
        """Candles for the pre-trade revalidation check, with a local fallback.

        BUG FIX (2026-08): the previous version was a bare 8s network fetch
        with no retry and no fallback. If that one attempt was slow -- which it
        often is, because get_candles may trigger a re-seed of 120+ bars -- the
        result was `current_df = None`, and signal_validation treats a missing
        frame as `data_insufficient` and VETOES the trade. So a slow candle
        request became "Setup no longer valid at entry", and no order was ever
        placed. Vetoing on missing data is right in principle; going to the
        network on the critical path to obtain data we already have on disk is
        not.

        The scan loop persists every candle it evaluates, so the local store
        is seconds old and needs no network at all. Try the live fetch briefly
        for freshness, then fall back to the store, and only report nothing if
        both are empty."""
        try:
            candles = await asyncio.wait_for(
                self.provider.get_candles(asset, timeframe, 120),
                timeout=REVALIDATION_FETCH_TIMEOUT_SECONDS,
            )
            if candles and len(candles) >= 21:
                return candles
            logger.debug("[revalidation] live fetch returned %d bars for %s/%s — using local store",
                         len(candles or []), asset, timeframe)
        except Exception as exc:
            logger.info("[revalidation] live fetch for %s/%s unavailable (%s) — using local store",
                        asset, timeframe, type(exc).__name__)

        # Local, already-persisted candles. No network, no retry, no veto for
        # a reason that has nothing to do with the setup itself.
        try:
            cached = self.candle_store.get_latest_n(asset, timeframe, 120)
            if cached:
                logger.info("[revalidation] using %d stored bars for %s/%s", len(cached), asset, timeframe)
            return cached
        except Exception:
            logger.exception("[revalidation] local candle store read failed for %s/%s", asset, timeframe)
            return []

    def _refuse(self, signal_id: str, reason: str) -> None:
        """Record WHY execution stopped, so the caller can be told.

        Every gate below used to `return None` after logging to the server
        journal. TradeEngine cannot distinguish those Nones from each other,
        so the API answered with a generic "the execution queue did not return
        a trade" and the real reason -- expired signal, failed revalidation,
        risk gate, payout drop -- was only visible to someone tailing logs.
        That is the whole reason manual Enter Trade looked broken."""
        self._last_execution_refusal = reason
        self.latency.drop(signal_id)
        logger.info("[execute] %s refused: %s", signal_id, reason)

    async def _do_execute_signal_impl(self, signal_id: str) -> Optional[TradeRecord]:
        """Actual order placement. Only ever called by the TradeEngine's
        single consumer for this user, so everything below is race-free —
        no other execution for this user can be interleaved with it."""
        self._last_execution_refusal = None
        sig = self.signals.get(signal_id)
        if not sig or sig.status not in ("new", "approved"):
            self._last_execution_refusal = (
                "Signal is gone" if not sig
                else f"Signal is already '{sig.status}' — only a new signal can be entered")
            return None
        if sig.submitted_at is None:
            # First execution attempt for this signal — mark the moment it
            # was actually handed off for execution (this is what the
            # trade_request->broker_ack latency should be measured against,
            # not created_at, since candle-boundary waits can legitimately
            # add tens of seconds between "generated" and "submitted").
            sig.submitted_at = now_ts()
        now = now_ts()
        
        # ═══════════════════════════════════════════════════════════════ #
        # ADAPTIVE TTL: Signal validity time based on timeframe + regime
        # ═══════════════════════════════════════════════════════════════ #
        tf_seconds = TIMEFRAMES.get(sig.timeframe, 60.0)
        
        # Base TTL is longer than old calculation to prevent valid signals from expiring
        # 1m: 120s, 5m: 600s, 15m: 1800s (full candle + buffer)
        ttl_seconds = signal_ttl_seconds(sig.timeframe, sig.regime)
        signal_age = now - sig.created_at
        
        if signal_age > ttl_seconds:
            sig.status = "expired"
            logger.warning(
                f"[v0] Signal EXPIRED: {sig.asset} {sig.direction.value} | "
                f"Age: {signal_age:.1f}s (TTL: {ttl_seconds:.1f}s) | "
                f"Regime: {sig.regime}"
            )
            self._refuse(signal_id,
                         f"Entry window closed — signal is {signal_age:.0f}s old, TTL is {ttl_seconds:.0f}s")
            return None
        # ═══════════════════════════════════════════════════════════════ #
        # SIGNAL REVALIDATION - Check setup still valid before execution
        # ═══════════════════════════════════════════════════════════════ #
        time_since_gen_ms = (now - sig.created_at) * 1000
        # BUGFIX: this used to call get_enriched(sig.asset, sig.timeframe) —
        # wrong on two counts: (1) get_enriched(key, candles) needs an actual
        # candle list as the 2nd arg, not a timeframe string (passing the
        # timeframe string made candles_to_df iterate over its characters,
        # crashing with "string indices must be integers, not 'str'"), and
        # (2) it returns a (df, warm) tuple, not just the df. Fixed to fetch
        # real candles first and use the same "asset|tf" cache key convention
        # used everywhere else in this file.
        current_df = None
        htf_df = None
        try:
            # BUG FIX (2026-08): these two revalidation fetches used
            # execute_with_retry's defaults -- max_retries=3, i.e. FOUR
            # attempts at up to 25s each plus backoff, over 100s, all BEFORE
            # place_order is even reached.
            #
            # That is longer than every budget wrapped around this executor
            # (place_order 70s, unknown-outcome guard 75s, caller 90s), so a
            # slow candle feed made the guard fire and mark the order
            # UNRECONCILED -- "may already be live at the broker" -- when no
            # order had been sent at all. A false unknown-outcome then paused
            # trading and demanded manual reconciliation of something that
            # never happened. The user-visible symptom was exactly
            # "still in flight after 90s".
            #
            # This is a pre-trade sanity check with a hard deadline: if fresh
            # candles are not here in seconds the setup is stale anyway, so
            # one short attempt and move on. revalidate_signal already handles
            # current_df being None.
            cur_candles = await self._revalidation_candles(sig.asset, sig.timeframe)
            cur_edf, cur_warm = self._indicators.get_enriched(f"{sig.asset}|{sig.timeframe}", cur_candles)
            # get_enriched()'s last row is always the still-forming candle
            # (commit=False) — every check inside revalidate_signal reads
            # .iloc[-1], so without this trim it's evaluating against
            # mutating, not-yet-closed data on the very last gate before
            # money moves. Same trim already used in _evaluate_asset_signal
            # and _revalidate_signal for this reason.
            current_df = (cur_edf.iloc[:-1] if len(cur_edf) > 1 else cur_edf) if cur_warm else None
        except Exception as exc:
            logger.warning("[revalidation] current_df fetch failed for %s/%s: %s", sig.asset, sig.timeframe, exc)

        htf_tf = HTF_MAP.get(sig.timeframe)
        if htf_tf:
            try:
                htf_candles = await self._revalidation_candles(sig.asset, htf_tf)
                htf_edf, htf_warm = self._indicators.get_enriched(f"{sig.asset}|{htf_tf}", htf_candles)
                htf_df = (htf_edf.iloc[:-1] if len(htf_edf) > 1 else htf_edf) if htf_warm else None
            except Exception as exc:
                logger.debug("[revalidation] htf_df fetch failed for %s/%s: %s", sig.asset, htf_tf, exc)

        cached_regime_result = None
        try:
            rt_key = f"{sig.asset}|{getattr(sig, 'timeframe', None)}"
            cached_regime_result = self.regime_transition_engine.get(rt_key)._last_result
        except Exception:
            logger.debug("[regime_transition] cached lookup failed for %s", sig.asset, exc_info=True)
        revalidation = self.signal_validator.revalidate_signal(
            sig, current_df, htf_df, time_since_gen_ms,
            market_context=self.market_context if getattr(self.market_context.cfg, "enabled", False) else None,
            context_asset=sig.asset,
            context_timeframe=getattr(sig, "timeframe", None),
            regime_transition_result=cached_regime_result,
        )
        if getattr(self.runtime.trading, "decision_engine_enabled", False) and sig.decision_id:
            try:
                revalidation_decision = SignalDecision(
                    asset=sig.asset, direction=sig.direction, timeframe=sig.timeframe,
                    thesis=f"Revalidation stage for signal {sig.id}",
                    supporting_evidence=[e for e in revalidation.evidence if e.kind == "supporting"],
                    rejecting_evidence=[e for e in revalidation.evidence if e.kind == "rejecting"],
                    final_decision="accepted" if revalidation.is_valid else "rejected",
                    rejection_reason=None if revalidation.is_valid else revalidation.revalidation_reason,
                    parent_decision_id=sig.decision_id,
                    price=sig.price, duration=sig.duration, payout=sig.payout,
                )
                self._persist_decision(revalidation_decision, sig.asset)
            except Exception:
                logger.debug("[decision_engine] revalidation decision logging failed for %s", sig.asset, exc_info=True)
        self.pipeline_tracer.mark_stage(
            signal_id, PipelineStage.SIGNAL_REVALIDATE,
            data={"confidence_adjustment": revalidation.confidence_adjustment},
            error=None if revalidation.is_valid else revalidation.revalidation_reason,
        )

        if not revalidation.is_valid:
            logger.warning(
                f"[v0] Signal Revalidation FAILED: {sig.asset} {sig.direction.value} | "
                f"Reason: {revalidation.revalidation_reason} | "
                f"Warnings: {', '.join(revalidation.warnings[:2])}"
            )
            self._refuse(signal_id,
                         f"Setup no longer valid at entry: {revalidation.revalidation_reason}")
            return None
        
        # Apply confidence adjustment from revalidation
        if revalidation.confidence_adjustment != 0:
            old_conf = sig.confidence
            sig.confidence = max(0, min(100, sig.confidence + revalidation.confidence_adjustment))
            if abs(revalidation.confidence_adjustment) > 2:
                logger.info(
                    f"[v0] Confidence Adjusted: {sig.asset} | "
                    f"{old_conf:.0f} → {sig.confidence:.0f} ({revalidation.revalidation_reason})"
                )
        
        # Allow up to 2 trades per asset (1 CALL + 1 PUT simultaneously)
        # This enables better scalping and improves signal throughput
        # Safe check because the queue guarantees this is the only execution for this user
        asset_trades = [t for t in self.active_trades.values() if t.asset == sig.asset]
        # Was hardcoded to 2. Now configurable -- set risk.max_trades_per_asset
        # higher to stack more positions on one asset. Each of these is a
        # separate, confirmed order, so the count is always known.
        max_per_asset = max(1, int(getattr(self.runtime.risk, "max_trades_per_asset", 2)))
        
        if len(asset_trades) >= max_per_asset:
            # Check if we can allow different directions
            directions = {t.direction for t in asset_trades}
            if len(directions) >= 2:  # Already have both CALL and PUT
                logger.warning(
                    f"[v0] Signal BLOCKED: {sig.asset} already has {len(asset_trades)} "
                    f"trades (both directions active)"
                )
                self._refuse(signal_id,
                             f"{sig.asset} already has trades open in both directions")
                return None
        
        # Allow if different direction or under max
        if len(asset_trades) < max_per_asset:
            logger.debug(f"[v0] Allowing trade #{len(asset_trades)+1} on {sig.asset}")
        elif len(asset_trades) == max_per_asset and asset_trades[0].direction != sig.direction:
            logger.debug(f"[v0] Allowing {sig.direction.value} trade alongside existing {asset_trades[0].direction.value}")
        else:
            logger.warning(f"[v0] Signal BLOCKED: Max {max_per_asset} trades per asset reached on {sig.asset}")
            self._refuse(signal_id,
                         f"Max {max_per_asset} trades per asset reached on {sig.asset} — "
                         f"raise 'Max trades per asset' in Settings to allow more")
            return None
        rs = self.runtime.risk
        if rs.correlation_guard_enabled:
            open_pairs = [(t.asset, t.direction.value) for t in self.active_trades.values()]
            block = self.correlation_guard.blocks_trade(
                sig.asset, sig.direction.value, open_pairs, sig.timeframe,
            )
            self.pipeline_tracer.mark_stage(signal_id, PipelineStage.ASSET_CHECK, error=block)
            if block:
                await hub.broadcast(self.user_id, "notice", {"message": block})
                self._refuse(signal_id, block)
                return None
        gate = self.risk.can_trade(now_ts(), len(self.active_trades),
                                   max_concurrent=self.runtime.risk.max_concurrent_trades,
                                   balance=self.state.balance)
        self.pipeline_tracer.mark_stage(signal_id, PipelineStage.RISK_CHECK, error=None if gate.allowed else gate.reason)
        if not gate.allowed:
            logger.warning("[risk] %s Signal RISK REJECTED: %s | Reason: %s",
                           sig.direction.value.upper(), sig.asset, gate.reason)
            await hub.broadcast(self.user_id, "notice", {"message": gate.reason})
            self._refuse(signal_id, f"Risk gate: {gate.reason}")
            return None

        # P2: sig.payout was captured once at signal generation and may be
        # stale by execution time (a signal can sit for up to ~2x its
        # timeframe waiting on gates/boundary — OTC payouts especially can
        # move within seconds). Re-fetch and re-check the breakeven math
        # against the *current* payout before risking money.
        try:
            fresh_payout = await self.error_recovery.execute_with_retry(
                self.provider.get_payout, "get_payout", sig.asset, sig.timeframe,
            )
        except Exception as exc:
            fresh_payout = None
            logger.debug("[payout-recheck] get_payout failed for %s: %s — proceeding with stale value", sig.asset, exc)

        if fresh_payout and fresh_payout > 0:
            fresh_effective_threshold = self._effective_confidence_threshold(float(fresh_payout))
            if sig.confidence < fresh_effective_threshold:
                logger.info(
                    "[payout-recheck] %s payout moved %.1f%%->%.1f%% — confidence %d no longer "
                    "clears fresh breakeven threshold %.1f — aborting execution",
                    sig.asset, sig.payout, fresh_payout, sig.confidence, fresh_effective_threshold,
                )
                sig.status = "expired"
                await hub.broadcast(self.user_id, "notice", {
                    "message": f"{sig.asset} payout dropped {sig.payout:.0f}%->{fresh_payout:.0f}% — signal aborted"
                })
                self._refuse(signal_id,
                             f"Payout dropped {sig.payout:.0f}% -> {fresh_payout:.0f}% — trade aborted")
                return None
            sig.payout = float(fresh_payout)  # keep the trade record honest about what was actually paid

        sig.status = "executing"
        stake_mult, _ = self._adaptive_risk_multipliers(sig.asset, getattr(sig, "timeframe", None))
        amount = self.risk.stake_for(self.state.balance, confidence=sig.confidence, adaptive_multiplier=stake_mult)
        if amount <= 0:
            # stake_for() returns 0.0 when even the $1 floor would breach
            # risk.max_stake_pct_of_balance on the current balance. This is a
            # refusal, not a failure -- no order is attempted.
            sig.status = "new"
            self._refuse(signal_id,
                        f"Balance too low (${self.state.balance:.2f}) to fund a stake within the "
                        f"configured cap ({getattr(self.runtime.risk, 'max_stake_pct_of_balance', 25):.0f}% "
                        f"of balance) — raise the cap in Settings or add funds")
            return None
        self.latency.mark(signal_id, "trade_request")

        if getattr(self.runtime.trading, "shadow_mode_enabled", False):
            return await self._execute_shadow(sig)

        self.queue_persistence.add(
            sig.id, sig.asset, sig.direction.value, sig.confidence,
            sig.price, sig.timeframe, sig.created_at,
        )
        self.pipeline_tracer.mark_stage(
            sig.id, PipelineStage.QUEUE_ADD,
            data={"amount": amount, "stake_multiplier": stake_mult, "payout": sig.payout},
        )
        
        logger.info("[execute] %s Signal Executing: %s @ %s | Confidence: %.2f | Amount: %s | Queue depth: %s",
                    sig.direction.value.upper(), sig.asset, sig.price, sig.confidence, amount,
                    self.trade_engine.pending)

        try:
            # CRITICAL BUG FIX (2026-08): this went through
            # execute_with_retry(), which does up to max_retries + 1 = 4
            # attempts with backoff between them, and -- unlike every
            # get_candles call around it -- passed no `timeout`, so each
            # attempt was unbounded.
            #
            # Two separate problems, one of them serious:
            #
            #  1. MONEY. That retry loop re-sends a BUY after a timeout. A
            #     timeout means "we did not hear back", NOT "it didn't
            #     happen" -- the broker may well have accepted the first
            #     order. Retrying is how one signal silently becomes two
            #     live positions on a real account. Order placement is not
            #     idempotent and must never be retried automatically; that
            #     is exactly the rule this project already states.
            #
            #  2. TIME. 4 attempts x ~60s provider budget + backoff is
            #     minutes, so TradeEngine's 75s guard always fired first and
            #     reported "did not return in 75s" while the retry loop was
            #     still grinding through attempts underneath.
            #
            # One attempt, bounded, no retry. A failure here is reported as
            # a failure and the decision to try again belongs to a human.
            # Push the configured protocol shape onto the provider right before
            # the call, so changing it in Settings takes effect immediately.
            setattr(self.provider, "_order_time_mode",
                    getattr(self.runtime.trading, "order_time_mode", "TIMER"))
            oid, open_price = await asyncio.wait_for(
                self.provider.place_order(sig.asset, amount, sig.direction, sig.duration),
                timeout=ORDER_PLACE_TIMEOUT_SECONDS,
            )
        except OrderNotSent as exc:
            # The provider proved the order was never transmitted, so there is
            # nothing to reconcile -- this is an ordinary failed attempt, and
            # it must not be allowed to look like an unknown outcome.
            logger.warning("[execute] %s: order not sent (%s)", sig.asset, exc)
            sig.status = "new"
            self._finish_signal_tracking(sig.id, False, f"order not sent: {exc}")
            await hub.broadcast(self.user_id, "notice",
                                {"message": f"Order not placed for {sig.asset}: {exc}"})
            self._last_execution_refusal = f"Order was not sent: {exc}"
            return None
        except Exception as exc:
            # Classify error for recovery strategy
            error_msg = str(exc).lower()
            is_timeout = "timeout" in error_msg or "timed out" in error_msg
            is_rate_limit = "rate" in error_msg or "too many" in error_msg
            
            # Rate limit errors: strong signal to back off
            if is_rate_limit:
                sig.status = "new"  # Retry later
                logger.warning(
                    f"[v0] Rate limited on {sig.asset}: {exc} | "
                    f"Backing off (signal will retry)"
                )
            # Timeout: transient, retry
            elif is_timeout:
                sig.status = "new"  # Retry
                logger.warning(f"[v0] Timeout on {sig.asset}: {exc} | Retrying")
            # Other errors: check if recoverable
            else:
                sig.status = "new" if "connection" in error_msg else "rejected"
                logger.error(f"[v0] Order placement error on {sig.asset}: {exc}")
            
            logger.error("[execute] %s Signal FAILED: %s | Error: %s | Status: %s | Active trades: %d",
                         sig.direction.value.upper(), sig.asset, exc, sig.status, len(self.active_trades))
            self.pipeline_tracer.mark_stage(signal_id, PipelineStage.EXECUTION, error=str(exc))
            if sig.status != "new":
                # Terminal (won't be retried) -- stop tracking it as pending.
                self.queue_persistence.remove(sig.id)
            await hub.broadcast(self.user_id, "error", {"message": f"Order failed: {exc}"})
            self._refuse(signal_id, f"Broker rejected the order: {exc}")
            return None
        self.latency.mark(signal_id, "broker_ack")
        latency_breakdown = self.latency.finish(signal_id)
        if latency_breakdown:
            await hub.broadcast(self.user_id, "latency", {"asset": sig.asset, **latency_breakdown})
        sig.status = "executed"
        self.queue_persistence.remove(sig.id)
        contributing = [name for name, d in sig.strategy_votes.items() if d == sig.direction.value]
        features = self._build_feature_snapshot(sig, current_df, contributing, latency_breakdown)

        logger.info("[execute] \u2705 %s Signal EXECUTED: %s | Order ID: %s | Entry: %s | Strategies: %s",
                    sig.direction.value.upper(), sig.asset, oid, open_price or sig.price, ", ".join(contributing))
        log_event(
            logger, logging.INFO, "trade_executed",
            signal_id=signal_id, asset=sig.asset, timeframe=sig.timeframe, direction=sig.direction.value,
            order_id=str(oid), open_price=open_price or sig.price, amount=amount,
            confidence=sig.confidence, strategies=contributing,
            execution_latency_ms=features.execution_latency_ms if features else None,
            reason="cleared_revalidation_risk_gate_and_payout_recheck",
        )
        self.pipeline_tracer.mark_stage(
            signal_id, PipelineStage.EXECUTION,
            data={"order_id": str(oid), "open_price": open_price or sig.price, "amount": amount},
        )

        trade = TradeRecord(
            signal_id=sig.id, asset=sig.asset, direction=sig.direction, amount=amount,
            duration=sig.duration, is_demo=self.state.is_demo, status=TradeStatus.OPEN,
            open_price=open_price or sig.price, payout=sig.payout, confidence=sig.confidence,
            raw_confidence=sig.raw_confidence, regime=sig.regime,
            strategies=contributing, timeframe=sig.timeframe, broker_order_id=str(oid),
            features=features, decision_id=sig.decision_id,
        )
        self.active_trades[str(oid)] = trade

        # Shadow-validation only: compute what PatternRiskEngine would have
        # recommended for this trade and record it alongside the (not yet
        # known) outcome. This NEVER affects amount/confidence/execution
        # above -- it's computed and stored strictly after the trade is
        # already committed.
        #
        # Audit note (H-2, confirmed intentional -- not yet approved for
        # live gating): PatternRiskEngine and RecoveryEngine are both fully
        # built and scoring correctly, but neither is wired into any
        # pre-trade decision. pattern_risk_validation.py's own bar for
        # recommending enforcement -- non-overlapping Wilson confidence
        # intervals between flagged/not-flagged trades, not just a
        # point-estimate gap -- hasn't been asked to clear yet for this
        # account's data. Wiring `pr_assessment.recommend_reject` into the
        # gate above (or RecoveryEngine.assess() into the pause/resume
        # path) is a contained, well-scoped change once that call is made;
        # doing it silently as part of an unrelated fix is not.
        try:
            candidate = {
                "adx": features.adx_at_entry if features else None,
                "atr_pct": features.atr_pct_at_entry if features else None,
                "rsi": features.rsi_at_entry if features else None,
                "volatility_percentile": features.volatility_percentile if features else None,
                "volume_ratio": features.volume_ratio_at_entry if features else None,
                "regime": sig.regime, "session": features.session if features else None,
                "timeframe": sig.timeframe, "asset": sig.asset,
                "otc_or_live": ("OTC" if sig.is_otc else "Live"),
            }
            pr_assessment = self.pattern_risk_engine.assess(candidate, self.store.all())
            self.pattern_risk_validation.record(ValidationRecord(
                trade_id=trade.id, asset=sig.asset, timeframe=sig.timeframe, timestamp=now_ts(),
                score=pr_assessment.score, evidence_count=pr_assessment.evidence_count,
                recommend_reject=pr_assessment.recommend_reject,
                stake_multiplier=pr_assessment.recommended.stake_multiplier,
                confidence_delta=pr_assessment.recommended.confidence_delta,
            ))
            # Enforcement: off by default. When explicitly enabled, applies
            # to a random subset of trades sized by pattern_risk_rollout_pct
            # (0-100, canary-style gradual rollout) -- and even for trades
            # selected into that subset, only acts when the engine's own
            # highest-confidence signal (recommend_reject, which per its own
            # docstring requires multiple independent signals to agree, not
            # a single feature) fires. The order for THIS trade already
            # executed by this point in the pipeline (pattern-risk scoring
            # needs the full trade history + entry features, which aren't
            # available until here -- see the audit note above), so
            # enforcement can't retroactively block or resize it. What it
            # CAN safely and immediately do is extend this asset's cooldown
            # (bounded 1.0-1.8x via cooldown_multiplier, only ever lengthens
            # it), delaying the *next* signal on this asset -- and that only
            # has any effect at all when Precision Mode is also on, since
            # that's the only code path that reads asset cooldowns.
            enforcement_applied = False
            if (bool(getattr(self.runtime.trading, "pattern_risk_enforcement_enabled", False))
                    and pr_assessment.recommend_reject):
                rollout_pct = max(0.0, min(100.0, float(getattr(self.runtime.trading, "pattern_risk_rollout_pct", 0.0))))
                if rollout_pct > 0.0 and random.uniform(0.0, 100.0) < rollout_pct:
                    base_cooldown = float(getattr(self.runtime.risk, "precision_asset_cooldown_seconds", 1800))
                    extension = base_cooldown * pr_assessment.recommended.cooldown_multiplier
                    self.risk.extend_cooldown(sig.asset, extension, now_ts())
                    enforcement_applied = True
                    logger.info(
                        "[pattern-risk-enforcement] %s: recommend_reject=True (score=%.1f), rollout hit -- "
                        "cooldown extended by %.0fs (multiplier=%.2f)",
                        sig.asset, pr_assessment.score, extension, pr_assessment.recommended.cooldown_multiplier,
                    )
            self.pipeline_tracer.mark_stage(
                signal_id, PipelineStage.PATTERN_RISK,
                data={"score": pr_assessment.score, "evidence_count": pr_assessment.evidence_count,
                      "recommend_reject": pr_assessment.recommend_reject,
                      "shadow_only": not enforcement_applied, "enforcement_applied": enforcement_applied},
            )
        except Exception:
            logger.warning("[pattern-risk-validation] failed to record shadow assessment for trade %s", trade.id, exc_info=True)
        self.store.add(trade)
        self.state.active_trades = len(self.active_trades)
        
        # Log execution metrics for quality tracking
        self.signal_validator.log_execution_metric(
            signal_id=signal_id,
            generated_at=sig.created_at,
            submitted_at=sig.submitted_at or sig.created_at,
            executed_at=now_ts(),
            success=True,
            reason="executed",
            direction=sig.direction.value
        )
        
        await hub.broadcast(self.user_id, "trade_opened", trade.model_dump())
        await self.broadcast_state()
        return trade

    def skip_signal(self, signal_id: str) -> None:
        sig = self.signals.get(signal_id)
        if sig and sig.status == "new":
            sig.status = "skipped"
            self.latency.drop(signal_id)

    async def _bounded_check_result(self, oid: str):
        """One settlement check under a hard ceiling.

        RCA F8: `_result_loop` used to `await self.provider.check_result(oid)`
        bare, and pyquotex's `check_win` blocks for up to 300s before giving
        up. Because the checks ran one after another inside the `for` loop, a
        single slow confirmation stalled every other open trade behind it --
        at `max_concurrent_trades=3` one pass could take ~900s, so trades sat
        unresolved, `active_trades` stayed full, and `can_trade()` refused new
        signals on the max_concurrent gate long after those trades had really
        closed.

        Measured, not estimated: the settlement test module runs in ~2.6s with
        this ceiling in place and ~306s without it, because one case waits out
        the vendor's full 300s.

        Returns None when the outcome is not yet known, which the caller
        treats as "leave it pending" -- never as a loss.
        """
        # A result recovered from the broker's history at startup wins: the
        # websocket slot that would have delivered it is gone after a restart,
        # so waiting on check_win() for these orders can only time out (RCA F8).
        recovered = self._recovered_results.pop(str(oid), None)
        if recovered is not None:
            return recovered
        try:
            return await asyncio.wait_for(
                self.provider.check_result(oid), RESULT_CHECK_TIMEOUT
            )
        except asyncio.TimeoutError:
            log_event(
                logger, logging.WARNING, "result_check_timeout",
                user_id=self.user_id, order_id=str(oid),
                timeout_seconds=RESULT_CHECK_TIMEOUT,
                reason="settlement check exceeded its ceiling -- trade left "
                       "pending for the next pass",
            )
            return None

    async def _collect_results(self) -> List[tuple]:
        """Fetch every open trade's result CONCURRENTLY, then hand them back
        for sequential bookkeeping.

        Concurrency is applied only to the network wait. The bookkeeping that
        follows stays strictly one trade at a time in the caller, because it
        mutates shared state -- `risk.record_result`, `state.balance`,
        `active_trades`, the strategy manager -- and running that in parallel
        would trade a latency bug for a correctness one.
        """
        pending = list(self.active_trades.items())
        if not pending:
            return []
        outcomes = await asyncio.gather(
            *(self._bounded_check_result(oid) for oid, _ in pending),
            return_exceptions=True,
        )
        settled: List[tuple] = []
        for (oid, trade), outcome in zip(pending, outcomes):
            if isinstance(outcome, BaseException):
                logger.warning("check_result failed for trade %s: %s", oid, outcome)
                continue
            settled.append((oid, trade, outcome))
        return settled

    async def _result_loop(self) -> None:
        while self._running:
            # RCA F8: gather first, apply second. See _collect_results().
            for oid, trade, result in await self._collect_results():
                try:
                    if oid not in self.active_trades:
                        # Settled or cancelled while the checks were in flight.
                        continue
                    if not result:
                        continue
                    status = result["status"]
                    profit = result["profit"]
                    trade.status = TradeStatus(status)
                    trade.profit = profit
                    trade.close_price = result.get("close_price")
                    trade.closed_at = now_ts()
                    self.risk.record_result(status, profit, now_ts())
                    self.production_metrics.record_trade_result(
                        trade.signal_id, profit, won=(status == "win"),
                    )
                    try:
                        self.pattern_risk_validation.update_outcome(trade.id, status, profit)
                    except Exception:
                        logger.warning("[pattern-risk-validation] failed to update outcome for trade %s", trade.id, exc_info=True)
                    _, cooldown_mult = self._adaptive_risk_multipliers(trade.asset, getattr(trade, "timeframe", None))
                    base_cooldown = float(getattr(self.runtime.risk, "precision_asset_cooldown_seconds", 1800))
                    self.risk.record_asset_result(
                        trade.asset, status, now_ts(),
                        streak_limit=int(getattr(self.runtime.risk, "precision_asset_loss_streak_limit", 2)),
                        cooldown_seconds=base_cooldown * cooldown_mult,
                    )
                    self.state.balance = round(self.state.balance + profit, 2)
                    self.risk.update_equity(self.state.balance)
                    changed = self.strategy_manager.record_trade(
                        trade.strategies, status, profit, regime=getattr(trade, "regime", None),
                    )
                    if getattr(self.runtime.trading, "drift_detection_enabled", False) and status in ("win", "loss"):
                        try:
                            regime_for_drift = getattr(trade, "regime", None)
                            if regime_for_drift and trade.strategies:
                                drift_alerts = self.drift_detector.observe(
                                    trade.strategies, regime_for_drift, won=(status == "win"),
                                )
                                auto_trial_mode = bool(getattr(self.runtime.trading, "drift_auto_trial_mode", True))
                                auto_mute = bool(getattr(self.runtime.trading, "drift_auto_mute", False))
                                drift_weight = self.strategy_manager.drift_weight
                                for alert in drift_alerts:
                                    if not auto_trial_mode:
                                        # "Detect only" mode -- alert is logged/visible (already
                                        # done inside drift_detector.observe()) but no state change.
                                        continue
                                    severe = alert.magnitude >= 2.0 * self.drift_detector.threshold
                                    # Additive-only blend: when adaptive_learning_drift_weight is
                                    # explicitly set, an alert that ISN'T severe on magnitude alone
                                    # can still become severe if the strategy's current win rate is
                                    # ALSO already poor -- i.e. drift + poor performance co-occurring
                                    # is treated as more urgent than either signal alone. Can only
                                    # ADD a path to "severe", never remove the existing one above
                                    # (severe is already True -> stays True regardless of this).
                                    # With drift_weight left at its None default, this whole block
                                    # is skipped and severe is exactly the original expression.
                                    if not severe and drift_weight is not None and 0.0 <= float(drift_weight) <= 1.0:
                                        st_for_blend = self.strategy_manager._get(alert.strategy)
                                        if len(st_for_blend.results) >= self.strategy_manager.min_sample:
                                            current_wr = st_for_blend.win_rate(st_for_blend.results)
                                            mute_bar = self.strategy_manager.mute_win_rate
                                            wr_deficit = max(0.0, (mute_bar - current_wr) / mute_bar) if mute_bar > 0 else 0.0
                                            magnitude_ratio = min(1.0, alert.magnitude / (2.0 * self.drift_detector.threshold))
                                            blended = float(drift_weight) * magnitude_ratio + (1.0 - float(drift_weight)) * wr_deficit
                                            if blended >= 0.5:
                                                severe = True
                                                logger.info(
                                                    "[adaptive-learning] %s drift alert escalated to severe by "
                                                    "blended score %.2f (magnitude_ratio=%.2f win_rate_deficit=%.2f "
                                                    "drift_weight=%.2f)",
                                                    alert.strategy, blended, magnitude_ratio, wr_deficit, drift_weight,
                                                )
                                    if auto_mute and severe:
                                        st = self.strategy_manager._get(alert.strategy)
                                        if st.manual_override is None and st.status == "active":
                                            st.status = "muted"
                                            st.muted_at = time.time()
                                            st.mute_reason = (
                                                f"Severe drift detected in {alert.regime} regime "
                                                f"(magnitude {alert.magnitude:.1f}, {2*self.drift_detector.threshold:.1f}+ threshold)"
                                            )
                                            st.cooldown_seconds = self.strategy_manager.base_cooldown
                                            self.strategy_manager._save()
                                            log_event(
                                                logger, logging.WARNING, "strategy_forced_mute_by_drift",
                                                strategy=alert.strategy, regime=alert.regime, magnitude=alert.magnitude,
                                            )
                                        continue
                                    forced = self.strategy_manager.force_trial(
                                        alert.strategy,
                                        reason=(
                                            f"Drift detected in {alert.regime} regime "
                                            f"(Page-Hinkley threshold {alert.magnitude:.1f} crossed)"
                                        ),
                                    )
                                    if forced:
                                        log_event(
                                            logger, logging.WARNING, "strategy_forced_trial_by_drift",
                                            strategy=alert.strategy, regime=alert.regime,
                                        )
                                        await hub.broadcast(self.user_id, "notice", {
                                            "message": f"'{alert.strategy}' moved to trial — drift detected in {alert.regime} regime"
                                        })
                        except Exception:
                            logger.debug("[drift_detection] observe/force_trial failed for trade %s", trade.id, exc_info=True)
                    log_event(
                        logger, logging.INFO, "trade_result_learned",
                        trade_id=trade.id, signal_id=trade.signal_id, asset=trade.asset, timeframe=getattr(trade, "timeframe", None),
                        status=status, profit=profit, balance=self.state.balance,
                        strategies=trade.strategies, strategy_states_changed=changed,
                        reason="trade_closed_fed_into_risk_strategy_manager_and_pattern_risk_validation",
                    )
                    for name in changed:
                        st = next((s for s in self.strategy_manager.status_for_ui([name])), None)
                        if st and st["status"] == "muted":
                            await hub.broadcast(self.user_id, "notice", {
                                "message": f"'{name}' auto-muted — {st['mute_reason']}"
                            })
                        elif st and st["status"] == "active":
                            await hub.broadcast(self.user_id, "notice", {
                                "message": f"'{name}' passed its trial and is fully active again"
                            })
                    self._sync_counters()
                    self.store.update(trade)
                    del self.active_trades[oid]
                    self.state.active_trades = len(self.active_trades)
                    await hub.broadcast(self.user_id, "trade_closed", trade.model_dump())
                    try:
                        recovery = self.recovery_engine.assess(self.shadow_store.all_resolved())
                        await hub.broadcast(self.user_id, "analytics_update", {
                            "overall": self.production_metrics.get_overall_report(),
                            "health": self.production_metrics.get_health_indicators(),
                            "recovery": {
                                "recovery_score": recovery.recovery_score,
                                "sample_size": recovery.sample_size,
                                "significant": recovery.significant,
                                "wilson_lower": recovery.wilson_lower,
                                "wilson_upper": recovery.wilson_upper,
                                "resume_recommended": recovery.resume_recommended,
                                "note": recovery.note,
                            },
                        })
                        # Auto-resume: off by default. Only ever acts on a
                        # pause caused by the drawdown circuit breaker
                        # (RiskManager._check_drawdown -> pause("Drawdown
                        # circuit breaker: ...")) -- never a kill-switch or
                        # Telegram /pause, which are deliberate human safety
                        # actions this setting must not silently override.
                        # Evaluated against whatever shadow-trade sample is
                        # available (existing pending shadow trades keep
                        # resolving via _shadow_loop even while paused, but
                        # no *new* ones are created during a pause -- same
                        # gate as real trades -- so this reflects the most
                        # recent evidence available, not necessarily
                        # fresh-since-the-pause data).
                        if (bool(getattr(self.runtime.trading, "recovery_auto_resume_enabled", False))
                                and self.risk.paused
                                and (self.risk.pause_reason or "").startswith("Drawdown circuit breaker")
                                and recovery.significant and recovery.resume_recommended):
                            self.risk.resume()
                            logger.info(
                                "[recovery-auto-resume] Trading resumed automatically: recovery_score=%.1f "
                                "sample_size=%d wilson=[%.1f,%.1f] note=%s",
                                recovery.recovery_score, recovery.sample_size,
                                recovery.wilson_lower, recovery.wilson_upper, recovery.note,
                            )
                            await hub.broadcast(self.user_id, "notice", {
                                "message": f"Trading auto-resumed after drawdown pause -- Recovery Engine "
                                           f"score {recovery.recovery_score:.0f} (n={recovery.sample_size})"
                            })
                            await self.broadcast_state()
                    except Exception:
                        pass
                    if self.runtime.telegram.send_results:
                        await self.telegram.send(self._fmt_result(trade))
                    await self.broadcast_state()
                except Exception as exc:
                    # A single bad/unreachable trade id must never block every
                    # other open trade behind it in this dict, and must never
                    # go unnoticed — an unresolved trade sits in active_trades
                    # forever, which silently eats into max_concurrent_trades
                    # and can make the bot look "stopped" with no visible cause.
                    self.health["last_error"] = f"check_result({oid}): {exc}"
                    logger.warning("check_result failed for trade %s: %s", oid, exc)
            await asyncio.sleep(2.0)

    def _sync_counters(self) -> None:
        s, r = self.state, self.risk
        s.wins, s.losses, s.draws = r.wins, r.losses, r.draws
        s.consecutive_losses = r.consecutive_losses
        s.trades_today = r.trades_today
        s.daily_pnl = r.daily_pnl
        s.paused = r.paused
        s.pause_reason = r.pause_reason

    async def broadcast_state(self) -> None:
        self.state.last_update = now_ts()
        self.state.connected = self.provider.connected
        await hub.broadcast(self.user_id, "state", self.state_dict())

    def state_dict(self) -> dict:
        d = self.state.model_dump()
        d["win_rate"] = self.state.win_rate
        return d

    # -- controls ----------------------------------------------------------- #
    def kill_switch(self) -> None:
        self.runtime.trading.mode = "off"
        self.risk.pause("Kill switch")
        self._sync_counters()
        self.runtime.save(self.user_id)

    def resume(self) -> None:
        self.risk.resume()
        self._sync_counters()

    # -- formatting + telegram commands ------------------------------------ #
    @staticmethod
    def _fmt_signal(s: Signal) -> str:
        arrow = "🟢 CALL" if s.direction == Direction.CALL else "🔴 PUT"
        chips = "\n".join(f"• {r}" for r in s.reasons[:4])
        return (f"<b>📡 Signal</b> {arrow}\n"
                f"<b>{s.asset}</b> · {s.timeframe} · {s.duration}s\n"
                f"Confidence: <b>{s.confidence}%</b> · Payout: {s.payout}%\n{chips}\n"
                f"Reply /enter to take or /skip to ignore.")

    @staticmethod
    def _fmt_result(t: TradeRecord) -> str:
        icon = {"win": "✅ WIN", "loss": "❌ LOSS", "draw": "➖ DRAW"}.get(t.status.value, t.status.value)
        return (f"<b>{icon}</b> {t.asset} {t.direction.value.upper()}\n"
                f"Stake: ${t.amount} · P/L: <b>${t.profit:+.2f}</b>")

    async def _handle_command(self, cmd: str, args: List[str]) -> str:
        if cmd == "status":
            s = self.state
            return (f"Mode: {self.runtime.trading.mode} | {'DEMO' if s.is_demo else 'LIVE'}\n"
                    f"Balance: ${s.balance:.2f} | Day P/L: ${s.daily_pnl:+.2f}\n"
                    f"W/L/D: {s.wins}/{s.losses}/{s.draws} | Win rate: {s.win_rate}%\n"
                    f"{'⏸ Paused: ' + (s.pause_reason or '') if s.paused else '▶️ Active'}")
        if cmd == "balance":
            return f"Balance: ${self.state.balance:.2f} {self.state.currency}"
        if cmd in ("stop", "kill"):
            self.kill_switch()
            await self.broadcast_state()
            return "🛑 Kill switch ON. Auto-trading stopped."
        if cmd == "pause":
            self.risk.pause("Paused via Telegram")
            self._sync_counters()
            await self.broadcast_state()
            return "⏸ Paused."
        if cmd == "resume":
            self.resume()
            await self.broadcast_state()
            return "▶️ Resumed."
        if cmd in ("enter", "take"):
            newest = [s for s in self.signals.values() if s.status == "new"]
            if not newest:
                return "No pending signal to enter."
            newest.sort(key=lambda s: s.created_at)
            trade = await self.execute_signal(newest[-1].id)
            return f"Entered {trade.asset} {trade.direction.value.upper()} ${trade.amount}" if trade else "Could not enter (risk gate)."
        if cmd in ("skip", "exit"):
            newest = [s for s in self.signals.values() if s.status == "new"]
            if newest:
                self.skip_signal(newest[-1].id)
                return "Skipped latest signal."
            return "Nothing to skip."
        return "Commands: /status /balance /enter /skip /pause /resume /stop"


# NOTE: the old module-level singleton (`orchestrator` / `get_orchestrator()`)
# is gone — one Orchestrator instance now exists per logged-in user, created
# and cached by `OrchestratorRegistry` (see orchestrator_registry.py). This
# is the core of the multi-user refactor: nothing in this class is a global
# any more, so N users run fully isolated instances of everything above.
