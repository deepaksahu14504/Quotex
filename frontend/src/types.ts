export type Direction = "call" | "put";
export type Mode = "auto" | "manual" | "off";

export interface EngineState {
  provider: string;
  connected: boolean;
  is_demo: boolean;
  mode: Mode;
  paused: boolean;
  pause_reason: string | null;
  balance: number;
  currency: string;
  starting_balance: number;
  daily_pnl: number;
  wins: number;
  losses: number;
  draws: number;
  consecutive_losses: number;
  trades_today: number;
  active_trades: number;
  otp_required: boolean;
  otp_verifying?: boolean;
  otp_message: string | null;
  win_rate: number;
  last_update: number;
  account_mode: "live" | "demo" | "tournament";
  tournament_id: number | null;
  tournament_supported: boolean;
}

export interface Signal {
  id: string;
  created_at: number;
  asset: string;
  direction: Direction;
  confidence: number;
  raw_confidence?: number;
  regime?: string;
  timeframe: string;
  duration: number;
  payout: number;
  price: number;
  reasons: string[];
  strategy_votes: Record<string, string>;
  status: string;
  expires_at?: number;
  trade_plan?: TradePlan | null;
}

export interface Trade {
  id: string;
  signal_id?: string;
  created_at: number;
  asset: string;
  direction: Direction;
  amount: number;
  duration: number;
  is_demo: boolean;
  status: "pending" | "open" | "win" | "loss" | "draw" | "error";
  open_price?: number;
  close_price?: number;
  profit: number;
  payout: number;
  confidence?: number;
  raw_confidence?: number;
  strategies?: string[];
  timeframe?: string;
  closed_at?: number;
  note?: string;
}

export interface AssetInfo {
  symbol: string;
  name: string;
  payout: number;
  is_open: boolean;
  is_otc: boolean;
}

export interface Candle {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface RuntimeSettings {
  provider?: "paper" | "pyquotex" | null;
  trading: {
    mode: Mode;
    is_demo: boolean;
    timeframe: string;
    duration_seconds: number;
    asset_whitelist: string[];
    max_scan_candidates?: number;
    min_candles_for_signal?: number;
    history_fetch_bars?: number;
    order_time_mode?: string;
    enabled_strategies: string[];
    multi_timeframe_confirmation: boolean;
    mtf_mode?: "soft" | "hard";
    candle_boundary_execution?: boolean;
    candle_boundary_max_wait?: number;
    signal_revalidation?: boolean;
    precision_mode_enabled?: boolean;
    precision_min_agreeing?: number;
    precision_max_opposing?: number;
    precision_require_regime_alignment?: boolean;
    dynamic_ensemble_enabled?: boolean;
    dynamic_ensemble_floor?: number;
    dynamic_ensemble_ceiling?: number;
    dynamic_ensemble_regime_blend?: number;
    regime_trend_adx?: number;
    regime_range_adx?: number;
    reval_regime_adx_floor?: number;
    adaptive_max_drift_pct?: number;
    shadow_mode_enabled?: boolean;
    account_mode?: "live" | "demo" | "tournament";
    tournament_id?: number | null;
    decision_engine_enabled?: boolean;
    decision_min_confidence_threshold?: number;
    decision_min_evidence_count?: number;
    decision_reject_weak_evidence?: boolean;
    decision_historical_probability_weight?: number;
    decision_regime_weight?: number;
    decision_calibration_weight?: number;
    drift_detection_enabled?: boolean;
    confidence_model_enabled?: boolean;
    confidence_model_shadow_mode?: boolean;
    drift_delta?: number;
    drift_threshold?: number;
    drift_min_samples?: number;
    drift_cooldown_seconds?: number;
    drift_auto_trial_mode?: boolean;
    drift_auto_mute?: boolean;
    confidence_model_min_training_samples?: number;
    confidence_model_prediction_threshold?: number;
    confidence_model_retrain_interval?: number;
    confidence_model_l2_regularization?: number;
    confidence_model_learning_rate?: number;
    confidence_model_max_iterations?: number;
    rejection_reconciliation_interval_seconds?: number;
    rejection_reconciliation_enabled?: boolean;
    rejection_min_sample_size?: number;
    regime_transition_detector_enabled?: boolean;
    strategy_min_sample?: number;
    strategy_trial_size?: number;
    strategy_rolling_window?: number;
    adaptive_learning_ewma_alpha?: number | null;
    adaptive_learning_drift_weight?: number | null;
    strategy_typical_payout_pct?: number;
    strategy_base_cooldown_seconds?: number;
    strategy_max_cooldown_seconds?: number;
    adaptive_wilson_confidence_level?: number;
    strategy_weight_overrides?: Record<string, number>;
    calibration_min_samples_total?: number;
    calibration_refresh_every_n_trades?: number;
    calibration_refresh_max_age_seconds?: number;
    calibration_holdout_validation_enabled?: boolean;
    calibration_rollback_on_worse_enabled?: boolean;
    pattern_risk_min_sample_size?: number;
    pattern_risk_enforcement_enabled?: boolean;
    pattern_risk_rollout_pct?: number;
    recovery_min_shadow_sample?: number;
    recovery_consistency_window?: number;
    recovery_win_rate_baseline?: number;
    recovery_score_threshold?: number;
    recovery_auto_resume_enabled?: boolean;
    dead_market_atr_pct_floor?: number;
    dead_market_adx_threshold?: number;
    dead_market_bb_ratio?: number;
    dead_market_rsi_adx_ceiling?: number;
    dead_market_rsi_high?: number;
    dead_market_rsi_low?: number;
    confluence_min_confirmations?: number;
    confluence_min_directional_edge?: number;
    adaptive_mode_enabled?: boolean;
    adaptive_rsi_enabled?: boolean;
    adaptive_adx_enabled?: boolean;
    adaptive_atr_enabled?: boolean;
    adaptive_volume_enabled?: boolean;
    adaptive_sr_enabled?: boolean;
    adaptive_confluence_enabled?: boolean;
    adaptive_signal_validation_enabled?: boolean;
    adaptive_precision_gate_enabled?: boolean;
    adaptive_risk_enabled?: boolean;
    regime_aware_strategy_intelligence_enabled?: boolean;
    calibration_enabled?: boolean;
    volatility_adjusted_duration?: boolean;
    duration_low_vol_atr_pct?: number;
    duration_high_vol_atr_pct?: number;
    duration_low_vol_mult?: number;
    duration_high_vol_mult?: number;
    duration_min_seconds?: number;
  };
  risk: {
    stake_mode: "fixed" | "percent";
    stake_amount: number;
    stake_percent: number;
    min_payout: number;
    confidence_threshold: number;
    payout_aware_threshold?: boolean;
    payout_edge_margin?: number;
    latency_aware_threshold?: boolean;
    latency_p95_ceiling_ms?: number;
    latency_threshold_widen?: number;
    stale_data_pause_seconds?: number;
    confidence_based_sizing?: boolean;
    confidence_size_floor?: number;
    confidence_size_ceiling?: number;
    precision_asset_loss_streak_limit?: number;
    precision_asset_cooldown_seconds?: number;
    daily_loss_limit: number;
    max_consecutive_losses: number;
    max_trades_per_day: number;
    max_concurrent_trades: number;
    max_trades_per_asset?: number;
    max_stake_pct_of_balance?: number;
    allow_duplicate_orders?: boolean;
    block_trading_on_unknown_order?: boolean;
    cooldown_seconds: number;
    martingale_enabled: boolean;
    martingale_multiplier: number;
    martingale_max_steps: number;
    drawdown_breaker_enabled: boolean;
    max_drawdown_pct: number;
    max_drawdown_amount: number;
    correlation_guard_enabled: boolean;
    correlation_threshold: number;
  };
  validation?: {
    enabled: boolean;
    daily_run_hour_utc: number;
    history_bars: number;
    walk_forward_folds: number;
    max_concurrency: number;
    max_assets_per_run: number;
    starting_balance: number;
    min_trades_for_score: number;
    active_score_min: number;
    trial_score_min: number;
    sync_strategy_manager: boolean;
  };
  ux: {
    sound_enabled: boolean;
    sound_volume: number;
    notifications_enabled: boolean;
    fog_intensity: number;
    reduced_motion: boolean;
    accent: string;
  };
  telegram: {
    enabled: boolean;
    bot_token: string | null;
    chat_id: string | null;
    send_signals: boolean;
    send_results: boolean;
    allow_commands: boolean;
  };
}

export type StatusLevel = "ok" | "warn" | "error" | "info";

export interface StatusCheck {
  key: string;
  label: string;
  status: StatusLevel;
  detail: string;
}

export interface SystemStatus {
  overall: StatusLevel;
  checks: StatusCheck[];
  uptime: number;
}

export interface PipelineTaskHealth {
  alive: boolean;
  stuck: boolean;
  restarts_recent: number;
  last_error: string | null;
  last_crash_at: number | null;
}

export interface PipelineHealth {
  running: boolean;
  stage: string;
  stuck: boolean;
  scan_age_seconds: number | null;
  tasks: Record<string, PipelineTaskHealth>;
}

export interface PipelineRestartResult {
  ok: boolean;
  restarted: boolean;
  message: string;
}

export interface EnvSettings {
  telegram_bot_token_set: boolean;
  telegram_chat_id: string;
  host: string;
  port: number;
  cors_origins: string;
}

export interface BrokerSessionInfo {
  connected: boolean;
  has_session: boolean;
  provider?: string;
  account_type?: "PRACTICE" | "REAL";
  login_at?: number | null;
  expires_at?: number | null;
  is_stale?: boolean;
  has_ssid?: boolean;
}

export interface Stats {
  total: number;
  wins: number;
  losses: number;
  win_rate: number;
  pnl: number;
  profit_factor: number | null;
  equity_curve: { t: number; equity: number }[];
}

export interface GroupStat {
  trades: number;
  wins: number;
  losses: number;
  pnl: number;
  win_rate: number;
}

export interface ComboStat extends GroupStat {
  strategy: string;
  asset: string;
}

export interface Insights {
  summary: {
    total: number;
    wins: number;
    losses: number;
    win_rate: number;
    net_pnl: number;
    profit_factor: number | null;
    expectancy: number;
    avg_confidence: number;
    current_streak: number;
    current_streak_type: string | null;
    longest_win_streak: number;
    longest_loss_streak: number;
    best_trade: Trade | null;
    worst_trade: Trade | null;
    avg_win: number;
    avg_loss: number;
    best_strategy: (GroupStat & { name: string }) | null;
    worst_strategy: (GroupStat & { name: string }) | null;
    most_profitable_asset: (GroupStat & { asset: string }) | null;
    least_profitable_asset: (GroupStat & { asset: string }) | null;
    best_combo: ComboStat | null;
  };
  top_combos: ComboStat[];
  by_strategy: (GroupStat & { name: string })[];
  by_asset: (GroupStat & { asset: string })[];
  by_timeframe: (GroupStat & { timeframe: string })[];
  by_direction: (GroupStat & { direction: string })[];
  by_hour: (GroupStat & { hour: number })[];
  by_confidence: (GroupStat & { bucket: string })[];
  form: string[];
  equity_curve: { t: number; equity: number }[];
}

export const STRATEGY_LABELS: Record<string, string> = {
  trend_pullback: "Trend Pullback",
  ema_ribbon: "EMA Ribbon",
  supertrend: "Supertrend",
  psar: "Parabolic SAR",
  adx_di: "ADX Trend",
  mean_reversion: "Mean Reversion",
  bollinger_bounce: "Bollinger Bounce",
  momentum_breakout: "Momentum Breakout",
  macd_cross: "MACD Cross",
  stoch_rsi: "Stochastic RSI",
  rsi_divergence: "RSI Divergence",
  vol_norm_reversion: "Vol-Norm Reversion",
  confluence_breakout: "Confluence Breakout",
  session_swing: "Session Swing",
  multi_confluence: "Multi-Factor Confluence",
  "(none)": "Manual / Other",
};

export interface RegimeCell {
  label: string;
  trades: number;
  win_rate: number;
  adjustment: number;
}

export interface RegimePerformance {
  name: string;
  overall_win_rate: number;
  regimes: Record<"trend" | "range" | "mixed", RegimeCell>;
}

export interface CalibrationPoint {
  bucket: string;
  trades: number;
  raw_win_rate: number;
  calibrated_win_rate: number;
}

export interface CalibrationStatus {
  built: boolean;
  total_samples: number;
  min_samples_required: number;
  points: CalibrationPoint[];
}

export interface StrategyPerformance {
  name: string;
  status: "active" | "watching" | "muted" | "trial";
  win_rate: number;
  trades: number;
  mute_reason: string | null;
  next_trial_at: number | null;
  manual_override: "active" | "muted" | null;
}

export interface BacktestTrade {
  id: string;
  opened_at: number;
  closed_at: number;
  asset: string;
  direction: "call" | "put";
  confidence: number;
  amount: number;
  payout_pct: number;
  entry_price: number;
  exit_price: number;
  profit: number;
  status: "win" | "loss" | "draw";
  strategies: string[];
  reasons: string[];
}

export interface BacktestStrategyBreakdown {
  name: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  pnl: number;
}

export interface BacktestSummary {
  asset: string;
  timeframe: string;
  bars_evaluated: number;
  from_ts: number;
  to_ts: number;
  starting_balance: number;
  ending_balance: number;
  total_trades: number;
  wins: number;
  losses: number;
  draws: number;
  win_rate: number;
  net_pnl: number;
  net_pnl_pct: number;
  profit_factor: number | null;
  max_drawdown: number;
  max_drawdown_pct: number;
  longest_win_streak: number;
  longest_loss_streak: number;
  stopped_early_reason: string | null;
}

export interface BacktestResult {
  summary: BacktestSummary;
  trades: BacktestTrade[];
  equity_curve: { t: number; equity: number }[];
  by_strategy: BacktestStrategyBreakdown[];
}

export interface BacktestRequest {
  asset: string;
  timeframe?: string;
  bars?: number;
  duration_seconds?: number;
  confidence_threshold?: number;
  enabled_strategies?: string[];
  starting_balance?: number;
  multi_timeframe_confirmation?: boolean;
  payout_pct?: number;
}

export interface ValidationProgress {
  status: string;
  run_id: string | null;
  started_at: number | null;
  finished_at: number | null;
  total_jobs: number;
  completed_jobs: number;
  failed_jobs?: number;
  percent?: number;
  current: string | null;
  error: string | null;
  last_run_at: number | null;
}

export interface ValidationHealthRow {
  asset: string;
  strategy: string;
  timeframe: string;
  score: number;
  status: "active" | "trial" | "muted";
  computed_at?: number;
  details?: Record<string, unknown>;
}

export interface ValidationResultRow {
  asset: string;
  strategy: string;
  timeframe: string;
  win_rate: number;
  total_trades: number;
  net_profit: number;
  profit_factor: number | null;
  max_drawdown: number;
  max_drawdown_pct: number;
  longest_win_streak: number;
  longest_loss_streak: number;
  confidence_accuracy: number;
  health_score: number;
  health_status: string;
  oos_folds: number;
  oos_consistency: number;
  by_regime: Record<string, { trades: number; wins: number; losses: number; pnl: number; win_rate: number }>;
  by_hour: Record<string, { trades: number; wins: number; losses: number; pnl: number; win_rate: number }>;
  by_session: Record<string, { trades: number; wins: number; losses: number; pnl: number; win_rate: number }>;
  equity_curve: { t: number; equity: number }[];
  drawdown_curve: { t: number; dd: number; dd_pct: number }[];
  details: Record<string, unknown>;
}

export interface ValidationLeaderboard {
  asset_rankings: { asset: string; avg_health: number; net_profit: number; trades: number }[];
  strategy_rankings: { strategy: string; avg_health: number; net_profit: number; trades: number }[];
  results: ValidationResultRow[];
}

// ── Pipeline Visualization ──────────────────────────────────────────────
// These mirror backend/app/engine/pipeline_events.py's AssetPipelineSnapshot
// and pipeline_tracer.py's PipelineTrace/StageMetric exactly -- the frontend
// only ever renders fields the backend already computed, nothing here is
// derived or recalculated client-side.

export type PipelineStageStatus = "waiting" | "running" | "passed" | "rejected" | "failed";

export interface AssetPipelineSnapshot {
  asset: string;
  timeframe: string;
  updated_at: number;
  ws_connected: boolean | null;
  last_candle_ts: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  indicators: Record<string, number>;
  trend_strength: number | null;
  volatility: number | null;
  regime: string | null;
  adaptive_thresholds: Record<string, number>;
  stage: PipelineStageStatus;
  stage_reason: string | null;
}

export interface PipelineStageMetric {
  stage: string;
  status: string;
  timestamp: number;
  duration_ms: number;
  data: Record<string, unknown>;
  error: string | null;
  warning: string | null;
}

export interface PipelineTraceDetail {
  signal_id: string;
  asset: string;
  timeframe: string;
  direction: string;
  created_at: string;
  total_duration_ms: number;
  successful: boolean;
  stages: PipelineStageMetric[];
  bugs_detected?: string[];
}

export interface PipelineTraceSummary {
  signal_id: string;
  asset: string;
  timeframe: string;
  direction: string;
  created_at: number;
  successful: boolean;
  stage_count: number;
  total_duration_ms: number;
  final_stage: string | null;
}

export interface PipelineReplayResponse {
  count: number;
  signals: PipelineTraceSummary[];
}

export interface RecoverySnapshot {
  recovery_score: number;
  sample_size: number;
  significant: boolean;
  wilson_lower: number;
  wilson_upper: number;
  resume_recommended: boolean;
  note: string;
}

export interface AnalyticsUpdate {
  overall: Record<string, unknown>;
  health: Record<string, unknown>;
  recovery: RecoverySnapshot | null;
}

// ── Decision Intelligence Engine ────────────────────────────────────────
// Mirrors backend/app/engine/{decision_engine,rejection_reconciliation,
// drift_detection,confidence_model}.py and schemas.py's EvidenceItem/
// SignalDecision exactly -- same "display only what the backend already
// computed" discipline as the rest of this file.

export interface EvidenceItem {
  source: string;
  direction: string | null;
  strength: number;
  kind: "supporting" | "rejecting";
  detail: string;
}

export interface RegimeSnapshot {
  regime: string;
  adx: number | null;
  adx_percentile: number | null;
  volatility_ratio: number | null;
}

export interface SignalDecision {
  id: string;
  created_at: number;
  asset: string;
  direction: string | null;
  timeframe: string;
  thesis: string;
  supporting_evidence: EvidenceItem[];
  rejecting_evidence: EvidenceItem[];
  base_rate: number | null;
  regime: RegimeSnapshot | null;
  recent_reliability: number | null;
  confidence_interval: [number, number] | null;
  raw_confidence: number | null;
  calibrated_confidence: number | null;
  shadow_confidence_model_prediction: number | null;
  regime_transition_state: string | null;
  regime_transition_probability: number | null;
  regime_transition_confidence: number | null;
  regime_stability_score: number | null;
  composite_confidence: number | null;
  final_decision: "accepted" | "rejected";
  rejection_reason: string | null;
  parent_decision_id: string | null;
}

export interface GateRegimeStat {
  gate: string;
  regime: string;
  total: number;
  wins: number;
  losses: number;
  win_rate: number;
  wilson_lower: number;
  wilson_upper: number;
  significant: boolean;
  total_pnl: number;
  verdict: string;
}

export interface RejectionReport {
  sample_size: number;
  min_sample_size: number;
  note: string;
  cells: GateRegimeStat[];
}

export interface DriftCell {
  strategy: string;
  regime: string;
  sample_count: number;
  cumulative_sum: number;
  threshold: number;
  total_alerts: number;
  last_alert_at: number | null;
}

export interface DriftStatus {
  cells: DriftCell[];
  threshold: number;
  delta: number;
  min_samples: number;
}

export interface TelegramStatus {
  enabled: boolean;
  has_token: boolean;
  has_chat_id: boolean;
  commands_enabled: boolean;
  poller_running: boolean;
  sender_running: boolean;
  queued: number;
  sent_count: number;
  failed_count: number;
  dropped_count: number;
  last_sent_at: number | null;
  last_error: string | null;
  last_error_at: number | null;
}

export interface SettingsRecommendation {
  id: string;
  setting: string | null;
  label: string;
  current: unknown;
  suggested: unknown;
  effect: "more_signals" | "fewer_signals" | "safety" | "reliability";
  evidence: "measured" | "observed" | "heuristic" | "needs_data";
  why: string;
  risk: string;
  applyable: boolean;
  priority: number;
  samples: number;
}

export interface SettingsAdvice {
  generated_at: number;
  data_confidence: string;
  resolved_samples: number;
  min_sample_size: number;
  typical_payout_pct: number;
  breakeven_win_rate: number;
  summary: string;
  recommendations: SettingsRecommendation[];
}

export interface TradePlan {
  direction: string;
  asset: string;
  entry_at: number;
  // The single instant both auto-trade and a manual Enter Trade press are
  // meant to honour -- see the backend TradePlan model for why this exists
  // as its own field rather than being re-derived from entry_at in the UI.
  scheduled_entry_at: number;
  entry_in_seconds: number;
  expiry_at: number;
  // "scheduled" = computed pre-trade (entry + duration). "broker_receipt" =
  // the broker's own confirmed settlement time, only present once an order
  // has actually been placed. build_trade_plan() always sends "scheduled".
  expiry_basis: "scheduled" | "broker_receipt";
  duration_seconds: number;
  entry_price_reference: number;
  stake: number;
  stake_is_estimate: boolean;
  payout_pct: number;
  potential_profit: number;
  potential_loss: number;
  waits_for_candle_open: boolean;
  candle_phase: string | null;
  valid_until: number | null;
  will_auto_execute: boolean;
  blocked_reason: string | null;
}
