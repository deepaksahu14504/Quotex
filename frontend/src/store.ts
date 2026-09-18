import { create } from "zustand";
import { api, setUnauthorizedHandler } from "./api";
import { getStoredUser, getToken, login as loginApi, logout as logoutApi, register as registerApi, type AuthUser } from "./auth";
import { sound } from "./sound";
import type { AnalyticsUpdate, AssetPipelineSnapshot, EngineState, PipelineStageMetric, RuntimeSettings, Signal, Trade } from "./types";

interface Toast {
  id: number;
  kind: "info" | "win" | "loss" | "warn";
  message: string;
}

// WebSocket reconnect: exponential backoff with a ceiling + jitter, so a
// dropped connection recovers on its own without hammering the server.
const WS_BASE_DELAY = 1000;
const WS_MAX_DELAY = 30000;
const CLIENT_PING_INTERVAL = 15000;

interface AppState {
  user: AuthUser | null;
  authChecked: boolean;
  authError: string | null;
  authBusy: boolean;

  state: EngineState | null;
  settings: RuntimeSettings | null;
  signals: Signal[];
  trades: Trade[];
  toasts: Toast[];
  wsConnected: boolean;

  // Pipeline Visualization: pure display state, never written to except by
  // the WS handler below and the one-time initial-load fetch in init().
  // Nothing here is computed client-side.
  pipelineSnapshots: Record<string, AssetPipelineSnapshot>; // key: "asset|timeframe"
  pipelineLiveTraces: Record<string, { asset: string; timeframe: string; direction: string; startedAt: number; stages: PipelineStageMetric[]; completed: boolean; successful: boolean }>;
  analytics: AnalyticsUpdate | null;

  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => void;

  init: () => Promise<void>;
  connectWS: () => void;
  setSettings: (s: RuntimeSettings) => Promise<void>;
  refreshTrades: () => Promise<void>;
  pushToast: (kind: Toast["kind"], message: string) => void;
  dismissToast: (id: number) => void;
}

function notify(title: string, body: string) {
  try {
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification(title, { body, silent: true });
    }
  } catch {
    /* ignore */
  }
}

let toastSeq = 1;
let ws: WebSocket | null = null;
let wsRetryDelay = WS_BASE_DELAY;
let wsRetryTimer: ReturnType<typeof setTimeout> | null = null;
let wsPingTimer: ReturnType<typeof setInterval> | null = null;
let wsGeneration = 0; // bumps on logout/manual disconnect so stale reconnect attempts no-op

function teardownWS() {
  wsGeneration++;
  if (wsRetryTimer) clearTimeout(wsRetryTimer);
  if (wsPingTimer) clearInterval(wsPingTimer);
  wsRetryTimer = null;
  wsPingTimer = null;
  if (ws) {
    ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
    try {
      ws.close();
    } catch {
      /* ignore */
    }
  }
  ws = null;
}

export const useStore = create<AppState>((set, get) => ({
  user: getStoredUser(),
  authChecked: false,
  authError: null,
  authBusy: false,

  state: null,
  settings: null,
  signals: [],
  trades: [],
  toasts: [],
  wsConnected: false,
  pipelineSnapshots: {},
  pipelineLiveTraces: {},
  analytics: null,

  async login(email, password) {
    set({ authBusy: true, authError: null });
    try {
      const user = await loginApi(email, password);
      set({ user, authBusy: false });
      await get().init();
    } catch (e) {
      set({ authBusy: false, authError: e instanceof Error ? e.message : "Login failed" });
      throw e;
    }
  },

  async register(email, password) {
    set({ authBusy: true, authError: null });
    try {
      const user = await registerApi(email, password);
      set({ user, authBusy: false });
      await get().init();
    } catch (e) {
      set({ authBusy: false, authError: e instanceof Error ? e.message : "Registration failed" });
      throw e;
    }
  },

  logout() {
    teardownWS();
    logoutApi();
    set({
      user: null, state: null, settings: null, signals: [], trades: [],
      wsConnected: false, authChecked: true,
    });
  },

  async init() {
    if (!getToken()) {
      set({ authChecked: true });
      return;
    }
    setUnauthorizedHandler(() => get().logout());
    try {
      const [state, settings, signals, trades] = await Promise.all([
        api.state(),
        api.settings(),
        api.signals(),
        api.trades(60),
      ]);
      sound.configure(settings.ux.sound_enabled, settings.ux.sound_volume);
      set({ state, settings, signals, trades, authChecked: true });
      get().connectWS();
      api.pipelineSnapshots().then((snaps) => set({ pipelineSnapshots: snaps })).catch(() => {});
    } catch {
      // 401s are handled globally via setUnauthorizedHandler (logs out);
      // any other failure still lets the UI render instead of hanging.
      set({ authChecked: true });
    }
  },

  connectWS() {
    const token = getToken();
    if (!token) return;
    teardownWS();
    const myGeneration = wsGeneration;

    const open = () => {
      if (myGeneration !== wsGeneration) return; // superseded by logout/newer connect
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
      ws = socket;

      socket.onopen = () => {
        wsRetryDelay = WS_BASE_DELAY; // reset backoff on a healthy connection
        set({ wsConnected: true });
        wsPingTimer = setInterval(() => {
          try {
            socket.send(JSON.stringify({ event: "ping" }));
          } catch {
            /* ignore */
          }
        }, CLIENT_PING_INTERVAL);
      };

      socket.onclose = (ev) => {
        set({ wsConnected: false });
        if (wsPingTimer) clearInterval(wsPingTimer);
        wsPingTimer = null;
        if (myGeneration !== wsGeneration) return;
        // 4401 = server rejected/expired the token — don't retry forever,
        // send the user back to login instead of spinning silently.
        if (ev.code === 4401) {
          get().logout();
          return;
        }
        wsRetryTimer = setTimeout(open, wsRetryDelay);
        wsRetryDelay = Math.min(wsRetryDelay * 2, WS_MAX_DELAY);
      };

      socket.onerror = () => {
        try {
          socket.close();
        } catch {
          /* ignore */
        }
      };

      socket.onmessage = (ev) => {
        const { event, data } = JSON.parse(ev.data);
        const s = get();
        switch (event) {
          case "ping":
            try {
              socket.send(JSON.stringify({ event: "pong" }));
            } catch {
              /* ignore */
            }
            break;
          case "pong":
            break;
          case "state":
            set({ state: data });
            break;
          case "signal": {
            const sig = data as Signal;
            set({ signals: [sig, ...s.signals].slice(0, 40) });
            sound.play("signal");
            if (s.settings?.ux.notifications_enabled)
              notify("New signal", `${sig.asset} ${sig.direction.toUpperCase()} · ${sig.confidence}%`);
            s.pushToast("info", `Signal: ${sig.asset} ${sig.direction.toUpperCase()} (${sig.confidence}%)`);
            break;
          }
          case "trade_opened": {
            const t = data as Trade;
            set({ trades: [t, ...s.trades].slice(0, 80) });
            s.pushToast("info", `Trade opened: ${t.asset} ${t.direction.toUpperCase()} $${t.amount}`);
            break;
          }
          case "trade_closed": {
            const t = data as Trade;
            set({ trades: [t, ...s.trades.filter((x) => x.id !== t.id)].slice(0, 80) });
            if (t.status === "win") {
              sound.play("win");
              s.pushToast("win", `WIN ${t.asset} +$${t.profit.toFixed(2)}`);
            } else if (t.status === "loss") {
              sound.play("loss");
              s.pushToast("loss", `LOSS ${t.asset} -$${Math.abs(t.profit).toFixed(2)}`);
            }
            break;
          }
          case "otp_required":
            sound.play("alert");
            s.pushToast("warn", "Quotex needs a PIN code — check your email");
            break;
          case "notice":
            s.pushToast("warn", data.message);
            break;
          case "error":
            s.pushToast("warn", data.message);
            break;
          case "pipeline_snapshot": {
            const snap = data as AssetPipelineSnapshot;
            const key = `${snap.asset}|${snap.timeframe}`;
            set({ pipelineSnapshots: { ...s.pipelineSnapshots, [key]: snap } });
            break;
          }
          case "pipeline_trace_update": {
            const { signal_id, event: traceEvent, ...payload } = data as { signal_id: string; event: string; [k: string]: unknown };
            const existing = s.pipelineLiveTraces[signal_id];
            if (traceEvent === "trace_started") {
              const trimmed = { ...s.pipelineLiveTraces };
              const keys = Object.keys(trimmed);
              if (keys.length >= 60) {
                // Drop the oldest completed trace to bound memory -- an
                // unbounded session could otherwise accumulate one entry
                // per signal attempt indefinitely.
                const oldestCompleted = keys
                  .filter((k) => trimmed[k].completed)
                  .sort((a, b) => trimmed[a].startedAt - trimmed[b].startedAt)[0];
                if (oldestCompleted) delete trimmed[oldestCompleted];
              }
              const next = {
                ...trimmed,
                [signal_id]: {
                  asset: payload.asset as string, timeframe: payload.timeframe as string,
                  direction: payload.direction as string, startedAt: payload.created_at as number,
                  stages: [], completed: false, successful: false,
                },
              };
              set({ pipelineLiveTraces: next });
            } else if (traceEvent === "stage_update" && existing) {
              const stage = payload as unknown as PipelineStageMetric;
              set({
                pipelineLiveTraces: {
                  ...s.pipelineLiveTraces,
                  [signal_id]: { ...existing, stages: [...existing.stages, stage] },
                },
              });
            } else if (traceEvent === "trace_completed" && existing) {
              set({
                pipelineLiveTraces: {
                  ...s.pipelineLiveTraces,
                  [signal_id]: { ...existing, completed: true, successful: payload.successful as boolean },
                },
              });
            }
            break;
          }
          case "analytics_update":
            set({ analytics: data as AnalyticsUpdate });
            break;
        }
      };
    };

    open();
  },

  async setSettings(next) {
    const saved = await api.saveSettings(next);
    sound.configure(saved.ux.sound_enabled, saved.ux.sound_volume);
    set({ settings: saved });
  },

  async refreshTrades() {
    set({ trades: await api.trades(80) });
  },

  pushToast(kind, message) {
    const id = toastSeq++;
    set((st) => ({ toasts: [...st.toasts, { id, kind, message }] }));
    setTimeout(() => get().dismissToast(id), 4200);
  },

  dismissToast(id) {
    set((st) => ({ toasts: st.toasts.filter((t) => t.id !== id) }));
  },
}));
