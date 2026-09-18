import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { STRATEGY_LABELS, type ValidationHealthRow, type ValidationLeaderboard, type ValidationProgress, type ValidationResultRow } from "../types";
import { BarRow, EquityArea } from "./charts";
import { GlassCard, Metric, Pill, SectionTitle } from "./ui";

const sl = (n: string) => STRATEGY_LABELS[n] ?? n;

const STATUS_COLORS: Record<string, string> = {
  active: "var(--color-up)",
  trial: "var(--color-gold)",
  muted: "var(--color-down)",
};

export function Validation() {
  const settings = useStore((s) => s.settings);
  const [progress, setProgress] = useState<ValidationProgress | null>(null);
  const [leaderboard, setLeaderboard] = useState<ValidationLeaderboard | null>(null);
  const [health, setHealth] = useState<ValidationHealthRow[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ValidationResultRow | null>(null);
  const [history, setHistory] = useState<{ finished_at: number; health_score: number; win_rate: number; net_profit: number }[]>([]);

  const load = useCallback(async () => {
    try {
      const [st, lb, h] = await Promise.all([
        api.validationStatus(),
        api.validationLeaderboard(),
        api.validationHealth(),
      ]);
      setProgress(st);
      setLeaderboard(lb);
      setHealth(h.matrix);
      setRunning(st.status === "running" || st.status === "queued");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load validation data");
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, progress?.status === "running" ? 3000 : 15000);
    return () => clearInterval(id);
  }, [load, progress?.status]);

  useEffect(() => {
    if (!selected) return;
    api.validationHistory(selected.asset, selected.strategy, selected.timeframe)
      .then(setHistory)
      .catch(() => setHistory([]));
  }, [selected]);

  async function runNow() {
    setError(null);
    try {
      await api.validationRun();
      setRunning(true);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start validation");
    }
  }

  const topAssets = leaderboard?.asset_rankings.slice(0, 8) ?? [];
  const topStrategies = leaderboard?.strategy_rankings.slice(0, 8) ?? [];
  const results = leaderboard?.results ?? [];

  const matrixAssets = useMemo(() => [...new Set(health.map((h) => h.asset))], [health]);
  const matrixStrats = useMemo(() => [...new Set(health.map((h) => h.strategy))], [health]);

  return (
    <div className="flex flex-col gap-4 pb-24">
      <SectionTitle>Strategy Validation</SectionTitle>

      <GlassCard className="flex flex-col gap-3 p-4">
        <div className="text-[11px] text-faint">
          Auto backtesting runs in the background using the same engine as live Auto Trade — walk-forward
          out-of-sample validation, health scores, and rankings. Live trading is never blocked.
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={runNow}
            disabled={running}
            className="rounded-xl bg-[var(--color-accent)] px-4 py-2 text-[13px] font-bold text-black disabled:opacity-50"
          >
            {running ? "Running…" : "Run Validation Now"}
          </button>
          {progress && (
            <Pill tone={progress.status === "completed" ? "up" : progress.status === "failed" ? "down" : "accent"}>
              {progress.status}
              {progress.total_jobs > 0 && ` · ${progress.percent ?? Math.round(progress.completed_jobs / progress.total_jobs * 100)}%`}
            </Pill>
          )}
        </div>
        {progress?.current && <div className="text-[11px] text-dim">Evaluating: {progress.current}</div>}
        {error && <div className="text-[12px] text-down">{error}</div>}
      </GlassCard>

      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard className="p-4">
          <div className="mb-3 text-[13px] font-semibold">Asset Rankings</div>
          {topAssets.length === 0 ? (
            <div className="text-[12px] text-faint">No validation run yet. Click Run Validation Now.</div>
          ) : (
            topAssets.map((a) => (
              <BarRow
                key={a.asset}
                label={a.asset}
                value={a.avg_health}
                max={100}
                display={`${a.avg_health} health · $${a.net_profit.toFixed(0)}`}
                tone="auto"
                sub={`${a.trades} trades across strategies`}
              />
            ))
          )}
        </GlassCard>

        <GlassCard className="p-4">
          <div className="mb-3 text-[13px] font-semibold">Strategy Rankings</div>
          {topStrategies.length === 0 ? (
            <div className="text-[12px] text-faint">Run validation to rank strategies.</div>
          ) : (
            topStrategies.map((s) => (
              <BarRow
                key={s.strategy}
                label={sl(s.strategy)}
                value={s.avg_health}
                max={100}
                display={`${s.avg_health} health · $${s.net_profit.toFixed(0)}`}
                tone="auto"
                sub={`${s.trades} OOS trades`}
              />
            ))
          )}
        </GlassCard>
      </div>

      <GlassCard className="p-4">
        <div className="mb-3 text-[13px] font-semibold">Health Score Matrix</div>
        {health.length === 0 ? (
          <div className="text-[12px] text-faint">Health scores appear after the first validation run.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[480px] text-left text-[11px]">
              <thead>
                <tr className="text-faint">
                  <th className="pb-2 pr-2">Asset</th>
                  {matrixStrats.map((s) => (
                    <th key={s} className="pb-2 px-1 text-center">{sl(s).split(" ")[0]}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrixAssets.map((asset) => (
                  <tr key={asset} className="border-t border-white/5">
                    <td className="py-1.5 pr-2 font-medium">{asset}</td>
                    {matrixStrats.map((strat) => {
                      const cell = health.find((h) => h.asset === asset && h.strategy === strat);
                      if (!cell) return <td key={strat} className="px-1 py-1.5 text-center text-faint">—</td>;
                      return (
                        <td key={strat} className="px-1 py-1.5 text-center">
                          <button
                            onClick={() => setSelected(results.find((r) => r.asset === asset && r.strategy === strat) ?? null)}
                            className="nums rounded-md px-1.5 py-0.5 font-semibold"
                            style={{ color: STATUS_COLORS[cell.status] ?? "inherit", background: "rgba(255,255,255,0.04)" }}
                            title={`${cell.status} · ${cell.score}/100`}
                          >
                            {cell.score}
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-2 flex gap-3 text-[10px] text-faint">
          <span><span style={{ color: STATUS_COLORS.active }}>●</span> Active 80+</span>
          <span><span style={{ color: STATUS_COLORS.trial }}>●</span> Trial 55–79</span>
          <span><span style={{ color: STATUS_COLORS.muted }}>●</span> Muted &lt;55</span>
        </div>
      </GlassCard>

      {selected && (
        <GlassCard className="p-4">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="text-[13px] font-semibold">
              {selected.asset} · {sl(selected.strategy)} · {selected.timeframe}
            </div>
            <Pill tone={selected.health_status === "active" ? "up" : selected.health_status === "trial" ? "gold" : "down"}>
              Health {selected.health_score} · {selected.health_status}
            </Pill>
          </div>
          <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Metric label="Win rate" value={`${selected.win_rate}%`} />
            <Metric label="Trades" value={String(selected.total_trades)} />
            <Metric label="Net P/L" value={`$${selected.net_profit.toFixed(2)}`} tone={selected.net_profit >= 0 ? "up" : "down"} />
            <Metric label="Max DD" value={`${selected.max_drawdown_pct}%`} tone="down" />
            <Metric label="Profit factor" value={selected.profit_factor != null ? String(selected.profit_factor) : "—"} />
            <Metric label="Conf. accuracy" value={`${selected.confidence_accuracy}%`} />
            <Metric label="OOS folds" value={String(selected.oos_folds)} />
            <Metric label="OOS stability" value={`${selected.oos_consistency}%`} />
          </div>
          <div className="mb-2 text-[12px] font-medium">Equity curve (OOS)</div>
          <EquityArea data={selected.equity_curve.map((p) => ({ equity: p.equity }))} height={140} />
          {history.length > 1 && (
            <>
              <div className="mt-4 mb-2 text-[12px] font-medium">Historical comparison</div>
              <div className="flex flex-wrap gap-2">
                {history.slice().reverse().map((h, i) => (
                  <div key={i} className="rounded-lg bg-white/5 px-2 py-1 text-[10px]">
                    <div className="text-faint">{new Date((h.finished_at ?? 0) * 1000).toLocaleDateString()}</div>
                    <div className="nums font-semibold" style={{ color: STATUS_COLORS[h.health_score >= 80 ? "active" : h.health_score >= 55 ? "trial" : "muted"] }}>
                      {h.health_score} · {h.win_rate}% WR
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </GlassCard>
      )}

      <GlassCard className="p-4">
        <div className="text-[11px] text-faint">
          Daily auto-run: {settings?.validation?.enabled !== false ? `enabled at ${settings?.validation?.daily_run_hour_utc ?? 3}:00 UTC` : "disabled"} ·
          Walk-forward OOS · Correlation guard · Drawdown circuit breaker on live equity.
          Nanosecond broker timing is not possible over WebSocket; scan loop runs every ~3s with minimal latency path to Quotex.
        </div>
      </GlassCard>
    </div>
  );
}
