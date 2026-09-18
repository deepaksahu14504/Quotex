import { useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { STRATEGY_LABELS, type BacktestResult } from "../types";
import { EquityArea } from "./charts";
import { GlassCard, Metric, Pill, SectionTitle } from "./ui";

const sl = (n: string) => STRATEGY_LABELS[n] ?? n;
const TIMEFRAMES = ["1m", "2m", "3m", "5m", "15m", "30m", "1h", "4h", "1day"];
const BAR_PRESETS = [
  { label: "500 bars", value: 500 },
  { label: "2,000 bars", value: 2000 },
  { label: "5,000 bars", value: 5000 },
  { label: "10,000 bars", value: 10000 },
];

export function Backtest() {
  const settings = useStore((s) => s.settings);
  const [asset, setAsset] = useState("EURUSD_otc");
  const [timeframe, setTimeframe] = useState(settings?.trading.timeframe ?? "1m");
  const [bars, setBars] = useState(2000);
  const [duration, setDuration] = useState(settings?.trading.duration_seconds ?? 60);
  const [confidence, setConfidence] = useState(settings?.risk.confidence_threshold ?? 65);
  const [startBalance, setStartBalance] = useState(1000);
  const [useHtf, setUseHtf] = useState(settings?.trading.multi_timeframe_confirmation ?? true);

  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.runBacktest({
        asset, timeframe, bars, duration_seconds: duration,
        confidence_threshold: confidence, starting_balance: startBalance,
        multi_timeframe_confirmation: useHtf,
      });
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Backtest failed");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 pb-24">
      <SectionTitle>Backtest</SectionTitle>

      <GlassCard className="flex flex-col gap-3 p-4">
        <div className="text-[11px] text-faint">
          Replays historical candles through the same strategy engine the live scanner uses — same
          indicators, same confluence scoring, same risk gate. Results reflect what auto-mode would
          actually have done, not a separate approximation.
        </div>

        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Asset">
            <input value={asset} onChange={(e) => setAsset(e.target.value)} className="input" placeholder="EURUSD_otc" />
          </Field>
          <Field label="Timeframe">
            <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="input">
              {TIMEFRAMES.map((tf) => <option key={tf} value={tf}>{tf}</option>)}
            </select>
          </Field>
          <Field label="History depth">
            <select value={bars} onChange={(e) => setBars(Number(e.target.value))} className="input">
              {BAR_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select>
          </Field>
          <Field label="Trade duration (s)">
            <input type="number" value={duration} onChange={(e) => setDuration(Number(e.target.value))} className="input" />
          </Field>
          <Field label="Confidence threshold">
            <input type="number" value={confidence} onChange={(e) => setConfidence(Number(e.target.value))} className="input" />
          </Field>
          <Field label="Starting balance">
            <input type="number" value={startBalance} onChange={(e) => setStartBalance(Number(e.target.value))} className="input" />
          </Field>
        </div>

        <label className="flex items-center gap-2 text-[12px] text-dim">
          <input type="checkbox" checked={useHtf} onChange={(e) => setUseHtf(e.target.checked)} />
          Multi-timeframe confirmation
        </label>

        {error && (
          <div className="rounded-lg bg-[var(--color-down)]/10 px-3 py-2 text-[12px] text-[var(--color-down)]">{error}</div>
        )}

        <button
          onClick={run}
          disabled={running}
          className="rounded-xl bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-accent-2)] py-2.5 text-[14px] font-bold text-black transition active:scale-[0.98] disabled:opacity-60"
        >
          {running ? "Running backtest…" : "Run Backtest"}
        </button>
        {running && (
          <div className="text-[11px] text-faint">
            Larger histories take longer — {bars.toLocaleString()} bars typically takes a few seconds to
            {bars > 5000 ? " a couple of minutes" : " ~10 seconds"}.
          </div>
        )}
      </GlassCard>

      {result && <BacktestResults result={result} />}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-[10.5px] font-medium text-dim">{label}</label>
      {children}
    </div>
  );
}

function BacktestResults({ result }: { result: BacktestResult }) {
  const s = result.summary;
  const pnlTone = s.net_pnl > 0 ? "up" : s.net_pnl < 0 ? "down" : "default";

  return (
    <>
      {s.stopped_early_reason && (
        <div className="rounded-xl bg-[var(--color-gold)]/10 px-3 py-2 text-[12px] text-[var(--color-gold)]">
          Stopped early: {s.stopped_early_reason}
        </div>
      )}

      <GlassCard className="p-4">
        <div className="mb-3 grid grid-cols-3 gap-3">
          <Metric label="Net P&L" value={`${s.net_pnl >= 0 ? "+" : ""}$${s.net_pnl.toFixed(2)}`} tone={pnlTone as "up" | "down" | "default"} />
          <Metric label="Win rate" value={`${s.win_rate}%`} />
          <Metric label="Trades" value={String(s.total_trades)} />
        </div>
        <div className="grid grid-cols-3 gap-3">
          <Metric label="Profit factor" value={s.profit_factor != null ? s.profit_factor.toFixed(2) : "—"} />
          <Metric label="Max drawdown" value={`$${s.max_drawdown.toFixed(2)}`} sub={`${s.max_drawdown_pct}%`} />
          <Metric label="Ending balance" value={`$${s.ending_balance.toFixed(2)}`} />
        </div>
      </GlassCard>

      {result.equity_curve.length > 1 && (
        <GlassCard className="p-4">
          <div className="mb-2 text-[11px] font-semibold text-dim">Equity curve</div>
          <EquityArea data={result.equity_curve} />
        </GlassCard>
      )}

      {result.by_strategy.length > 0 && (
        <GlassCard className="flex flex-col gap-2 p-4">
          <div className="text-[11px] font-semibold text-dim">By strategy</div>
          {result.by_strategy.map((b) => (
            <div key={b.name} className="tile flex items-center justify-between px-3 py-2">
              <div className="min-w-0">
                <div className="text-[12.5px] font-semibold">{sl(b.name)}</div>
                <div className="nums text-[10px] text-faint">{b.trades} trades</div>
              </div>
              <div className="flex items-center gap-2">
                <Pill tone={b.win_rate >= 55 ? "up" : b.win_rate < 45 ? "down" : "neutral"}>{b.win_rate}%</Pill>
                <span className={`nums w-16 text-right text-[12px] font-semibold ${b.pnl > 0 ? "text-up" : b.pnl < 0 ? "text-down" : "text-dim"}`}>
                  {b.pnl >= 0 ? "+" : "-"}${Math.abs(b.pnl).toFixed(2)}
                </span>
              </div>
            </div>
          ))}
        </GlassCard>
      )}

      {result.trades.length > 0 && (
        <GlassCard className="flex flex-col gap-2 p-4">
          <div className="text-[11px] font-semibold text-dim">
            Trades ({result.trades.length}{result.trades.length > 50 ? " — showing last 50" : ""})
          </div>
          {result.trades.slice(-50).reverse().map((t) => {
            const time = new Date(t.closed_at * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
            const tone = t.status === "win" ? "up" : t.status === "loss" ? "down" : "neutral";
            return (
              <div key={t.id} className="tile flex items-center justify-between px-3 py-2">
                <div className="flex min-w-0 items-center gap-2.5">
                  <span
                    className="grid h-7 w-7 shrink-0 place-items-center rounded-lg text-[11px] font-bold"
                    style={{ color: t.direction === "call" ? "var(--color-up)" : "var(--color-down)", backgroundColor: t.direction === "call" ? "rgba(43,220,160,0.12)" : "rgba(255,90,118,0.12)" }}
                  >
                    {t.direction === "call" ? "▲" : "▼"}
                  </span>
                  <div className="min-w-0">
                    <div className="nums text-[11px] text-faint">{time}</div>
                    <div className="nums text-[10px] text-faint">${t.amount} · {t.confidence}%</div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Pill tone={tone as "up" | "down" | "neutral"}>{t.status.toUpperCase()}</Pill>
                  <span className={`nums w-16 text-right text-[12px] font-semibold ${t.profit > 0 ? "text-up" : t.profit < 0 ? "text-down" : "text-dim"}`}>
                    {t.profit >= 0 ? "+" : "-"}${Math.abs(t.profit).toFixed(2)}
                  </span>
                </div>
              </div>
            );
          })}
        </GlassCard>
      )}

      {result.trades.length === 0 && (
        <div className="rounded-xl bg-white/5 px-3 py-3 text-center text-[12px] text-faint">
          No trades fired at this confidence threshold over this period — try lowering the threshold or a longer history.
        </div>
      )}
    </>
  );
}
