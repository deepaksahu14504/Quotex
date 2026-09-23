import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { BrokerSessionInfo, EnvSettings, RuntimeSettings } from "../types";
import { Toggle } from "./ui";
import { ACCENT_PRESETS, DEFAULT_ACCENT } from "../accentPresets";
import { Recommendations } from "./Recommendations";

type ValidationSettings = NonNullable<RuntimeSettings["validation"]>;
type SettingsDraft = RuntimeSettings & { validation: ValidationSettings };

interface EnvDraft {
  host: string;
  port: number;
  cors_origins: string;
}

interface BrokerDraft {
  quotex_email: string;
  quotex_password: string;
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-accent)]/80">{title}</div>
      {subtitle && <div className="mb-2 text-[11px] text-faint">{subtitle}</div>}
      <div className="space-y-3 rounded-2xl bg-white/5 p-4">{children}</div>
    </div>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm text-white/80">{label}</div>
        {hint && <div className="text-[11px] text-faint">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function NumberInput({ value, onChange, step = 1, min, max }: { value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number }) {
  const clamp = (v: number) => {
    if (Number.isNaN(v)) return value;
    let out = v;
    if (min !== undefined) out = Math.max(min, out);
    if (max !== undefined) out = Math.min(max, out);
    return out;
  };
  return (
    <input
      type="number"
      step={step}
      min={min}
      max={max}
      value={value}
      onChange={(e) => onChange(clamp(+e.target.value))}
      className="nums w-24 rounded-lg bg-white/8 px-2 py-1 text-right text-sm outline-none ring-1 ring-white/10 focus:ring-[var(--color-accent)]/50"
    />
  );
}

function TextInput({ value, onChange, placeholder, type = "text" }: { value: string; onChange: (v: string) => void; placeholder?: string; type?: string }) {
  return (
    <input
      type={type}
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="w-44 rounded-lg bg-white/8 px-2 py-1 text-right text-sm outline-none ring-1 ring-white/10 focus:ring-[var(--color-accent)]/50"
    />
  );
}

const DEFAULT_VALIDATION: ValidationSettings = {
  enabled: true,
  daily_run_hour_utc: 3,
  history_bars: 2000,
  walk_forward_folds: 5,
  max_concurrency: 2,
  max_assets_per_run: 12,
  starting_balance: 1000,
  min_trades_for_score: 8,
  active_score_min: 80,
  trial_score_min: 55,
  sync_strategy_manager: true,
};

function mergeValidation(v?: RuntimeSettings["validation"]): ValidationSettings {
  return { ...DEFAULT_VALIDATION, ...v };
}

function withSettingDefaults(s: RuntimeSettings): SettingsDraft {
  return {
    ...s,
    risk: {
      ...s.risk,
      drawdown_breaker_enabled: s.risk.drawdown_breaker_enabled ?? true,
      max_drawdown_pct: s.risk.max_drawdown_pct ?? 15,
      max_drawdown_amount: s.risk.max_drawdown_amount ?? 0,
      correlation_guard_enabled: s.risk.correlation_guard_enabled ?? true,
      correlation_threshold: s.risk.correlation_threshold ?? 0.85,
    },
    validation: mergeValidation(s.validation),
  };
}

export function Settings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const settings = useStore((s) => s.settings);
  const setSettings = useStore((s) => s.setSettings);
  const engineState = useStore((s) => s.state);
  const pushToast = useStore((s) => s.pushToast);
  const [draft, setDraft] = useState<SettingsDraft | null>(settings ? withSettingDefaults(settings) : null);
  const [advanced, setAdvanced] = useState(false);
  const [switchingAccount, setSwitchingAccount] = useState(false);
  const [tournamentIdInput, setTournamentIdInput] = useState<string>(() => String(engineState?.tournament_id ?? ""));
  const [env, setEnv] = useState<EnvSettings | null>(null);
  const [envDraft, setEnvDraft] = useState<EnvDraft>({ host: "127.0.0.1", port: 8090, cors_origins: "" });

  const [brokerSession, setBrokerSession] = useState<BrokerSessionInfo | null>(null);
  const [brokerDraft, setBrokerDraft] = useState<BrokerDraft>({ quotex_email: "", quotex_password: "" });
  const [brokerSaving, setBrokerSaving] = useState(false);
  const [brokerError, setBrokerError] = useState<string | null>(null);
  const [brokerSaved, setBrokerSaved] = useState(false);

  useEffect(() => setDraft(settings ? withSettingDefaults(settings) : settings), [settings, open]);
  useEffect(() => {
    if (!open) return;
    api.env().then((e) => {
      setEnv(e);
      setEnvDraft({ host: e.host, port: e.port, cors_origins: e.cors_origins });
    }).catch(() => {});
    api.brokerSession().then(setBrokerSession).catch(() => {});
  }, [open]);
  if (!draft) return null;

  const saveBrokerCredentials = async () => {
    if (!brokerDraft.quotex_email.trim() || !brokerDraft.quotex_password) {
      setBrokerError("Email and password are required");
      return;
    }
    setBrokerSaving(true);
    setBrokerError(null);
    setBrokerSaved(false);
    try {
      await api.saveBrokerCredentials({
        quotex_email: brokerDraft.quotex_email.trim(),
        quotex_password: brokerDraft.quotex_password,
        quotex_is_demo: draft.trading.is_demo,
      });
      setBrokerDraft({ quotex_email: brokerDraft.quotex_email, quotex_password: "" });
      setBrokerSaved(true);
      api.brokerSession().then(setBrokerSession).catch(() => {});
    } catch (e) {
      setBrokerError(e instanceof Error ? e.message : "Failed to save credentials");
    } finally {
      setBrokerSaving(false);
    }
  };

  const [tgTest, setTgTest] = useState<string | null>(null);
  const patch = (p: Partial<SettingsDraft>) => setDraft({ ...draft, ...p });
  const patchValidation = (p: Partial<ValidationSettings>) =>
    patch({ validation: { ...draft.validation, ...p } });
  const patchEnv = (p: Partial<EnvDraft>) => setEnvDraft({ ...envDraft, ...p });
  const save = async () => {
    await setSettings(draft);
    if (env) {
      const payload: Record<string, unknown> = {};
      if (envDraft.host !== env.host) payload.host = envDraft.host;
      if (envDraft.port !== env.port) payload.port = envDraft.port;
      if (envDraft.cors_origins !== env.cors_origins) payload.cors_origins = envDraft.cors_origins;
      if (Object.keys(payload).length) {
        try {
          await api.saveEnv(payload);
        } catch {
          /* ignore */
        }
      }
    }
    onClose();
  };
  const askNotify = () => {
    if ("Notification" in window) Notification.requestPermission();
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-end justify-center sm:items-center"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
          <motion.div
            initial={{ y: 40, opacity: 0, scale: 0.98 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 40, opacity: 0 }}
            transition={{ type: "spring", stiffness: 320, damping: 32 }}
            className="glass glass-2 relative z-10 max-h-[88vh] w-full max-w-lg overflow-y-auto p-5 safe-bottom"
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-xl font-bold">Settings</h2>
              <button onClick={onClose} className="rounded-full bg-white/10 px-3 py-1 text-sm">Close</button>
            </div>

            {/* Recommended for you -- evidence-ranked settings advice */}
            <Section
              title="Recommended for you"
              subtitle="Ranked from your own resolved outcomes. Risk limits are never suggested in the looser direction, and threshold advice stays locked until there is a significant sample."
            >
              <Recommendations onApplied={() => setDraft({ ...draft })} />
            </Section>

            {/* Account */}
            <Section title="Account">
              <Row label="Data provider">
                <select
                  value={draft.provider ?? "paper"}
                  onChange={(e) => patch({ provider: e.target.value as RuntimeSettings["provider"] })}
                  className="rounded-lg bg-white/8 px-2 py-1 text-sm outline-none ring-1 ring-white/10"
                >
                  <option value="paper">Real data · Paper (Binance/Bybit)</option>
                  <option value="pyquotex">Quotex (PyQuotex)</option>
                </select>
              </Row>
              <Row label="Live trading" hint={draft.trading.is_demo ? "Demo — virtual money" : "⚠ Real money at risk"}>
                <Toggle checked={!draft.trading.is_demo} onChange={(v) => patch({ trading: { ...draft.trading, is_demo: !v } })} />
              </Row>

              <div className="rounded-lg bg-white/4 p-3">
                <div className="mb-2 flex items-center justify-between">
                  <div className="text-sm text-white/80">Trading Account</div>
                  {engineState && (
                    <span className="nums text-[11px] text-faint">
                      {engineState.currency} {engineState.balance.toFixed(2)}
                    </span>
                  )}
                </div>
                <div className="mb-2 flex gap-1.5">
                  {(["live", "demo", "tournament"] as const)
                    .filter((m) => m !== "tournament" || engineState?.tournament_supported)
                    .map((m) => (
                      <button
                        key={m}
                        type="button"
                        onClick={() => setDraft((d) => d && { ...d, trading: { ...d.trading, account_mode: m } })}
                        className={`flex-1 rounded-lg px-2 py-1.5 text-xs font-medium capitalize transition ${
                          (draft.trading.account_mode ?? "demo") === m
                            ? "bg-[var(--color-accent)]/20 text-[var(--color-accent)] ring-1 ring-[var(--color-accent)]/40"
                            : "bg-white/5 text-faint hover:bg-white/8"
                        }`}
                      >
                        {m}
                      </button>
                    ))}
                </div>
                {!engineState?.tournament_supported && (
                  <div className="mb-2 text-[10.5px] text-faint">
                    Tournament mode isn't available on this connection's broker/provider.
                  </div>
                )}
                {draft.trading.account_mode === "tournament" && (
                  <div className="mb-2 space-y-1">
                    <TextInput
                      value={tournamentIdInput}
                      onChange={setTournamentIdInput}
                      placeholder="Tournament ID (from the Quotex platform)"
                    />
                    <div className="text-[10.5px] text-faint">
                      The broker doesn't provide a way to list tournaments automatically — find the ID under the
                      Tournaments section on the Quotex platform and paste it here.
                    </div>
                  </div>
                )}
                <button
                  type="button"
                  disabled={switchingAccount || (draft.trading.account_mode === "tournament" && !tournamentIdInput.trim())}
                  onClick={async () => {
                    setSwitchingAccount(true);
                    try {
                      const mode = draft.trading.account_mode ?? "demo";
                      const tid = mode === "tournament" ? Number(tournamentIdInput) : null;
                      const res = await api.switchAccount(mode, tid);
                      pushToast(res.ok ? "info" : "warn", res.message);
                    } catch (e) {
                      pushToast("warn", e instanceof Error ? e.message : "Account switch failed");
                    } finally {
                      setSwitchingAccount(false);
                    }
                  }}
                  className="w-full rounded-lg bg-[var(--color-accent)]/15 px-3 py-1.5 text-xs font-semibold text-[var(--color-accent)] disabled:opacity-40"
                >
                  {switchingAccount ? "Switching…" : `Switch to ${draft.trading.account_mode ?? "demo"}`}
                </button>
                {engineState && engineState.account_mode !== (draft.trading.account_mode ?? "demo") && (
                  <div className="mt-1.5 text-[10.5px] text-faint">
                    Currently active: <span className="capitalize text-white/70">{engineState.account_mode}</span>
                    {engineState.tournament_id ? ` (#${engineState.tournament_id})` : ""}
                  </div>
                )}
              </div>
              {draft.provider === "pyquotex" && (
                <>
                  <div className={`rounded-lg px-3 py-2 text-[11px] ${
                    brokerSession?.connected && !brokerSession?.is_stale
                      ? "bg-[var(--color-up)]/10 text-[var(--color-up)]"
                      : brokerSession?.connected
                      ? "bg-[var(--color-gold)]/10 text-[var(--color-gold)]"
                      : "bg-white/5 text-faint"
                  }`}>
                    {brokerSession?.connected
                      ? brokerSession.is_stale
                        ? `Saved session may be stale — will re-login automatically (${brokerSession.account_type ?? "PRACTICE"})`
                        : `Session active · ${brokerSession.account_type ?? "PRACTICE"}`
                      : "No Quotex session saved yet — enter your credentials below"}
                  </div>
                  <Row label="Quotex email">
                    <TextInput value={brokerDraft.quotex_email} onChange={(v) => { setBrokerDraft({ ...brokerDraft, quotex_email: v }); setBrokerSaved(false); }} placeholder="you@example.com" />
                  </Row>
                  <Row label="Quotex password" hint={brokerSession?.has_session ? "Saved · leave blank to keep" : "Not set"}>
                    <TextInput type="password" value={brokerDraft.quotex_password} onChange={(v) => { setBrokerDraft({ ...brokerDraft, quotex_password: v }); setBrokerSaved(false); }} placeholder={brokerSession?.has_session ? "••••••••" : "password"} />
                  </Row>
                  <div className="rounded-lg bg-white/5 px-3 py-2 text-[11px] text-faint">
                    First login emails you a PIN — the app will prompt for it. Credentials are encrypted at rest and used only for your own account; no other user can see or trigger a login with them.
                  </div>
                  {brokerError && (
                    <div className="rounded-lg bg-[var(--color-down)]/10 px-3 py-2 text-[11px] text-[var(--color-down)]">{brokerError}</div>
                  )}
                  <button
                    onClick={saveBrokerCredentials}
                    disabled={brokerSaving}
                    className="w-full rounded-xl bg-white/8 py-2 text-sm font-semibold text-white/90 ring-1 ring-white/10 transition active:scale-[0.98] disabled:opacity-60"
                  >
                    {brokerSaving ? "Connecting…" : brokerSaved ? "Saved ✓ — reconnecting" : "Save & connect Quotex account"}
                  </button>
                </>
              )}
            </Section>

            {/* Core trade controls */}
            <Section title="Trade & Risk">
              <Row label="Stake per trade ($)">
                <NumberInput value={draft.risk.stake_amount} onChange={(v) => patch({ risk: { ...draft.risk, stake_amount: v, stake_mode: "fixed" } })} />
              </Row>
              <Row label="Daily loss limit ($)" hint="Auto-stops for the day">
                <NumberInput value={draft.risk.daily_loss_limit} onChange={(v) => patch({ risk: { ...draft.risk, daily_loss_limit: v } })} />
              </Row>
              <Row label="Max open trades">
                <NumberInput value={draft.risk.max_concurrent_trades} onChange={(v) => patch({ risk: { ...draft.risk, max_concurrent_trades: v } })} />
              </Row>
              <Row label="Confidence threshold" hint={`Only trade signals ≥ ${draft.risk.confidence_threshold}%`}>
                <NumberInput value={draft.risk.confidence_threshold} onChange={(v) => patch({ risk: { ...draft.risk, confidence_threshold: v } })} />
              </Row>
              <Row label="Max stake (% of balance)" hint="Hard ceiling on ANY single trade's stake, regardless of stake mode or martingale. Prevents a losing streak from sizing a trade larger than the account.">
                <NumberInput value={draft.risk.max_stake_pct_of_balance ?? 25} onChange={(v) => patch({ risk: { ...draft.risk, max_stake_pct_of_balance: v } })} />
              </Row>
              <Row label="Max trades per asset" hint="How many positions can be open on one asset at once. Each is a separate confirmed order.">
                <NumberInput value={draft.risk.max_trades_per_asset ?? 2} onChange={(v) => patch({ risk: { ...draft.risk, max_trades_per_asset: v } })} />
              </Row>
              <Row label="Allow duplicate orders" hint="Lets the SAME signal be sent again while its first order is still in flight. You won't know if it placed 1 or 2 until they settle.">
                <Toggle checked={draft.risk.allow_duplicate_orders ?? false} onChange={(v) => patch({ risk: { ...draft.risk, allow_duplicate_orders: v } })} />
              </Row>
              <Row label="Pause on unknown order" hint="If an order doesn't return in time, stop trading until you've checked it at the broker. Off = keep trading, reconcile yourself.">
                <Toggle checked={draft.risk.block_trading_on_unknown_order ?? true} onChange={(v) => patch({ risk: { ...draft.risk, block_trading_on_unknown_order: v } })} />
              </Row>
              <Row label="Order protocol" hint="How the order is sent to Quotex. If orders time out with no broker reply, try the other one.">
                <select
                  value={draft.trading.order_time_mode ?? "TIMER"}
                  onChange={(e) => patch({ trading: { ...draft.trading, order_time_mode: e.target.value } })}
                  className="rounded-lg bg-white/8 px-2 py-1 text-[12px] ring-1 ring-white/12"
                >
                  <option value="TIMER">TIMER (optionType 100)</option>
                  <option value="TIME">TIME (optionType 3)</option>
                </select>
              </Row>
              <Row label="Min candles for a signal" hint="Below this an asset is skipped. 120 is what Quotex serves; 55 = indicators return a number but it has not converged.">
                <NumberInput value={draft.trading.min_candles_for_signal ?? 120} onChange={(v) => patch({ trading: { ...draft.trading, min_candles_for_signal: v } })} />
              </Row>
              <Row label="Assets scanned per cycle" hint="Raises signal count without lowering any quality bar. Higher = longer scan cycles.">
                <NumberInput value={draft.trading.max_scan_candidates ?? 12} onChange={(v) => patch({ trading: { ...draft.trading, max_scan_candidates: v } })} />
              </Row>
              <Row label="Multi-timeframe confirmation" hint="Weigh signals by the higher-TF trend">
                <Toggle checked={draft.trading.multi_timeframe_confirmation} onChange={(v) => patch({ trading: { ...draft.trading, multi_timeframe_confirmation: v } })} />
              </Row>
              {draft.trading.multi_timeframe_confirmation && (
                <Row label="MTF mode" hint="Soft: HTF only nudges confidence. Hard: adds a 2nd (HHTF) tier that can veto a signal outright on strong counter-trend.">
                  <select
                    value={draft.trading.mtf_mode ?? "soft"}
                    onChange={(e) => patch({ trading: { ...draft.trading, mtf_mode: e.target.value as "soft" | "hard" } })}
                  >
                    <option value="soft">Soft (confidence nudge)</option>
                    <option value="hard">Hard (HHTF veto)</option>
                  </select>
                </Row>
              )}
              <Row label="Candle-boundary execution" hint="Wait for the next candle open before firing, instead of mid-candle">
                <Toggle checked={!!draft.trading.candle_boundary_execution} onChange={(v) => patch({ trading: { ...draft.trading, candle_boundary_execution: v } })} />
              </Row>
              {draft.trading.candle_boundary_execution && (
                <>
                  <Row label="Max boundary wait (sec)" hint="If waiting would exceed this, execute immediately instead">
                    <NumberInput value={draft.trading.candle_boundary_max_wait ?? 30} onChange={(v) => patch({ trading: { ...draft.trading, candle_boundary_max_wait: v } })} />
                  </Row>
                  <Row label="Signal revalidation" hint="Re-check the signal on fresh data at candle open; skip the trade if it no longer confirms">
                    <Toggle checked={draft.trading.signal_revalidation ?? true} onChange={(v) => patch({ trading: { ...draft.trading, signal_revalidation: v } })} />
                  </Row>
                </>
              )}

              <Section title="Precision Mode">
                <Row label="Enable Precision Mode" hint="Quality-over-quantity gate: requires strong strategy agreement and no counter-votes before a signal fires">
                  <Toggle checked={!!draft.trading.precision_mode_enabled} onChange={(v) => patch({ trading: { ...draft.trading, precision_mode_enabled: v } })} />
                </Row>
                {draft.trading.precision_mode_enabled && (
                  <>
                    <Row label="Min agreeing strategies" hint="How many strategies must vote the winning direction">
                      <NumberInput value={draft.trading.precision_min_agreeing ?? 3} onChange={(v) => patch({ trading: { ...draft.trading, precision_min_agreeing: v } })} />
                    </Row>
                    <Row label="Max opposing strategies" hint="Counter-vote veto -- 0 means any opposing vote blocks the signal">
                      <NumberInput value={draft.trading.precision_max_opposing ?? 0} onChange={(v) => patch({ trading: { ...draft.trading, precision_max_opposing: v } })} />
                    </Row>
                    <Row label="Require regime alignment" hint="At least one agreeing strategy must fit the current trend/range regime">
                      <Toggle checked={draft.trading.precision_require_regime_alignment ?? true} onChange={(v) => patch({ trading: { ...draft.trading, precision_require_regime_alignment: v } })} />
                    </Row>
                    <Row label="Per-asset loss-streak cooldown" hint="Consecutive losses on ONE asset before it's sidelined">
                      <NumberInput value={draft.risk.precision_asset_loss_streak_limit ?? 2} onChange={(v) => patch({ risk: { ...draft.risk, precision_asset_loss_streak_limit: v } })} />
                    </Row>
                    <Row label="Asset cooldown duration (sec)">
                      <NumberInput value={draft.risk.precision_asset_cooldown_seconds ?? 1800} onChange={(v) => patch({ risk: { ...draft.risk, precision_asset_cooldown_seconds: v } })} />
                    </Row>
                  </>
                )}
              </Section>

              <Section title="Signal Gate Sensitivity" subtitle="Controls how strict the pre-signal filters are. Defaults match the bot's original hardcoded behavior -- lower the ADX/edge values or raise the BB ratio to let more signals through.">
                <Row label="Min ATR% (volatility floor)" hint="Below this, market is considered too flat to trade at all">
                  <NumberInput value={draft.trading.dead_market_atr_pct_floor ?? 0.03} step={0.01} onChange={(v) => patch({ trading: { ...draft.trading, dead_market_atr_pct_floor: v } })} />
                </Row>
                <Row label="Chop ADX threshold" hint="ADX below this + BB width collapse = rejected as no-trend chop. Lower = fewer rejections">
                  <NumberInput value={draft.trading.dead_market_adx_threshold ?? 14} onChange={(v) => patch({ trading: { ...draft.trading, dead_market_adx_threshold: v } })} />
                </Row>
                <Row label="Chop BB width ratio" hint="BB width vs its 20-period average, used with the ADX check above. Higher = fewer rejections">
                  <NumberInput value={draft.trading.dead_market_bb_ratio ?? 0.55} step={0.05} onChange={(v) => patch({ trading: { ...draft.trading, dead_market_bb_ratio: v } })} />
                </Row>
                <Row label="Extreme-RSI ADX ceiling" hint="Only apply the extreme-RSI whipsaw filter below this ADX">
                  <NumberInput value={draft.trading.dead_market_rsi_adx_ceiling ?? 20} onChange={(v) => patch({ trading: { ...draft.trading, dead_market_rsi_adx_ceiling: v } })} />
                </Row>
                <Row label="Extreme RSI high" hint="RSI above this (with low ADX) is rejected as reversion risk">
                  <NumberInput value={draft.trading.dead_market_rsi_high ?? 85} onChange={(v) => patch({ trading: { ...draft.trading, dead_market_rsi_high: v } })} />
                </Row>
                <Row label="Extreme RSI low" hint="RSI below this (with low ADX) is rejected as reversion risk">
                  <NumberInput value={draft.trading.dead_market_rsi_low ?? 15} onChange={(v) => patch({ trading: { ...draft.trading, dead_market_rsi_low: v } })} />
                </Row>
                <Row label="Min agreeing strategies (confluence)" hint="Required when any strategy votes the opposite direction. Lower = fewer rejections">
                  <NumberInput value={draft.trading.confluence_min_confirmations ?? 2} onChange={(v) => patch({ trading: { ...draft.trading, confluence_min_confirmations: v } })} />
                </Row>
                <Row label="Min directional edge" hint="winning score / (winning + losing) floor when there's opposition. Lower = fewer rejections">
                  <NumberInput value={draft.trading.confluence_min_directional_edge ?? 0.60} step={0.01} onChange={(v) => patch({ trading: { ...draft.trading, confluence_min_directional_edge: v } })} />
                </Row>
              </Section>

              <Section title="Sentiment Adjustment" subtitle="A small, bounded confidence nudge from trader sentiment. It can never pick or reverse a direction, never replaces the OHLC/indicator/confluence signal, and is skipped entirely (adjustment 0) when sentiment is missing, stale or the provider call fails -- so turning it on cannot block a signal. ON by default.">
                <Row label="Sentiment Adjustment" hint={(draft.market_data?.sentiment_enabled ?? true) ? "On - bounded sentiment nudge applies" : "Off - sentiment is completely bypassed"}>
                  <Toggle
                    checked={draft.market_data?.sentiment_enabled ?? true}
                    onChange={(v) => patch({ market_data: { ...draft.market_data, sentiment_enabled: v } })}
                  />
                </Row>
                <Row label="Max adjustment (confidence points)" hint="Hard cap on how far sentiment may move confidence, in either direction">
                  <NumberInput value={draft.market_data?.sentiment_max_confidence_adjustment ?? 4} step={0.5} onChange={(v) => patch({ market_data: { ...draft.market_data, sentiment_max_confidence_adjustment: v } })} />
                </Row>
                <Row label="Max age (seconds)" hint="Sentiment older than this is ignored, not penalised">
                  <NumberInput value={draft.market_data?.sentiment_max_age_seconds ?? 120} onChange={(v) => patch({ market_data: { ...draft.market_data, sentiment_max_age_seconds: v } })} />
                </Row>
                <Row label="Min strength" hint="Bias weaker than this is ignored (0.10 = 10 percentage points)">
                  <NumberInput value={draft.market_data?.sentiment_min_strength ?? 0.1} step={0.01} onChange={(v) => patch({ market_data: { ...draft.market_data, sentiment_min_strength: v } })} />
                </Row>
              </Section>

              <Section title="Adaptive Intelligence" subtitle="Learns thresholds from each asset's own recent behavior instead of using one fixed number for everyone. Off by default -- turning it on only changes behavior for the specific toggles enabled below, and each one falls back cleanly to the original fixed behavior when switched off.">
                <Row label="Adaptive Mode" hint="Master switch -- must be on for any toggle below to do anything">
                  <Toggle checked={!!draft.trading.adaptive_mode_enabled} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_mode_enabled: v } })} />
                </Row>
                <Row label="Trend ADX threshold" hint="ADX at/above this value classifies the market as trending. Higher = stricter trend confirmation (fewer bars classified as trending, but more confident when it does). Always active -- this is the baseline regime classification used everywhere in the pipeline, independent of Adaptive Mode above.">
                  <NumberInput value={draft.trading.regime_trend_adx ?? 25.0} step={1} min={5} max={50} onChange={(v) => patch({ trading: { ...draft.trading, regime_trend_adx: v } })} />
                </Row>
                <Row label="Range ADX threshold" hint="ADX below this value classifies the market as sideways/ranging (between this and the Trend threshold above = 'mixed'). Lower = stricter sideways detection (fewer bars classified as ranging). Always active, independent of Adaptive Mode above.">
                  <NumberInput value={draft.trading.regime_range_adx ?? 18.0} step={1} min={5} max={50} onChange={(v) => patch({ trading: { ...draft.trading, regime_range_adx: v } })} />
                </Row>
                <Row label="Revalidation ADX floor" hint="Minimum trend strength (ADX) required immediately before trade execution -- if ADX has fallen below this AND dropped more than 30% versus 50 bars ago, the setup is treated as destabilizing and revalidation penalizes confidence accordingly. Always active whenever Signal Revalidation (below) is on, independent of Adaptive Mode above.">
                  <NumberInput value={draft.trading.reval_regime_adx_floor ?? 15.0} step={1} min={5} max={50} onChange={(v) => patch({ trading: { ...draft.trading, reval_regime_adx_floor: v } })} />
                </Row>
                {draft.trading.adaptive_mode_enabled && (
                  <>
                    <Row label="Max drift per update" hint="Maximum allowed market-behaviour drift before adaptive confidence reduction or stricter filtering begins -- caps how much any adaptive threshold (adaptive RSI/ADX/ATR/volume/S-R bounds) can shift in a single recompute, as a fraction of its own value (0.05 = 5% max change per update). Higher = adaptive thresholds can swing further per update (more reactive, less stable); lower = smoother but slower to reflect genuinely new conditions.">
                      <NumberInput value={draft.trading.adaptive_max_drift_pct ?? 0.05} step={0.01} min={0.01} max={1.0} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_max_drift_pct: v } })} />
                    </Row>
                    <Row label="Adaptive RSI" hint="Overbought/oversold bounds learned per asset">
                      <Toggle checked={draft.trading.adaptive_rsi_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_rsi_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive ADX" hint="Trend-strength floor learned per asset">
                      <Toggle checked={draft.trading.adaptive_adx_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_adx_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive ATR" hint="Volatility floor learned per asset">
                      <Toggle checked={draft.trading.adaptive_atr_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_atr_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive Volume" hint="Above/below-average volume bounds learned per asset">
                      <Toggle checked={draft.trading.adaptive_volume_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_volume_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive Support/Resistance" hint="Proximity band scales with each asset's own volatility">
                      <Toggle checked={draft.trading.adaptive_sr_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_sr_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive Confluence" hint="Required strategy agreement scales with trend strength">
                      <Toggle checked={draft.trading.adaptive_confluence_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_confluence_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive Signal Validation" hint="Pre-execution revalidation reuses the learned RSI bounds">
                      <Toggle checked={draft.trading.adaptive_signal_validation_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_signal_validation_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive Precision Gate" hint="Reserved -- not yet active, safe to leave on">
                      <Toggle checked={draft.trading.adaptive_precision_gate_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_precision_gate_enabled: v } })} />
                    </Row>
                    <Row label="Regime-aware Strategy Intelligence" hint="Strategy vote weights also factor in current-regime performance, not just overall">
                      <Toggle checked={!!draft.trading.regime_aware_strategy_intelligence_enabled} onChange={(v) => patch({ trading: { ...draft.trading, regime_aware_strategy_intelligence_enabled: v } })} />
                    </Row>
                    <Row label="Adaptive Risk" hint="Reserved -- not yet active, safe to leave on">
                      <Toggle checked={draft.trading.adaptive_risk_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_risk_enabled: v } })} />
                    </Row>
                    <Row label="Calibration" hint="Confidence calibration learned from your own trade history, per regime">
                      <Toggle checked={draft.trading.calibration_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, calibration_enabled: v } })} />
                    </Row>
                  </>
                )}
              </Section>

              <Section title="Dynamic Weighted Ensemble">
                <Row label="Enable Dynamic Ensemble" hint="Scales each strategy's vote weight by its own recent live win-rate">
                  <Toggle checked={!!draft.trading.dynamic_ensemble_enabled} onChange={(v) => patch({ trading: { ...draft.trading, dynamic_ensemble_enabled: v } })} />
                </Row>
                {draft.trading.dynamic_ensemble_enabled && (
                  <>
                    <Row label="Weight floor" hint="Minimum multiplier for an underperforming strategy">
                      <NumberInput value={draft.trading.dynamic_ensemble_floor ?? 0.7} step={0.05} onChange={(v) => patch({ trading: { ...draft.trading, dynamic_ensemble_floor: v } })} />
                    </Row>
                    <Row label="Weight ceiling" hint="Maximum multiplier for an outperforming strategy">
                      <NumberInput value={draft.trading.dynamic_ensemble_ceiling ?? 1.3} step={0.05} onChange={(v) => patch({ trading: { ...draft.trading, dynamic_ensemble_ceiling: v } })} />
                    </Row>
                    {draft.trading.regime_aware_strategy_intelligence_enabled && (
                      <Row label="Regime blend" hint="Dynamically adjusts strategy weights according to the detected market regime (trend, range, breakout, reversal). Higher values make the ensemble adapt more aggressively. 0.0 = ignore regime entirely (pure global win-rate, original behavior); 1.0 = fully regime-specific weighting. Only takes effect because 'Regime-aware Strategy Intelligence' is on (Adaptive Intelligence section) -- backend clamps this to [0, 1] regardless of what's entered.">
                        <NumberInput value={draft.trading.dynamic_ensemble_regime_blend ?? 0.4} step={0.05} min={0} max={1} onChange={(v) => patch({ trading: { ...draft.trading, dynamic_ensemble_regime_blend: v } })} />
                      </Row>
                    )}
                  </>
                )}
              </Section>

              <Section title="Shadow Mode">
                <Row label="Enable Shadow Mode" hint="Simulate trades against live prices -- no real orders are ever placed while this is on">
                  <Toggle checked={!!draft.trading.shadow_mode_enabled} onChange={(v) => patch({ trading: { ...draft.trading, shadow_mode_enabled: v } })} />
                </Row>
              </Section>

              <Section title="Decision Intelligence Engine" subtitle="Instrumentation and shadow-only learning components. Every toggle here is off by default and, when on, only records/observes -- none of them change which signals fire, their confidence, or any gate, EXCEPT the evidence gate below, which is explicitly a real accept/reject gate and is called out as such.">
                <Row label="Decision logging" hint="Records a structured record of every signal evaluation -- accepted or rejected -- with supporting/rejecting evidence, base rate, and regime context. Pure instrumentation: enables the Rejection Analysis panel below, and is what the confidence model (further down) trains on.">
                  <Toggle checked={!!draft.trading.decision_engine_enabled} onChange={(v) => patch({ trading: { ...draft.trading, decision_engine_enabled: v } })} />
                </Row>
                <Row label="Reject weak-evidence signals" hint="REAL GATE, not instrumentation: when on (and decision logging above is also on), a signal with too little supporting evidence or too low a Decision Intelligence confidence estimate is rejected outright -- same as the precision gate or confidence-threshold gate elsewhere in the pipeline. Off by default; leave off unless you specifically want this extra filter.">
                  <Toggle checked={!!draft.trading.decision_reject_weak_evidence} onChange={(v) => patch({ trading: { ...draft.trading, decision_reject_weak_evidence: v } })} />
                </Row>
                {draft.trading.decision_reject_weak_evidence && (
                  <div className="ml-4 space-y-2 border-l border-white/10 pl-4">
                    <Row label="Min. supporting evidence" hint="Minimum number of supporting-evidence items required, or the signal is rejected. Default 1 (i.e. effectively no floor beyond having at least one).">
                      <NumberInput value={draft.trading.decision_min_evidence_count ?? 1} step={1} min={1} max={10} onChange={(v) => patch({ trading: { ...draft.trading, decision_min_evidence_count: v } })} />
                    </Row>
                    <Row label="Min. decision confidence" hint="Minimum Decision Intelligence confidence estimate (composite/calibrated/raw, whichever is available) required, 0-100, or the signal is rejected. Default 50.">
                      <NumberInput value={draft.trading.decision_min_confidence_threshold ?? 50} step={5} min={0} max={100} onChange={(v) => patch({ trading: { ...draft.trading, decision_min_confidence_threshold: v } })} />
                    </Row>
                  </div>
                )}
                <Row label="Drift detection" hint="Sequential (Page-Hinkley) statistical test per strategy/regime for a sustained win-rate shift -- flags it faster than waiting for the rolling-window mute check alone. Independent of decision logging above. Hyperparameters are literature-standard starting points, not yet validated against this account's own variance -- treat early alerts as provisional.">
                  <Toggle checked={!!draft.trading.drift_detection_enabled} onChange={(v) => patch({ trading: { ...draft.trading, drift_detection_enabled: v } })} />
                </Row>
                {draft.trading.drift_detection_enabled && (
                  <div className="ml-4 space-y-2 border-l border-white/10 pl-4">
                    <Row label="Sensitivity (delta)" hint="Minimum change magnitude worth accumulating evidence for. LOWER = more sensitive (accumulates evidence from smaller shifts, alerts sooner but with more false alarms). Default 0.005 matches the module's own literature-standard starting point.">
                      <NumberInput value={draft.trading.drift_delta ?? 0.005} step={0.001} min={0.0001} max={0.1} onChange={(v) => patch({ trading: { ...draft.trading, drift_delta: v } })} />
                    </Row>
                    <Row label="Alert threshold" hint="Accumulated evidence required before an alert actually fires. LOWER = fewer candles needed to trigger a forced trial (more reactive, more false positives). HIGHER = requires more sustained evidence (slower, more conservative). Default 25.0.">
                      <NumberInput value={draft.trading.drift_threshold ?? 25.0} step={1} min={5} max={100} onChange={(v) => patch({ trading: { ...draft.trading, drift_threshold: v } })} />
                    </Row>
                    <Row label="Minimum samples" hint="No drift detection permitted before this many resolved trades for a (strategy, regime) pair -- protects against declaring drift from a handful of noisy outcomes. Default 20.">
                      <NumberInput value={draft.trading.drift_min_samples ?? 20} step={1} min={5} max={200} onChange={(v) => patch({ trading: { ...draft.trading, drift_min_samples: v } })} />
                    </Row>
                  </div>
                )}
                <Row label="Data-driven confidence model" hint="Fits a logistic regression on realized outcomes (via decision logging above) to re-weight the existing confidence formula's own inputs. Shadow-only by default (its prediction is logged alongside the real confidence score that actually trades, not substituted in) -- see 'Promote out of shadow' below to change that. Needs 300+ resolved decisions before it produces anything.">
                  <Toggle checked={!!draft.trading.confidence_model_enabled} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_enabled: v } })} />
                </Row>
                {draft.trading.confidence_model_enabled && (
                  <Row label="Promote out of shadow" hint="REAL EFFECT, not instrumentation: when on, a trusted model prediction is blended 50/50 with the live calibrated confidence used for the accept/reject threshold, instead of only being logged. Bounded blend, not a full replacement. Off (shadow-only) by default -- leave off unless you've reviewed the shadow predictions in Insights/decision logs and are comfortable with them.">
                    <Toggle checked={!draft.trading.confidence_model_shadow_mode} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_shadow_mode: !v } })} />
                  </Row>
                )}
                {draft.trading.confidence_model_enabled && (
                  <div className="ml-4 space-y-2 border-l border-white/10 pl-4">
                    <Row label="Min. samples to train" hint="fit() refuses below this many resolved decisions -- not enough data to reliably estimate 10 model parameters. Default 200.">
                      <NumberInput value={draft.trading.confidence_model_min_training_samples ?? 200} step={10} min={50} max={2000} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_min_training_samples: v } })} />
                    </Row>
                    <Row label="Min. samples to trust" hint="predict_proba() returns nothing (silently falls back to the real, hand-tuned confidence formula) below this many decisions, even if the model has already been fit. Stricter than the training minimum above by design. Default 300.">
                      <NumberInput value={draft.trading.confidence_model_prediction_threshold ?? 300} step={10} min={50} max={3000} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_prediction_threshold: v } })} />
                    </Row>
                    <Row label="Retrain every N trades" hint="How often the orchestrator triggers a refit as new outcomes accumulate. LOWER = model adapts faster to recent conditions but retrains (and its own noise) more often. Default 50.">
                      <NumberInput value={draft.trading.confidence_model_retrain_interval ?? 50} step={5} min={10} max={500} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_retrain_interval: v } })} />
                    </Row>
                    <Row label="L2 regularization" hint="Penalizes large model weights to reduce overfitting risk. HIGHER = more conservative/simpler fitted model (safer with limited data). LOWER = model can fit the training data more closely (higher overfitting risk). Default 1.0.">
                      <NumberInput value={draft.trading.confidence_model_l2_regularization ?? 1.0} step={0.1} min={0.01} max={10} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_l2_regularization: v } })} />
                    </Row>
                    <Row label="Learning rate" hint="Gradient descent step size during fitting. Too high risks the fit not converging; too low needs more epochs to converge. Default 0.3, tuned for standardized inputs -- don't raise this much without also checking train_log_loss stays sane.">
                      <NumberInput value={draft.trading.confidence_model_learning_rate ?? 0.3} step={0.05} min={0.01} max={1.0} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_learning_rate: v } })} />
                    </Row>
                    <Row label="Training epochs" hint="Gradient descent iterations per fit. More = closer convergence but slower refits. Default 1000.">
                      <NumberInput value={draft.trading.confidence_model_max_iterations ?? 1000} step={100} min={100} max={10000} onChange={(v) => patch({ trading: { ...draft.trading, confidence_model_max_iterations: v } })} />
                    </Row>
                  </div>
                )}
                <div className="ml-0 mt-1 space-y-2">
                  <div className="text-[11px] font-medium text-white/70">Rejection Reconciliation <span className="text-faint">(shares Decision logging's toggle above -- nothing to reconcile without it)</span></div>
                  <Row label="Reconciliation check interval" hint="How often (seconds) the background loop checks for rejected signals whose hypothetical outcome duration has elapsed and can now be resolved. Lower = more responsive Rejection Analysis panel, marginally more overhead. Default 3.0s.">
                    <NumberInput value={draft.trading.rejection_reconciliation_interval_seconds ?? 3.0} step={0.5} min={1} max={30} onChange={(v) => patch({ trading: { ...draft.trading, rejection_reconciliation_interval_seconds: v } })} />
                  </Row>
                  <Row label="Minimum sample size" hint="A (gate, regime) cell in the Rejection Analysis report needs at least this many resolved rejections before its Wilson-bound verdict is shown as meaningful rather than 'insufficient data'. Default 30.">
                    <NumberInput value={draft.trading.rejection_min_sample_size ?? 30} step={5} min={10} max={200} onChange={(v) => patch({ trading: { ...draft.trading, rejection_min_sample_size: v } })} />
                  </Row>
                </div>
              </Section>

              <Section title="Adaptive Regime Transition Detector" subtitle="Classifies the current market regime from a weighted confluence of signals and tracks transitions between regimes. Off by default. INFLUENCES confidence and can request a short entry cooldown during a chaotic transition -- never forces a direction, never overrides an existing gate.">
                <Row label="Enable detector" hint="With this off, the orchestrator never instantiates or calls the detector -- zero effect on any signal. This module was rewritten (numerically-stable running stats, real multi-timeframe confirmation) and, unlike the settings above, doesn't currently expose its internal thresholds for tuning -- leave off until you've watched its behavior via the logs on this account's own data.">
                  <Toggle checked={!!draft.trading.regime_transition_detector_enabled} onChange={(v) => patch({ trading: { ...draft.trading, regime_transition_detector_enabled: v } })} />
                </Row>
              </Section>

              <Section title="Adaptive Strategy Performance" subtitle="Sample-size floors and cooldown durations for the per-strategy mute/trial/reactivate state machine (engine.strategy_manager). Defaults match the values that were previously hardcoded, so leaving these alone changes nothing.">
                <Row label="Min. sample before acting" hint="No mute/reactivate decision is made for a strategy before this many resolved trades. Default 30.">
                  <NumberInput value={draft.trading.strategy_min_sample ?? 30} step={5} min={5} max={200} onChange={(v) => patch({ trading: { ...draft.trading, strategy_min_sample: v } })} />
                </Row>
                <Row label="Reactivation trial size" hint="Trades given during a reactivation trial before deciding to fully reactivate or re-mute. Default 20.">
                  <NumberInput value={draft.trading.strategy_trial_size ?? 20} step={5} min={5} max={100} onChange={(v) => patch({ trading: { ...draft.trading, strategy_trial_size: v } })} />
                </Row>
                <Row label="Rolling window" hint="Trades considered for an active strategy's live win rate. Default 50.">
                  <NumberInput value={draft.trading.strategy_rolling_window ?? 50} step={10} min={10} max={500} onChange={(v) => patch({ trading: { ...draft.trading, strategy_rolling_window: v } })} />
                </Row>
                <Row label="Fast-reacting EWMA mute check" hint="REAL, ADDITIVE effect: maintains an exponentially-weighted win-rate estimate per strategy that reacts faster to a sudden drop than the rolling-window check above (which needs the full min-sample size and statistical confirmation first). Can only mute a strategy sooner -- never prevents or delays the existing check. Off by default.">
                  <Toggle checked={draft.trading.adaptive_learning_ewma_alpha != null} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_learning_ewma_alpha: v ? 0.3 : null } })} />
                </Row>
                {draft.trading.adaptive_learning_ewma_alpha != null && (
                  <Row label={`EWMA alpha ${draft.trading.adaptive_learning_ewma_alpha.toFixed(2)}`} hint="Smoothing factor, 0-1. Higher = reacts faster to the most recent trades but noisier; lower = smoother but slower to catch a real shift. 0.3 is a reasonable starting point.">
                    <input type="range" min={0.05} max={0.9} step={0.05} value={draft.trading.adaptive_learning_ewma_alpha}
                           onChange={(e) => patch({ trading: { ...draft.trading, adaptive_learning_ewma_alpha: +e.target.value } })}
                           className="w-32 accent-[var(--color-accent)]" />
                  </Row>
                )}
                <Row label="Blend drift severity with win rate" hint="REAL, ADDITIVE effect: when a drift alert's magnitude alone isn't quite 'severe', this can still escalate it to severe if the strategy's current win rate is ALSO already poor -- co-occurring signals treated as more urgent together. Can only add a path to escalation, never remove the existing magnitude-only one. Needs Drift Detection enabled above to have any effect. Off by default.">
                  <Toggle checked={draft.trading.adaptive_learning_drift_weight != null} onChange={(v) => patch({ trading: { ...draft.trading, adaptive_learning_drift_weight: v ? 0.5 : null } })} />
                </Row>
                {draft.trading.adaptive_learning_drift_weight != null && (
                  <Row label={`Drift weight ${draft.trading.adaptive_learning_drift_weight.toFixed(2)}`} hint="0 = escalation decided entirely by current win-rate deficit; 1 = decided entirely by drift magnitude; 0.5 weighs them equally.">
                    <input type="range" min={0} max={1} step={0.05} value={draft.trading.adaptive_learning_drift_weight}
                           onChange={(e) => patch({ trading: { ...draft.trading, adaptive_learning_drift_weight: +e.target.value } })}
                           className="w-32 accent-[var(--color-accent)]" />
                  </Row>
                )}
              </Section>

              <Section title="Confidence Calibration" subtitle="Phase 6: maps raw confidence scores to realized win rates. Holdout validation with rollback protection is on by default -- a calibration refresh is only adopted if it scores at least as well as the current one on held-out data.">
                <Row label="Holdout validation" hint="Before adopting a refreshed calibration curve, score it against held-out data it wasn't fit on. Off reverts to always adopting a fresh refresh directly.">
                  <Toggle checked={draft.trading.calibration_holdout_validation_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, calibration_holdout_validation_enabled: v } })} />
                </Row>
                {draft.trading.calibration_holdout_validation_enabled !== false && (
                  <Row label="Roll back if worse" hint="If a candidate refresh scores worse than the current calibration on held-out data, keep the current one instead of adopting the worse candidate.">
                    <Toggle checked={draft.trading.calibration_rollback_on_worse_enabled ?? true} onChange={(v) => patch({ trading: { ...draft.trading, calibration_rollback_on_worse_enabled: v } })} />
                  </Row>
                )}
              </Section>

              <Section title="Pattern Risk Engine" subtitle="Shadow-scores every trade against historical loss patterns (engine.pattern_risk_engine) -- always runs, purely observational by default. Enforcement (below) is a separate, off-by-default opt-in that lets a strongly-flagged pattern actually delay the next signal on that asset.">
                <Row label="Shadow-scoring sample size" hint="Minimum historical trades needed before pattern comparisons are considered meaningful. Default 10.">
                  <NumberInput value={draft.trading.pattern_risk_min_sample_size ?? 10} step={1} min={3} max={100} onChange={(v) => patch({ trading: { ...draft.trading, pattern_risk_min_sample_size: v } })} />
                </Row>
                <Row label="Enable enforcement" hint="REAL EFFECT, not instrumentation: when the engine's recommend_reject signal fires (requires multiple independent risk signals to agree, not just one), extends this asset's cooldown so the next signal on it is delayed -- only takes effect if Precision Mode is also on. Off by default.">
                  <Toggle checked={!!draft.trading.pattern_risk_enforcement_enabled} onChange={(v) => patch({ trading: { ...draft.trading, pattern_risk_enforcement_enabled: v } })} />
                </Row>
                {draft.trading.pattern_risk_enforcement_enabled && (
                  <Row label="Rollout %" hint="Gradual canary rollout: the percentage of recommend_reject-flagged trades that actually get the cooldown extension applied, chosen at random per trade. Start low (e.g. 10-20%) and watch the Pattern Risk validation report before raising it. 0 = enforcement toggle has no effect yet.">
                    <NumberInput value={draft.trading.pattern_risk_rollout_pct ?? 0} step={5} min={0} max={100} onChange={(v) => patch({ trading: { ...draft.trading, pattern_risk_rollout_pct: v } })} />
                  </Row>
                )}
              </Section>

              <Section title="Recovery Engine" subtitle="Advisory, shadow-only score (engine.recovery_engine) estimating whether recent shadow-trade performance suggests it's safe to resume after a pause. Always computes; auto-resume (below) is the only part that can act on it, and only for a pause caused by the drawdown circuit breaker -- never a manual kill-switch or Telegram pause.">
                <Row label="Min. shadow sample" hint="Resolved shadow trades required before an assessment is considered significant enough to act on. Default 20.">
                  <NumberInput value={draft.trading.recovery_min_shadow_sample ?? 20} step={5} min={5} max={200} onChange={(v) => patch({ trading: { ...draft.trading, recovery_min_shadow_sample: v } })} />
                </Row>
                <Row label="Consistency window" hint="Recent-trades window split in half to check both halves clear the win-rate baseline and don't diverge wildly from each other. Default 20.">
                  <NumberInput value={draft.trading.recovery_consistency_window ?? 20} step={5} min={5} max={200} onChange={(v) => patch({ trading: { ...draft.trading, recovery_consistency_window: v } })} />
                </Row>
                <Row label="Win-rate baseline %" hint="Minimum win rate the shadow sample needs to clear. Default 50.">
                  <NumberInput value={draft.trading.recovery_win_rate_baseline ?? 50} step={1} min={0} max={100} onChange={(v) => patch({ trading: { ...draft.trading, recovery_win_rate_baseline: v } })} />
                </Row>
                <Row label="Resume score threshold" hint="Minimum recovery_score (0-100) before resume_recommended is set. Default 65.">
                  <NumberInput value={draft.trading.recovery_score_threshold ?? 65} step={5} min={0} max={100} onChange={(v) => patch({ trading: { ...draft.trading, recovery_score_threshold: v } })} />
                </Row>
                <Row label="Auto-resume after drawdown pause" hint="REAL EFFECT, not instrumentation: when on, and trading is paused specifically by the drawdown circuit breaker (never a manual kill-switch or Telegram pause), and this assessment is both significant and recommends resuming, trading resumes automatically. Off by default -- review the Recovery panel in Analytics for a while before turning this on.">
                  <Toggle checked={!!draft.trading.recovery_auto_resume_enabled} onChange={(v) => patch({ trading: { ...draft.trading, recovery_auto_resume_enabled: v } })} />
                </Row>
              </Section>

              <Section title="Volatility-Adjusted Duration">
                <Row label="Enable volatility-adjusted duration" hint="Scales trade expiry by current ATR% instead of a fixed duration">
                  <Toggle checked={!!draft.trading.volatility_adjusted_duration} onChange={(v) => patch({ trading: { ...draft.trading, volatility_adjusted_duration: v } })} />
                </Row>
                {draft.trading.volatility_adjusted_duration && (
                  <>
                    <Row label="Calm-market multiplier" hint="Duration multiplier when ATR% is low">
                      <NumberInput value={draft.trading.duration_low_vol_mult ?? 1.3} step={0.05} onChange={(v) => patch({ trading: { ...draft.trading, duration_low_vol_mult: v } })} />
                    </Row>
                    <Row label="Volatile-market multiplier" hint="Duration multiplier when ATR% is high">
                      <NumberInput value={draft.trading.duration_high_vol_mult ?? 0.7} step={0.05} onChange={(v) => patch({ trading: { ...draft.trading, duration_high_vol_mult: v } })} />
                    </Row>
                    <Row label="Minimum duration (sec)">
                      <NumberInput value={draft.trading.duration_min_seconds ?? 30} onChange={(v) => patch({ trading: { ...draft.trading, duration_min_seconds: v } })} />
                    </Row>
                  </>
                )}
              </Section>

              <Section title="Confidence-Based Stake Sizing">
                <Row label="Enable confidence-based sizing" hint="Scales stake by signal confidence instead of a flat amount">
                  <Toggle checked={!!draft.risk.confidence_based_sizing} onChange={(v) => patch({ risk: { ...draft.risk, confidence_based_sizing: v } })} />
                </Row>
                {draft.risk.confidence_based_sizing && (
                  <>
                    <Row label="Stake floor multiplier" hint="Stake multiplier at borderline (threshold-level) confidence">
                      <NumberInput value={draft.risk.confidence_size_floor ?? 0.6} step={0.05} onChange={(v) => patch({ risk: { ...draft.risk, confidence_size_floor: v } })} />
                    </Row>
                    <Row label="Stake ceiling multiplier" hint="Stake multiplier at 100% confidence">
                      <NumberInput value={draft.risk.confidence_size_ceiling ?? 1.4} step={0.05} onChange={(v) => patch({ risk: { ...draft.risk, confidence_size_ceiling: v } })} />
                    </Row>
                  </>
                )}
              </Section>

              <Row label="Drawdown circuit breaker" hint="Pause when balance falls from session peak">
                <Toggle checked={draft.risk.drawdown_breaker_enabled} onChange={(v) => patch({ risk: { ...draft.risk, drawdown_breaker_enabled: v } })} />
              </Row>
              {draft.risk.drawdown_breaker_enabled && (
                <>
                  <Row label="Max drawdown (%)" hint="Pause trading at this % drop from peak">
                    <NumberInput value={draft.risk.max_drawdown_pct} step={1} onChange={(v) => patch({ risk: { ...draft.risk, max_drawdown_pct: v } })} />
                  </Row>
                  <Row label="Max drawdown ($)" hint="0 = disabled, use % only">
                    <NumberInput value={draft.risk.max_drawdown_amount} onChange={(v) => patch({ risk: { ...draft.risk, max_drawdown_amount: v } })} />
                  </Row>
                </>
              )}
              <Row label="Correlation guard" hint="Block same-direction trades on correlated pairs">
                <Toggle checked={draft.risk.correlation_guard_enabled} onChange={(v) => patch({ risk: { ...draft.risk, correlation_guard_enabled: v } })} />
              </Row>
              {draft.risk.correlation_guard_enabled && (
                <Row label="Correlation threshold" hint="0.85 = highly correlated">
                  <NumberInput value={Math.round(draft.risk.correlation_threshold * 100)} onChange={(v) => patch({ risk: { ...draft.risk, correlation_threshold: v / 100 } })} />
                </Row>
              )}
            </Section>

            {/* Auto Validation */}
            <Section title="Auto Validation">
              <Row label="Daily auto-run" hint={`Runs at ${draft.validation.daily_run_hour_utc}:00 UTC in background`}>
                <Toggle checked={draft.validation.enabled} onChange={(v) => patchValidation({ enabled: v })} />
              </Row>
              {draft.validation.enabled && (
                <>
                  <Row label="Daily run hour (UTC)">
                    <NumberInput value={draft.validation.daily_run_hour_utc} onChange={(v) => patchValidation({ daily_run_hour_utc: Math.max(0, Math.min(23, v)) })} />
                  </Row>
                  <Row label="History bars" hint="Candles per asset for OOS backtest">
                    <select
                      value={draft.validation.history_bars}
                      onChange={(e) => patchValidation({ history_bars: +e.target.value })}
                      className="rounded-lg bg-white/8 px-2 py-1 text-sm outline-none ring-1 ring-white/10"
                    >
                      <option value={500}>500 bars</option>
                      <option value={2000}>2,000 bars</option>
                      <option value={5000}>5,000 bars</option>
                      <option value={10000}>10,000 bars</option>
                    </select>
                  </Row>
                  <Row label="Walk-forward folds" hint="More folds = stricter OOS test">
                    <NumberInput value={draft.validation.walk_forward_folds} onChange={(v) => patchValidation({ walk_forward_folds: Math.max(2, Math.min(8, v)) })} />
                  </Row>
                  <Row label="Active score min" hint="80+ = strategy votes fully">
                    <NumberInput value={draft.validation.active_score_min} onChange={(v) => patchValidation({ active_score_min: v })} />
                  </Row>
                  <Row label="Trial score min" hint="55–79 = probation mode">
                    <NumberInput value={draft.validation.trial_score_min} onChange={(v) => patchValidation({ trial_score_min: v })} />
                  </Row>
                  <Row label="Sync Strategy Manager" hint="Mute strategies with low health on all assets">
                    <Toggle checked={draft.validation.sync_strategy_manager} onChange={(v) => patchValidation({ sync_strategy_manager: v })} />
                  </Row>
                </>
              )}
              <div className="rounded-lg bg-white/5 px-3 py-2 text-[11px] text-faint">
                Validation runs in a separate background worker — Auto Trade is never paused. Run manually from the Validate tab anytime.
              </div>
            </Section>

            {/* Notifications */}
            <Section title="Notifications & Sound">
              <Row label="Notifications">
                <Toggle checked={draft.ux.notifications_enabled} onChange={(v) => { patch({ ux: { ...draft.ux, notifications_enabled: v } }); if (v) askNotify(); }} />
              </Row>
              <Row label="Sound">
                <Toggle checked={draft.ux.sound_enabled} onChange={(v) => patch({ ux: { ...draft.ux, sound_enabled: v } })} />
              </Row>
              {draft.ux.sound_enabled && (
                <Row label={`Volume ${Math.round(draft.ux.sound_volume * 100)}%`}>
                  <input type="range" min={0} max={1} step={0.05} value={draft.ux.sound_volume} onChange={(e) => patch({ ux: { ...draft.ux, sound_volume: +e.target.value } })} className="w-32 accent-[var(--color-accent)]" />
                </Row>
              )}
            </Section>

            {/* Appearance */}
            <Section title="Appearance" subtitle="Accent color -- applies immediately, saved with the rest of your settings.">
              <div className="flex flex-wrap gap-2">
                {Object.entries(ACCENT_PRESETS).map(([key, preset]) => {
                  const active = (draft.ux.accent || DEFAULT_ACCENT) === key;
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => patch({ ux: { ...draft.ux, accent: key } })}
                      title={preset.label}
                      className={`flex items-center gap-2 rounded-xl px-3 py-2 text-xs font-medium ring-1 transition ${
                        active ? "ring-2 ring-offset-1 ring-offset-black/20" : "ring-white/10 opacity-80 hover:opacity-100"
                      }`}
                      style={active ? { boxShadow: `0 0 0 1.5px ${preset.accent}` } as React.CSSProperties : undefined}
                    >
                      <span
                        className="h-4 w-4 rounded-full"
                        style={{ background: `linear-gradient(120deg, ${preset.accent}, ${preset.accent2})` }}
                      />
                      {preset.label}
                    </button>
                  );
                })}
              </div>
            </Section>

            {/* Telegram */}
            <Section title="Telegram">
              <Row label="Enable Telegram">
                <Toggle checked={draft.telegram.enabled} onChange={(v) => patch({ telegram: { ...draft.telegram, enabled: v } })} />
              </Row>
              {draft.telegram.enabled && (
                <>
                  <Row label="Bot token"><TextInput value={draft.telegram.bot_token ?? ""} onChange={(v) => patch({ telegram: { ...draft.telegram, bot_token: v } })} placeholder="123:ABC" /></Row>
                  <Row label="Chat ID"><TextInput value={draft.telegram.chat_id ?? ""} onChange={(v) => patch({ telegram: { ...draft.telegram, chat_id: v } })} placeholder="123456789" /></Row>
                  <Row label="Allow commands" hint="/enter /skip /stop from chat"><Toggle checked={draft.telegram.allow_commands} onChange={(v) => patch({ telegram: { ...draft.telegram, allow_commands: v } })} /></Row>
                  <Row label="Test" hint="Saves settings first, then sends a real message">
                    <button
                      onClick={async () => {
                        setTgTest("Sending…");
                        try {
                          await setSettings(draft);
                          await api.telegramTest();
                          setTgTest("✅ Sent — check your chat");
                        } catch (e) {
                          setTgTest(`❌ ${e instanceof Error ? e.message : "failed"}`);
                        }
                      }}
                      className="rounded-lg bg-white/10 px-3 py-1 text-[12px] ring-1 ring-white/15"
                    >
                      Send test message
                    </button>
                  </Row>
                  {tgTest && <div className="px-1 text-[11px] text-dim">{tgTest}</div>}
                </>
              )}
            </Section>

            {/* Advanced (collapsible) */}
            <button
              onClick={() => setAdvanced((v) => !v)}
              className="mb-3 flex w-full items-center justify-between rounded-2xl bg-white/5 px-4 py-3 text-sm font-medium text-dim ring-1 ring-white/8"
            >
              <span>Advanced</span>
              <span className={`transition-transform ${advanced ? "rotate-180" : ""}`}>⌄</span>
            </button>
            {advanced && (
              <>
                <Section title="Advanced Risk">
                  <Row label="Stake mode">
                    <select value={draft.risk.stake_mode} onChange={(e) => patch({ risk: { ...draft.risk, stake_mode: e.target.value as any } })} className="rounded-lg bg-white/8 px-2 py-1 text-sm outline-none ring-1 ring-white/10">
                      <option value="fixed">Fixed $</option>
                      <option value="percent">% of balance</option>
                    </select>
                  </Row>
                  {draft.risk.stake_mode === "percent" && (
                    <Row label="Stake (% balance)"><NumberInput value={draft.risk.stake_percent} step={0.5} onChange={(v) => patch({ risk: { ...draft.risk, stake_percent: v } })} /></Row>
                  )}
                  <Row label="Min payout (%)"><NumberInput value={draft.risk.min_payout} onChange={(v) => patch({ risk: { ...draft.risk, min_payout: v } })} /></Row>
                  <Row label="Max consecutive losses"><NumberInput value={draft.risk.max_consecutive_losses} onChange={(v) => patch({ risk: { ...draft.risk, max_consecutive_losses: v } })} /></Row>
                  <Row label="Max trades / day"><NumberInput value={draft.risk.max_trades_per_day} onChange={(v) => patch({ risk: { ...draft.risk, max_trades_per_day: v } })} /></Row>
                  <Row label="Cooldown after loss (s)"><NumberInput value={draft.risk.cooldown_seconds} onChange={(v) => patch({ risk: { ...draft.risk, cooldown_seconds: v } })} /></Row>
                  <Row label="Martingale (risky)"><Toggle checked={draft.risk.martingale_enabled} onChange={(v) => patch({ risk: { ...draft.risk, martingale_enabled: v } })} /></Row>
                </Section>
                <Section title="Appearance">
                  <Row label={`Fog intensity ${Math.round(draft.ux.fog_intensity * 100)}%`}>
                    <input type="range" min={0} max={1} step={0.1} value={draft.ux.fog_intensity} onChange={(e) => patch({ ux: { ...draft.ux, fog_intensity: +e.target.value } })} className="w-32 accent-[var(--color-accent)]" />
                  </Row>
                  <Row label="Reduced motion"><Toggle checked={draft.ux.reduced_motion} onChange={(v) => patch({ ux: { ...draft.ux, reduced_motion: v } })} /></Row>
                </Section>
                <Section title="Server (.env)">
                  <div className="rounded-lg bg-white/5 px-3 py-2 text-[11px] text-faint">Changes here apply after restarting the backend.</div>
                  <Row label="Host"><TextInput value={envDraft.host} onChange={(v) => patchEnv({ host: v })} placeholder="127.0.0.1" /></Row>
                  <Row label="Port"><NumberInput value={envDraft.port} onChange={(v) => patchEnv({ port: v })} /></Row>
                  <Row label="CORS origins"><TextInput value={envDraft.cors_origins} onChange={(v) => patchEnv({ cors_origins: v })} placeholder="http://localhost:5173" /></Row>
                </Section>
              </>
            )}

            <button onClick={save} className="sticky bottom-0 w-full rounded-2xl bg-[var(--color-accent)]/25 py-3 font-semibold text-[var(--color-accent)] ring-1 ring-[var(--color-accent)]/50 transition active:scale-[0.98]">
              Save Settings
            </button>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
