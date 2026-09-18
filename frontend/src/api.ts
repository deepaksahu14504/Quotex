import type { AssetInfo, AssetPipelineSnapshot, BacktestRequest, BacktestResult, CalibrationStatus, Candle, DriftStatus, EnvSettings, Insights, PipelineHealth, PipelineReplayResponse, PipelineRestartResult, PipelineTraceDetail, RegimePerformance, RejectionReport, RuntimeSettings, Signal, Stats, StrategyPerformance, SystemStatus, Trade, EngineState, ValidationHealthRow, ValidationLeaderboard, ValidationProgress, SettingsAdvice, TelegramStatus } from "./types";
import { authHeaders, clearSession } from "./auth";

// Set by store.ts on init so a 401 from any API call (expired/invalid token)
// forces the user back to the login screen instead of the app quietly
// serving another user's data or throwing unhandled errors everywhere.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

function authedHeaders(extra?: Record<string, string>): Record<string, string> {
  return { ...authHeaders(), ...(extra || {}) };
}

async function handleAuthFailure(r: Response) {
  if (r.status === 401) {
    clearSession();
    onUnauthorized?.();
  }
}

/**
 * Pull the server's own explanation out of an error response.
 *
 * Both helpers used to throw `new Error("/api/... -> 409")`, discarding the
 * JSON `error` field the backend sends. Every refusal therefore reached the UI
 * as a status code with no reason attached -- which is why a failed "Enter
 * Trade" could only ever be reported as a number, if at all.
 */
async function errorFrom(r: Response, url: string): Promise<Error> {
  try {
    const body = await r.json();
    if (body && typeof body.error === "string" && body.error) return new Error(body.error);
  } catch {
    /* not JSON, or an empty body -- fall through to the status form */
  }
  return new Error(`${url} -> ${r.status}`);
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url, { headers: authedHeaders() });
  if (!r.ok) {
    await handleAuthFailure(r);
    throw await errorFrom(r, url);
  }
  return r.json();
}

async function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: authedHeaders(body !== undefined ? { "Content-Type": "application/json" } : undefined),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    await handleAuthFailure(r);
    throw await errorFrom(r, url);
  }
  const text = await r.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  state: () => get<EngineState>("/api/state"),
  time: () => get<{ utc: number }>("/api/time"),
  status: () => get<SystemStatus>("/api/status"),
  pipelineHealth: () => get<PipelineHealth>("/api/pipeline/health"),
  pipelineRestart: () => send<PipelineRestartResult>("/api/pipeline/restart", "POST"),
  settings: () => get<RuntimeSettings>("/api/settings"),
  saveSettings: (s: RuntimeSettings) => send<RuntimeSettings>("/api/settings", "PUT", s),
  assets: () => get<AssetInfo[]>("/api/assets"),
  candles: (asset: string, tf: string, count = 120) =>
    get<Candle[]>(`/api/candles?asset=${encodeURIComponent(asset)}&timeframe=${tf}&count=${count}`),
  signals: () => get<Signal[]>("/api/signals"),
  trades: (limit = 100) => get<Trade[]>(`/api/trades?limit=${limit}`),
  deleteTrade: (id: string) => send(`/api/trades/${id}`, "DELETE"),
  deleteAllTrades: () => send("/api/trades", "DELETE"),
  deleteSelectedTrades: (ids: string[]) => send("/api/trades/delete", "POST", { ids }),
  stats: () => get<Stats>("/api/stats"),
  insights: () => get<Insights>("/api/insights"),
  env: () => get<EnvSettings>("/api/env"),
  saveEnv: (e: Partial<EnvSettings> & { quotex_password?: string; telegram_bot_token?: string }) =>
    send<EnvSettings>("/api/env", "PUT", e),
  telegramTest: () => send<{ ok: boolean; error?: string }>("/api/telegram/test", "POST"),
  telegramStatus: () => get<TelegramStatus>("/api/telegram/status"),
  settingsRecommendations: () => get<SettingsAdvice>("/api/settings/recommendations"),
  applyRecommendation: (setting: string, value: unknown) =>
    send<{ ok: boolean; settings: RuntimeSettings }>("/api/settings/recommendations/apply", "POST", { setting, value }),
  saveBrokerCredentials: (c: { quotex_email: string; quotex_password: string; quotex_is_demo: boolean }) =>
    send("/api/broker/credentials", "PUT", c),
  brokerSession: () => get<{ connected: boolean; has_session: boolean; is_stale?: boolean }>("/api/broker/session"),
  /** Enter a signal. By default this waits for the plan's pinned entry
   *  instant; pass { enterNow: true } for the deliberate early override. */
  execute: (id: string, opts?: { enterNow?: boolean }) =>
    send(`/api/signals/${id}/execute`, "POST", { enter_now: opts?.enterNow ?? false }),
  skip: (id: string) => send(`/api/signals/${id}/skip`, "POST"),
  setMode: (mode: string) => send("/api/control/mode", "POST", { mode }),
  kill: () => send("/api/control/kill", "POST"),
  resume: () => send("/api/control/resume", "POST"),
  submitOtp: (code: string) => send<{ ok: boolean }>("/api/otp", "POST", { code }),
  switchAccount: (accountMode: "live" | "demo" | "tournament", tournamentId?: number | null) =>
    send<{ ok: boolean; message: string; state: EngineState }>("/api/control/switch-account", "POST", {
      account_mode: accountMode, tournament_id: tournamentId ?? null,
    }),
  runBacktest: (req: BacktestRequest) => send<BacktestResult>("/api/backtest/run", "POST", req),
  validationRun: () => send<{ ok: boolean; status: ValidationProgress }>("/api/validation/run", "POST"),
  validationStatus: () => get<ValidationProgress>("/api/validation/status"),
  validationHealth: () => get<{ matrix: ValidationHealthRow[] }>("/api/validation/health"),
  validationLeaderboard: () => get<ValidationLeaderboard>("/api/validation/leaderboard"),
  validationRuns: (limit = 20) => get<unknown[]>(`/api/validation/runs?limit=${limit}`),
  validationRunDetail: (id: string) => get<unknown>(`/api/validation/runs/${id}`),
  validationHistory: (asset: string, strategy: string, timeframe?: string) =>
    get<{ finished_at: number; health_score: number; win_rate: number; net_profit: number }[]>(
      `/api/validation/history?asset=${encodeURIComponent(asset)}&strategy=${encodeURIComponent(strategy)}${timeframe ? `&timeframe=${encodeURIComponent(timeframe)}` : ""}`,
    ),
  strategyPerformance: () => get<StrategyPerformance[]>("/api/strategies/performance"),
  setStrategyOverride: (name: string, override: "active" | "muted" | null) =>
    send<{ ok: boolean; status: StrategyPerformance }>(`/api/strategies/${encodeURIComponent(name)}/override`, "POST", { override }),
  calibration: () => get<CalibrationStatus>("/api/calibration"),
  regimeStatus: () => get<{ regimes: Record<string, string> }>("/api/regime/status"),
  regimePerformance: () => get<RegimePerformance[]>("/api/regime/performance"),
  pipelineSnapshots: () => get<Record<string, AssetPipelineSnapshot>>("/api/pipeline/snapshots"),
  pipelineReplay: (limit = 100) => get<PipelineReplayResponse>(`/api/pipeline/replay?limit=${limit}`),
  pipelineTrace: (signalId: string) => get<PipelineTraceDetail>(`/api/pipeline/trace/${encodeURIComponent(signalId)}`),
  rejectionsReport: () => get<RejectionReport>("/api/rejections/report"),
  driftStatus: () => get<DriftStatus>("/api/drift/status"),
};
