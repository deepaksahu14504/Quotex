import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { STRATEGY_LABELS, type CalibrationStatus, type Insights as InsightsData, type RegimePerformance, type Trade } from "../types";
import { BarRow, Donut, EquityArea, FormDots, HourHeatmap } from "./charts";
import { GlassCard, Metric, Pill, SectionTitle, Skeleton } from "./ui";

const sl = (n?: string | null) => (n ? STRATEGY_LABELS[n] ?? n : "—");
const asset = (a?: string | null) => (a ? a.replace("_otc", "") : "—");

function HourTradeRow({ t }: { t: Trade }) {
  const time = new Date((t.closed_at || t.created_at) * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const tone = t.status === "win" ? "up" : t.status === "loss" ? "down" : "neutral";
  return (
    <div className="tile flex items-center justify-between gap-2 px-3 py-2">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="nums text-[11px] text-faint">{time}</span>
        <span
          className="grid h-7 w-7 shrink-0 place-items-center rounded-lg text-[11px] font-bold"
          style={{ color: t.direction === "call" ? "var(--color-up)" : "var(--color-down)", backgroundColor: t.direction === "call" ? "rgba(43,220,160,0.12)" : "rgba(255,90,118,0.12)" }}
        >
          {t.direction === "call" ? "▲" : "▼"}
        </span>
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-[12.5px] font-semibold">{asset(t.asset)}</span>
            {t.timeframe && <span className="nums rounded bg-white/6 px-1 py-0.5 text-[9px] text-faint">{t.timeframe}</span>}
          </div>
          <div className="nums text-[10px] text-faint">
            ${t.amount} · {t.duration}s{t.confidence ? ` · ${t.confidence}%` : ""}
            {t.strategies && t.strategies.length > 0 ? ` · ${t.strategies.map((s) => sl(s)).slice(0, 2).join(", ")}${t.strategies.length > 2 ? "…" : ""}` : ""}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Pill tone={tone as "up" | "down" | "neutral"}>{t.status.toUpperCase()}</Pill>
        <span className={`nums w-14 text-right text-[12px] font-semibold ${t.profit > 0 ? "text-up" : t.profit < 0 ? "text-down" : "text-dim"}`}>
          {t.profit >= 0 ? "+" : "-"}${Math.abs(t.profit).toFixed(2)}
        </span>
      </div>
    </div>
  );
}

function Highlight({
  title,
  name,
  value,
  sub,
  tone,
  icon,
}: {
  title: string;
  name: string;
  value: string;
  sub?: string;
  tone: "up" | "down" | "accent" | "gold";
  icon: string;
}) {
  const colors: Record<string, string> = {
    up: "var(--color-up)",
    down: "var(--color-down)",
    accent: "var(--color-accent)",
    gold: "var(--color-gold)",
  };
  const c = colors[tone];
  return (
    <div className="tile relative overflow-hidden p-3.5">
      <div className="absolute -right-4 -top-4 h-16 w-16 rounded-full opacity-20 blur-2xl" style={{ background: c }} />
      <div className="flex items-center gap-1.5">
        <span className="text-sm" style={{ color: c }}>{icon}</span>
        <span className="label">{title}</span>
      </div>
      <div className="mt-1.5 truncate text-[15px] font-semibold tracking-tight">{name}</div>
      <div className="nums mt-0.5 text-[17px] font-bold" style={{ color: c }}>{value}</div>
      {sub && <div className="mt-0.5 text-[10.5px] text-faint">{sub}</div>}
    </div>
  );
}

export function Insights() {
  const trades = useStore((s) => s.trades);
  const [d, setD] = useState<InsightsData | null>(null);
  const [allTrades, setAllTrades] = useState<Trade[]>([]);
  const [selHour, setSelHour] = useState<number | null>(null);
  const [cal, setCal] = useState<CalibrationStatus | null>(null);
  const [regimePerf, setRegimePerf] = useState<RegimePerformance[]>([]);

  useEffect(() => {
    api.insights().then(setD).catch(() => {});
    api.trades(1000).then(setAllTrades).catch(() => {});
    api.calibration().then(setCal).catch(() => {});
    api.regimePerformance().then(setRegimePerf).catch(() => {});
  }, [trades.length]);

  const hourTrades = useMemo(() => {
    if (selHour === null) return [];
    return allTrades
      .filter((t) => new Date((t.closed_at || t.created_at) * 1000).getHours() === selHour)
      .sort((a, b) => (b.closed_at || b.created_at) - (a.closed_at || a.created_at));
  }, [allTrades, selHour]);

  if (!d) return <div className="space-y-4"><Skeleton className="h-40" /><Skeleton className="h-56" /></div>;

  const s = d.summary;
  if (s.total === 0) {
    return (
      <GlassCard>
        <div className="flex h-48 flex-col items-center justify-center text-center text-dim">
          <div className="mb-3 h-12 w-12 rounded-2xl border border-dashed border-white/20" />
          No closed trades yet. Insights appear once trades resolve.
        </div>
      </GlassCard>
    );
  }

  const maxAbsPnl = Math.max(...d.by_asset.map((x) => Math.abs(x.pnl)), 1);
  const pnlTone = s.net_pnl >= 0 ? "up" : "down";

  return (
    <div className="space-y-4">
      {/* Headline highlights */}
      <GlassCard>
        <SectionTitle right={<Pill tone="accent">{s.total} trades</Pill>}>Key Insights</SectionTitle>
        <div className="grid grid-cols-2 gap-2.5 lg:grid-cols-4">
          <Highlight title="Most wins" icon="★" tone="up" name={sl(s.best_strategy?.name)}
            value={`${s.best_strategy?.win_rate ?? 0}% win`} sub={`${s.best_strategy?.trades ?? 0} trades · ${(s.best_strategy?.pnl ?? 0) >= 0 ? "+" : ""}$${(s.best_strategy?.pnl ?? 0).toFixed(2)}`} />
          <Highlight title="Fewest wins" icon="▼" tone="down" name={sl(s.worst_strategy?.name)}
            value={`${s.worst_strategy?.win_rate ?? 0}% win`} sub={`${s.worst_strategy?.trades ?? 0} trades · ${(s.worst_strategy?.pnl ?? 0) >= 0 ? "+" : ""}$${(s.worst_strategy?.pnl ?? 0).toFixed(2)}`} />
          <Highlight title="Most profitable" icon="◆" tone="gold" name={asset(s.most_profitable_asset?.asset)}
            value={`${(s.most_profitable_asset?.pnl ?? 0) >= 0 ? "+" : ""}$${(s.most_profitable_asset?.pnl ?? 0).toFixed(2)}`} sub={`${s.most_profitable_asset?.win_rate ?? 0}% · ${s.most_profitable_asset?.trades ?? 0} trades`} />
          <Highlight title="Least profitable" icon="◇" tone="down" name={asset(s.least_profitable_asset?.asset)}
            value={`${(s.least_profitable_asset?.pnl ?? 0) >= 0 ? "+" : ""}$${(s.least_profitable_asset?.pnl ?? 0).toFixed(2)}`} sub={`${s.least_profitable_asset?.win_rate ?? 0}% · ${s.least_profitable_asset?.trades ?? 0} trades`} />
        </div>
      </GlassCard>

      {/* Best strategy + asset combinations */}
      {d.top_combos.length > 0 && (
        <GlassCard delay={0.03}>
          <SectionTitle right={s.best_combo && <Pill tone="gold">★ {s.best_combo.win_rate}% win</Pill>}>
            Winning Combinations
          </SectionTitle>
          {s.best_combo && (
            <div className="tile relative mb-3 overflow-hidden p-4">
              <div className="absolute -right-6 -top-6 h-24 w-24 rounded-full opacity-20 blur-3xl" style={{ background: "var(--color-gold)" }} />
              <div className="label">Best combination</div>
              <div className="mt-1 flex flex-wrap items-baseline gap-x-2">
                <span className="text-[17px] font-bold tracking-tight">{sl(s.best_combo.strategy)}</span>
                <span className="text-dim">on</span>
                <span className="text-[17px] font-bold tracking-tight gradient-text">{asset(s.best_combo.asset)}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <Pill tone="up">{s.best_combo.win_rate}% win</Pill>
                <Pill tone="neutral">{s.best_combo.trades} trades</Pill>
                <span className="nums text-[13px] font-semibold" style={{ color: s.best_combo.pnl >= 0 ? "var(--color-up)" : "var(--color-down)" }}>
                  {s.best_combo.pnl >= 0 ? "+" : "-"}${Math.abs(s.best_combo.pnl).toFixed(2)} P/L
                </span>
              </div>
            </div>
          )}
          <div className="label mb-1">Top combos by win rate</div>
          <div className="space-y-0.5">
            {d.top_combos.map((c) => (
              <BarRow
                key={`${c.strategy}-${c.asset}`}
                label={`${sl(c.strategy)} · ${asset(c.asset)}`}
                value={c.win_rate}
                max={100}
                display={`${c.win_rate}%`}
                tone="auto"
                sub={`${c.trades} trades · ${c.pnl >= 0 ? "+" : ""}$${c.pnl.toFixed(2)}`}
              />
            ))}
          </div>
        </GlassCard>
      )}

      {/* Overview + equity */}
      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard delay={0.04}>
          <SectionTitle>Performance Overview</SectionTitle>
          <div className="flex items-center gap-5">
            <Donut wins={s.wins} losses={s.losses} />
            <div className="grid flex-1 grid-cols-2 gap-2.5">
              <Metric label="Net P/L" value={`${s.net_pnl >= 0 ? "+" : "-"}$${Math.abs(s.net_pnl).toFixed(2)}`} tone={pnlTone} />
              <Metric label="Profit factor" value={s.profit_factor ?? "—"} tone={(s.profit_factor ?? 0) >= 1 ? "up" : "down"} />
              <Metric label="Avg win" value={`+$${s.avg_win.toFixed(2)}`} tone="up" />
              <Metric label="Avg loss" value={`$${s.avg_loss.toFixed(2)}`} tone="down" />
            </div>
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2.5">
            <Metric label="Expectancy" value={`$${s.expectancy.toFixed(2)}`} tone={s.expectancy >= 0 ? "up" : "down"} sub="per trade" />
            <Metric label="Avg conf" value={`${s.avg_confidence}%`} tone="accent" />
            <Metric label="Streak" value={`${s.current_streak}${s.current_streak_type === "win" ? "W" : "L"}`} tone={s.current_streak_type === "win" ? "up" : "down"} />
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2"><span className="label">Form</span><FormDots form={d.form} /></div>
            <div className="flex gap-2"><Pill tone="up">{s.longest_win_streak}W best</Pill><Pill tone="down">{s.longest_loss_streak}L worst</Pill></div>
          </div>
        </GlassCard>

        <GlassCard delay={0.06}>
          <SectionTitle right={<Pill tone={pnlTone}>{s.net_pnl >= 0 ? "+" : ""}{s.net_pnl.toFixed(2)}</Pill>}>Equity Curve</SectionTitle>
          <EquityArea data={d.equity_curve} height={150} />
        </GlassCard>
      </div>

      {/* Strategy + Asset profitability */}
      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard delay={0.08}>
          <SectionTitle right={d.by_strategy[0] && <Pill tone="gold">★ {sl(d.by_strategy[0].name)}</Pill>}>Win Rate by Strategy</SectionTitle>
          <div className="space-y-0.5">
            {d.by_strategy.map((st) => (
              <BarRow key={st.name} label={sl(st.name)} value={st.win_rate} max={100} display={`${st.win_rate}%`} tone="auto"
                sub={`${st.trades} trades · ${st.pnl >= 0 ? "+" : ""}$${st.pnl.toFixed(2)}`} />
            ))}
          </div>
        </GlassCard>

        <GlassCard delay={0.1}>
          <SectionTitle>Asset Profitability</SectionTitle>
          <div className="space-y-0.5">
            {[...d.by_asset].sort((a, b) => b.pnl - a.pnl).map((a) => (
              <BarRow key={a.asset} label={asset(a.asset)} value={Math.abs(a.pnl)} max={maxAbsPnl}
                display={`${a.pnl >= 0 ? "+" : "-"}$${Math.abs(a.pnl).toFixed(2)}`} tone={a.pnl >= 0 ? "up" : "down"}
                sub={`${a.win_rate}% win · ${a.trades} trades`} />
            ))}
          </div>
        </GlassCard>
      </div>

      {/* Direction / confidence / timeframe */}
      <div className="grid gap-4 lg:grid-cols-3">
        <GlassCard delay={0.12}>
          <SectionTitle>By Direction</SectionTitle>
          <div className="grid grid-cols-2 gap-2.5">
            {d.by_direction.map((dir) => (
              <div key={dir.direction} className="tile p-3">
                <div className="flex items-center justify-between">
                  <span className="label">{dir.direction.toUpperCase()}</span>
                  <Pill tone={dir.direction === "call" ? "up" : "down"}>{dir.direction === "call" ? "▲" : "▼"}</Pill>
                </div>
                <div className="nums mt-1 text-lg font-semibold">{dir.win_rate}%</div>
                <div className="nums text-[11px] text-faint">{dir.trades} trades · {dir.pnl >= 0 ? "+" : ""}${dir.pnl.toFixed(0)}</div>
              </div>
            ))}
          </div>
        </GlassCard>

        <GlassCard delay={0.14}>
          <SectionTitle>By Confidence</SectionTitle>
          <div className="space-y-0.5">
            {d.by_confidence.map((b) => (
              <BarRow key={b.bucket} label={b.bucket} value={b.win_rate} max={100} display={`${b.win_rate}%`} tone="auto" sub={`${b.trades} trades`} />
            ))}
          </div>
        </GlassCard>

        <GlassCard delay={0.15}>
          <SectionTitle right={cal?.built ? <Pill tone="up">Active</Pill> : <Pill tone="neutral">Collecting data</Pill>}>
            Confidence Calibration
          </SectionTitle>
          {!cal || !cal.built ? (
            <p className="text-[12px] leading-relaxed text-faint">
              Needs {cal?.min_samples_required ?? 30} closed trades to start calibrating — {cal?.total_samples ?? 0} so far.
              Until then, signal confidence is used exactly as the strategy engine computes it.
            </p>
          ) : (
            <>
              <p className="mb-3 text-[12px] leading-relaxed text-faint">
                What the engine scores vs. what actually won, from your last {cal.total_samples} closed trades. Live
                signals are corrected to the calibrated line before your confidence threshold is applied.
              </p>
              <div className="space-y-2.5">
                {cal.points.map((p) => {
                  const delta = p.calibrated_win_rate - p.raw_win_rate;
                  return (
                    <div key={p.bucket} className="tile p-3">
                      <div className="mb-1.5 flex items-center justify-between">
                        <span className="nums text-[12.5px] font-semibold">{p.bucket}%</span>
                        <span className="text-[10.5px] text-faint">{p.trades} trades</span>
                      </div>
                      <div className="flex items-center gap-3">
                        <div className="flex-1">
                          <div className="mb-0.5 text-[9.5px] text-faint">Engine said</div>
                          <div className="h-1.5 overflow-hidden rounded-full bg-white/6">
                            <div className="h-full rounded-full bg-white/25" style={{ width: `${Math.max(2, p.raw_win_rate)}%` }} />
                          </div>
                        </div>
                        <div className="flex-1">
                          <div className="mb-0.5 text-[9.5px] text-faint">Actually won</div>
                          <div className="h-1.5 overflow-hidden rounded-full bg-white/6">
                            <div
                              className="h-full rounded-full"
                              style={{ width: `${Math.max(2, p.calibrated_win_rate)}%`, background: p.calibrated_win_rate >= 50 ? "var(--color-up)" : "var(--color-down)" }}
                            />
                          </div>
                        </div>
                        <span className={`nums w-14 shrink-0 text-right text-[11.5px] font-semibold ${Math.abs(delta) < 1 ? "text-dim" : delta > 0 ? "text-up" : "text-down"}`}>
                          {delta > 0 ? "+" : ""}{delta.toFixed(0)}pt
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </GlassCard>

        <GlassCard delay={0.155}>
          <SectionTitle>Performance by Market Regime</SectionTitle>
          <p className="mb-3 text-[12px] leading-relaxed text-faint">
            How each strategy does when the market is trending vs ranging (based on ADX). A strategy that's
            genuinely worse in the current regime gets a small confidence penalty; better-suited ones get a boost.
          </p>
          <div className="space-y-3">
            {regimePerf.filter((r) => r.regimes.trend.trades + r.regimes.range.trades > 0).length === 0 ? (
              <p className="text-[12px] text-faint">Not enough regime-tagged trade history yet.</p>
            ) : (
              regimePerf
                .filter((r) => r.regimes.trend.trades + r.regimes.range.trades + r.regimes.mixed.trades > 0)
                .map((r) => (
                  <div key={r.name} className="tile p-3">
                    <div className="mb-1.5 flex items-center justify-between">
                      <span className="text-[12.5px] font-semibold">{sl(r.name)}</span>
                      <span className="nums text-[10.5px] text-faint">overall {r.overall_win_rate}%</span>
                    </div>
                    <div className="grid grid-cols-3 gap-2">
                      {(["trend", "range", "mixed"] as const).map((key) => {
                        const cell = r.regimes[key];
                        if (cell.trades === 0) return <div key={key} />;
                        const tone = cell.adjustment > 0 ? "text-up" : cell.adjustment < 0 ? "text-down" : "text-dim";
                        return (
                          <div key={key} className="rounded-lg bg-white/4 p-2 text-center">
                            <div className="text-[9.5px] text-faint">{cell.label}</div>
                            <div className="nums text-[13px] font-semibold">{cell.win_rate}%</div>
                            <div className={`nums text-[9.5px] font-medium ${tone}`}>
                              {cell.adjustment > 0 ? "+" : ""}{cell.adjustment !== 0 ? `${cell.adjustment}pt` : "—"}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))
            )}
          </div>
        </GlassCard>

        <GlassCard delay={0.16}>
          <SectionTitle>By Timeframe</SectionTitle>
          <div className="space-y-0.5">
            {d.by_timeframe.map((b) => (
              <BarRow key={b.timeframe} label={b.timeframe} value={b.win_rate} max={100} display={`${b.win_rate}%`} tone="auto"
                sub={`${b.trades} trades · ${b.pnl >= 0 ? "+" : ""}$${b.pnl.toFixed(2)}`} />
            ))}
          </div>
        </GlassCard>
      </div>

      {/* Hour heatmap */}
      <GlassCard delay={0.18}>
        <SectionTitle right={<span className="label">tap an hour for trades</span>}>Time-of-Day Heatmap</SectionTitle>
        <HourHeatmap data={d.by_hour} selected={selHour} onSelect={(h) => setSelHour(selHour === h ? null : h)} />
        <div className="mt-3 flex items-center gap-3 text-[11px] text-faint">
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded" style={{ background: "var(--color-up)" }} /> profitable</span>
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded" style={{ background: "var(--color-down)" }} /> losing</span>
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded bg-white/10" /> none</span>
        </div>

        <AnimatePresence initial={false}>
          {selHour !== null && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <div className="mt-4 border-t border-white/8 pt-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-[13px] font-semibold">
                    {String(selHour).padStart(2, "0")}:00–{String((selHour + 1) % 24).padStart(2, "0")}:00
                    <span className="ml-2 text-faint">{hourTrades.length} trade{hourTrades.length === 1 ? "" : "s"}</span>
                  </span>
                  <button onClick={() => setSelHour(null)} className="rounded-lg bg-white/6 px-2 py-1 text-[11px] text-faint">Close</button>
                </div>
                {hourTrades.length === 0 ? (
                  <div className="py-6 text-center text-[12px] text-faint">No trades in this hour.</div>
                ) : (
                  <div className="max-h-72 space-y-1.5 overflow-y-auto pr-1">
                    {hourTrades.map((t) => <HourTradeRow key={t.id} t={t} />)}
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </GlassCard>

      {/* Best / worst trade */}
      {s.best_trade && s.worst_trade && (
        <div className="grid gap-4 sm:grid-cols-2">
          <GlassCard delay={0.2}>
            <SectionTitle right={<Pill tone="up">Best</Pill>}>Best Trade</SectionTitle>
            <div className="flex items-center justify-between">
              <div><div className="font-semibold">{asset(s.best_trade.asset)}</div><div className="text-[11px] text-faint">{s.best_trade.direction.toUpperCase()} · {s.best_trade.confidence ?? "—"}%</div></div>
              <div className="nums text-xl font-bold text-up">+${s.best_trade.profit.toFixed(2)}</div>
            </div>
          </GlassCard>
          <GlassCard delay={0.22}>
            <SectionTitle right={<Pill tone="down">Worst</Pill>}>Worst Trade</SectionTitle>
            <div className="flex items-center justify-between">
              <div><div className="font-semibold">{asset(s.worst_trade.asset)}</div><div className="text-[11px] text-faint">{s.worst_trade.direction.toUpperCase()} · {s.worst_trade.confidence ?? "—"}%</div></div>
              <div className="nums text-xl font-bold text-down">-${Math.abs(s.worst_trade.profit).toFixed(2)}</div>
            </div>
          </GlassCard>
        </div>
      )}
    </div>
  );
}
