import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { StrategyPerformance } from "../types";
import { GlassCard, Pill, SectionTitle, Toggle } from "./ui";

const STRATEGIES = [
  { id: "trend_pullback", name: "Trend Pullback", desc: "EMA stack + RSI pullback entry", regime: "trending" },
  { id: "ema_ribbon", name: "EMA Ribbon", desc: "5/8/13/21 ribbon alignment", regime: "trending" },
  { id: "supertrend", name: "Supertrend", desc: "ATR-based trend follower", regime: "trending" },
  { id: "adx_di", name: "ADX Trend", desc: "Strong trend with rising ADX", regime: "trending" },
  { id: "psar", name: "Parabolic SAR", desc: "SAR flip trend reversal", regime: "trending" },
  { id: "momentum_breakout", name: "Momentum Breakout", desc: "BB squeeze → expansion + MACD", regime: "volatile" },
  { id: "macd_cross", name: "MACD Cross", desc: "Signal-line crossover", regime: "any" },
  { id: "rsi_divergence", name: "RSI Divergence", desc: "Price/RSI divergence reversal", regime: "any" },
  { id: "stoch_rsi", name: "Stochastic RSI", desc: "StochRSI cross from extremes", regime: "ranging" },
  { id: "bollinger_bounce", name: "Bollinger Bounce", desc: "Band rejection back to mean", regime: "ranging" },
  { id: "mean_reversion", name: "Mean Reversion", desc: "Bollinger extremes + RSI/Stoch", regime: "ranging" },
  // High-accuracy improvement strategies — pure price-action for binary (no volume dependency)
  { id: "vol_norm_reversion", name: "Vol-Norm Reversion", desc: "ATR-adjusted extremes + S/R bounce + price strength (binary)", regime: "ranging" },
  { id: "confluence_breakout", name: "Confluence Breakout", desc: "Key-level break 3+ confl + strong body/wick (binary)", regime: "trending" },
  { id: "session_swing", name: "Session Swing", desc: "Session-aware swings: RSI/EMA/MACD + time-vol mult (binary)", regime: "any" },
  { id: "multi_confluence", name: "Multi-Factor Confluence", desc: "6+/8 OHLC factors + body strength (binary, no volume)", regime: "trending" },
];

const TIMEFRAMES = ["30s", "1m", "2m", "3m", "5m", "15m", "30m", "1h", "4h", "1day"];

export function Strategy() {
  const settings = useStore((s) => s.settings);
  const setSettings = useStore((s) => s.setSettings);
  const [perf, setPerf] = useState<Record<string, StrategyPerformance>>({});

  useEffect(() => {
    const load = () =>
      api.strategyPerformance().then((rows) => {
        const map: Record<string, StrategyPerformance> = {};
        rows.forEach((r) => { map[r.name] = r; });
        setPerf(map);
      }).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  if (!settings) return null;

  const toggleOverride = async (name: string, current: StrategyPerformance | undefined) => {
    const next = current?.manual_override === "muted" ? null : "muted";
    const res = await api.setStrategyOverride(name, next);
    setPerf((p) => ({ ...p, [name]: res.status }));
  };

  const toggle = (id: string, on: boolean) => {
    const set = new Set(settings.trading.enabled_strategies);
    on ? set.add(id) : set.delete(id);
    setSettings({ ...settings, trading: { ...settings.trading, enabled_strategies: [...set] } });
  };

  return (
    <div className="space-y-4">
      <GlassCard>
        <SectionTitle right={<Pill tone="accent">{settings.trading.enabled_strategies.length} active</Pill>}>
          Strategy Lab
        </SectionTitle>
        <p className="mb-4 text-[12.5px] leading-relaxed text-dim">
          Enabled strategies vote on every candle. The confluence engine fires a signal only when the weighted
          consensus clears your confidence threshold.
        </p>
        <div className="space-y-2.5">
          {STRATEGIES.map((s) => {
            const on = settings.trading.enabled_strategies.includes(s.id);
            const p = perf[s.id];
            const statusTone = p?.status === "muted" ? "down" : p?.status === "watching" ? "warn" : p?.status === "trial" ? "gold" : "up";
            return (
              <div key={s.id} className={`tile flex items-center justify-between p-3.5 transition ${on ? "ring-1 ring-[var(--color-accent)]/25" : ""}`}>
                <div className="min-w-0 pr-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[14px] font-semibold">{s.name}</span>
                    <Pill tone="neutral">{s.regime}</Pill>
                    {p && p.trades > 0 && (
                      <Pill tone={statusTone}>
                        {p.status === "muted" ? "Muted" : p.status === "trial" ? "On trial" : p.status === "watching" ? "Watching" : "Active"}
                        {" · "}{p.win_rate}%
                      </Pill>
                    )}
                  </div>
                  <div className="mt-0.5 text-[11.5px] text-faint">{s.desc}</div>
                  {p?.mute_reason && (
                    <div className="mt-1 text-[10.5px] text-faint">
                      {p.mute_reason}
                      {p.status === "muted" && p.next_trial_at && (
                        <> — trial retry {new Date(p.next_trial_at * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</>
                      )}
                    </div>
                  )}
                  {p && p.trades > 0 && (
                    <button
                      onClick={() => toggleOverride(s.id, p)}
                      className="mt-1.5 text-[10.5px] font-medium text-white/50 underline underline-offset-2"
                    >
                      {p.manual_override === "muted" ? "Remove manual mute" : "Force mute"}
                    </button>
                  )}
                </div>
                <Toggle checked={on} onChange={(v) => toggle(s.id, v)} />
              </div>
            );
          })}
        </div>
      </GlassCard>

      <GlassCard delay={0.05}>
        <SectionTitle>Signal Timing</SectionTitle>
        <div className="label mb-2">Timeframe</div>
        <div className="mb-4 flex flex-wrap gap-2">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => setSettings({ ...settings, trading: { ...settings.trading, timeframe: tf } })}
              className={`nums rounded-xl px-3.5 py-2 text-[13px] font-semibold transition ${
                settings.trading.timeframe === tf
                  ? "bg-[var(--color-accent)]/18 text-[var(--color-accent)] ring-1 ring-[var(--color-accent)]/40"
                  : "bg-white/5 text-dim"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>

        <div className="space-y-4">
          <Slider
            label="Trade duration"
            value={settings.trading.duration_seconds}
            display={`${settings.trading.duration_seconds}s`}
            min={30}
            max={300}
            step={30}
            onChange={(v) => setSettings({ ...settings, trading: { ...settings.trading, duration_seconds: v } })}
          />
          <Slider
            label="Confidence threshold"
            value={settings.risk.confidence_threshold}
            display={`${settings.risk.confidence_threshold}%`}
            min={50}
            max={95}
            step={1}
            onChange={(v) => setSettings({ ...settings, risk: { ...settings.risk, confidence_threshold: v } })}
          />
        </div>
      </GlassCard>
    </div>
  );
}

function Slider({ label, value, display, min, max, step, onChange }: {
  label: string; value: number; display: string; min: number; max: number; step: number; onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[13px] text-dim">{label}</span>
        <span className="nums text-[13px] font-semibold text-[var(--color-accent)]">{display}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(+e.target.value)}
        className="w-full accent-[var(--color-accent)]"
      />
    </div>
  );
}
