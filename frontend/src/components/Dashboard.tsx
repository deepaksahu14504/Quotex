import { motion } from "framer-motion";
import { api } from "../api";
import { sound } from "../sound";
import { useStore } from "../store";
import type { Mode } from "../types";
import { AnimatedNumber, GlassCard, Metric, Pill } from "./ui";

export function Dashboard() {
  const state = useStore((s) => s.state);
  if (!state) return null;

  const pnlTone = state.daily_pnl > 0 ? "up" : state.daily_pnl < 0 ? "down" : "default";
  const modes: Mode[] = ["off", "manual", "auto"];
  const modeLabels: Record<Mode, string> = { off: "Off", manual: "Manual", auto: "Auto" };

  const setMode = (m: Mode) => {
    sound.play("click");
    api.setMode(m);
  };

  return (
    <div className="space-y-4">
      <GlassCard>
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2 text-[11px] text-dim">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${state.connected ? "bg-[var(--color-up)] pulse-dot" : "bg-[var(--color-down)]"}`} />
            <span className="truncate">{state.connected ? "Connected" : "Offline"} · {state.provider}</span>
          </div>
          <Pill tone={state.is_demo ? "accent" : "down"} className="shrink-0">
            {state.is_demo ? "DEMO" : "● LIVE"}
          </Pill>
        </div>

        <div className="label mt-3">Account balance</div>
        <div className="mt-1 flex flex-wrap items-end gap-x-2">
          <span className="text-3xl font-bold leading-none tracking-tight xl:text-[34px]">
            <AnimatedNumber value={state.balance} prefix="$" />
          </span>
          <span className="text-sm font-medium text-faint">{state.currency}</span>
        </div>
        <div className={`nums mt-1.5 text-[13px] font-semibold ${pnlTone === "up" ? "text-up" : pnlTone === "down" ? "text-down" : "text-dim"}`}>
          {state.daily_pnl >= 0 ? "+" : "-"}${Math.abs(state.daily_pnl).toFixed(2)} today
        </div>

        <div className="mt-5 grid grid-cols-3 gap-2.5">
          <Metric label="Win rate" value={`${state.win_rate}%`} tone={state.win_rate >= 50 ? "up" : "default"} />
          <Metric label="Trades" value={state.trades_today} sub="today" />
          <Metric label="Active" value={state.active_trades} tone="accent" />
        </div>
        <div className="mt-2.5 grid grid-cols-3 gap-2.5">
          <Metric label="Wins" value={state.wins} tone="up" />
          <Metric label="Losses" value={state.losses} tone="down" />
          <Metric label="Streak" value={state.consecutive_losses ? `${state.consecutive_losses}L` : "—"} tone={state.consecutive_losses ? "down" : "default"} />
        </div>

        {state.paused && (
          <div className="mt-3 flex items-center justify-between rounded-2xl bg-[var(--color-gold)]/12 px-3 py-2.5 text-sm text-[var(--color-gold)] ring-1 ring-[var(--color-gold)]/25">
            <span>Paused — {state.pause_reason}</span>
            <button onClick={() => api.resume()} className="rounded-lg bg-[var(--color-gold)]/15 px-2.5 py-1 text-xs font-semibold">Resume</button>
          </div>
        )}
      </GlassCard>

      <GlassCard delay={0.05}>
        <div className="label mb-3">Trading mode</div>
        <div className="grid grid-cols-3 gap-1.5 rounded-2xl bg-black/20 p-1.5">
          {modes.map((m) => {
            const active = state.mode === m;
            return (
              <button key={m} onClick={() => setMode(m)} className="relative rounded-xl py-2.5 text-[13px] font-semibold">
                {active && (
                  <motion.div
                    layoutId="mode-pill"
                    className="absolute inset-0 rounded-xl bg-[var(--color-accent)]/18 ring-1 ring-[var(--color-accent)]/45"
                    style={{ boxShadow: "0 0 20px -6px var(--color-accent)" }}
                    transition={{ type: "spring", stiffness: 420, damping: 34 }}
                  />
                )}
                <span className={`relative ${active ? "text-[var(--color-accent)]" : "text-dim"}`}>{modeLabels[m]}</span>
              </button>
            );
          })}
        </div>

        <button
          onClick={() => {
            sound.play("alert");
            api.kill();
          }}
          className="group mt-3 flex w-full items-center justify-center gap-2 rounded-2xl bg-[var(--color-down)]/12 py-3 text-[13px] font-bold uppercase tracking-wide text-[var(--color-down)] ring-1 ring-[var(--color-down)]/30 transition active:scale-[0.98]"
        >
          <span className="grid h-4 w-4 place-items-center rounded-full bg-[var(--color-down)] text-[10px] text-black">■</span>
          Kill Switch — Stop All
        </button>
      </GlassCard>
    </div>
  );
}
