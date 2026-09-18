import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";
import { Chart } from "./components/Chart";
import { Clock } from "./components/Clock";
import { Dashboard } from "./components/Dashboard";
import { FogCanvas } from "./components/FogCanvas";
import { History } from "./components/History";
import { Backtest } from "./components/Backtest";
import { Validation } from "./components/Validation";
import { Insights } from "./components/Insights";
import { Login } from "./components/Login";
import { OtpModal } from "./components/OtpModal";
import { PipelineDashboard } from "./components/PipelineDashboard";
import { Settings } from "./components/Settings";
import { Signals } from "./components/Signals";
import { StatusModal } from "./components/StatusModal";
import { Strategy } from "./components/Strategy";
import { Toasts } from "./components/Toasts";
import { GlassCard } from "./components/ui";
import { api } from "./api";
import { applyAccent } from "./accentPresets";
import { useStore } from "./store";
import type { StatusLevel } from "./types";

type Tab = "home" | "signals" | "insights" | "history" | "strategy" | "backtest" | "validation" | "pipeline";

const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
  { id: "home", label: "Home", icon: <IconHome /> },
  { id: "signals", label: "Signals", icon: <IconSignal /> },
  { id: "pipeline", label: "Pipeline", icon: <IconFlow /> },
  { id: "insights", label: "Insights", icon: <IconChart /> },
  { id: "backtest", label: "Backtest", icon: <IconFlask /> },
  { id: "validation", label: "Validate", icon: <IconShield /> },
  { id: "history", label: "History", icon: <IconClock /> },
  { id: "strategy", label: "Strategy", icon: <IconGear /> },
];

export default function App() {
  const init = useStore((s) => s.init);
  const user = useStore((s) => s.user);
  const authChecked = useStore((s) => s.authChecked);
  const logout = useStore((s) => s.logout);
  const settings = useStore((s) => s.settings);
  const state = useStore((s) => s.state);
  const wsConnected = useStore((s) => s.wsConnected);
  const [tab, setTab] = useState<Tab>("home");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [statusOpen, setStatusOpen] = useState(false);
  const [statusLevel, setStatusLevel] = useState<StatusLevel>("info");

  useEffect(() => {
    init().catch(() => {});
  }, [init]);

  useEffect(() => {
    if (!user) return;
    const poll = () => api.status().then((s) => setStatusLevel(s.overall)).catch(() => setStatusLevel("error"));
    poll();
    const id = setInterval(poll, 20000);
    return () => clearInterval(id);
  }, [user]);

  useEffect(() => {
    applyAccent(settings?.ux.accent);
  }, [settings?.ux.accent]);

  const statusColor: Record<StatusLevel, string> = {
    ok: "var(--color-up)",
    warn: "var(--color-gold)",
    error: "var(--color-down)",
    info: "var(--color-ink-dim)",
  };

  const fog = settings?.ux.reduced_motion ? 0 : settings?.ux.fog_intensity ?? 0.6;

  if (!authChecked) {
    return (
      <div className="relative grid min-h-dvh place-items-center">
        <div className="bg-scene" />
        <div className="text-[13px] text-faint">Loading…</div>
      </div>
    );
  }

  if (!user) {
    return <Login />;
  }

  return (
    <div className="relative min-h-full">
      <div className="bg-scene" />
      <FogCanvas intensity={fog} />
      <Toasts />

      <header className="safe-top sticky top-0 z-40 px-4 pt-2">
        <div className="glass mx-auto flex max-w-7xl items-center justify-between !rounded-2xl px-3.5 py-2.5">
          <div className="flex items-center gap-2.5">
            <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-accent-2)] text-[15px] font-black text-black shadow-lg">Q</div>
            <div>
              <div className="text-[13px] font-bold leading-tight tracking-tight">QuotexAutoTrader</div>
              <div className="flex items-center gap-1.5 text-[10px] text-faint">
                <span className={`h-1.5 w-1.5 rounded-full ${wsConnected ? "bg-[var(--color-up)]" : "bg-[var(--color-down)]"}`} />
                {wsConnected ? "Live" : "Reconnecting"} · {state?.provider ?? "—"}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setStatusOpen(true)}
              className="flex items-center gap-1.5 rounded-xl bg-white/5 px-2.5 py-1.5 ring-1 ring-white/8 transition active:scale-95"
              title="System status"
            >
              <span className="h-2 w-2 rounded-full" style={{ background: statusColor[statusLevel], boxShadow: `0 0 8px ${statusColor[statusLevel]}` }} />
              <span className="hidden text-[12px] font-semibold text-dim sm:inline">Status</span>
            </button>
            <Clock />
            {state && (
              <div className="nums hidden rounded-xl bg-white/5 px-3 py-1.5 text-right sm:block">
                <div className="text-[13px] font-bold leading-none">${state.balance.toFixed(2)}</div>
                <div className={`text-[10px] ${state.daily_pnl >= 0 ? "text-up" : "text-down"}`}>
                  {state.daily_pnl >= 0 ? "+" : "-"}${Math.abs(state.daily_pnl).toFixed(2)} today
                </div>
              </div>
            )}
            <button
              onClick={() => setSettingsOpen(true)}
              className="grid h-9 w-9 place-items-center rounded-xl bg-white/6 text-dim ring-1 ring-white/8 transition hover:text-white active:scale-90"
              aria-label="Settings"
            >
              <IconGear />
            </button>
            <button
              onClick={() => logout()}
              className="grid h-9 w-9 place-items-center rounded-xl bg-white/6 text-dim ring-1 ring-white/8 transition hover:text-white active:scale-90"
              aria-label="Log out"
              title={user.email}
            >
              <IconLogout />
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-md px-4 pb-28 pt-4 sm:max-w-3xl xl:max-w-7xl">
        <AnimatePresence mode="wait">
          <motion.div
            key={tab}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
          >
            {tab === "home" && (
              <div className="grid gap-4 lg:h-[calc(100dvh-7.5rem)] lg:grid-cols-12">
                <div className="lg:col-span-3 lg:overflow-y-auto lg:pr-1">
                  <Dashboard />
                </div>
                <GlassCard className="h-[46vh] lg:col-span-6 lg:h-full" delay={0.05}>
                  <Chart timeframe={settings?.trading.timeframe ?? "1m"} />
                </GlassCard>
                <div className="hidden lg:col-span-3 lg:block lg:h-full lg:min-h-0">
                  <Signals />
                </div>
              </div>
            )}
            {tab === "signals" && (
              <div className="h-[calc(100dvh-9.5rem)]">
                <Signals />
              </div>
            )}
            {tab === "insights" && <Insights />}
            {tab === "history" && <History />}
            {tab === "backtest" && <Backtest />}
            {tab === "validation" && <Validation />}
            {tab === "pipeline" && <PipelineDashboard />}
            {tab === "strategy" && <Strategy />}
          </motion.div>
        </AnimatePresence>
      </main>

      {/* Scrim so scrolling content fades out behind the floating nav */}
      <div className="pointer-events-none fixed inset-x-0 bottom-0 z-30 h-24 bg-gradient-to-t from-[var(--color-bg)] via-[var(--color-bg)]/80 to-transparent" />

      <nav className="safe-bottom fixed bottom-0 left-1/2 z-40 w-full max-w-md -translate-x-1/2 px-4 pb-3">
        <div className="glass glass-2 flex items-center justify-around !rounded-2xl p-1.5">
          {TABS.map((t) => {
            const active = tab === t.id;
            return (
              <button key={t.id} onClick={() => setTab(t.id)} className={`relative flex flex-1 flex-col items-center gap-1 rounded-xl py-2 text-[10px] ${t.id === "signals" ? "lg:hidden" : ""}`}>
                {active && (
                  <motion.div layoutId="tab-pill" className="absolute inset-0 rounded-xl bg-white/8" transition={{ type: "spring", stiffness: 420, damping: 32 }} />
                )}
                <span className={`relative transition-colors ${active ? "text-[var(--color-accent)]" : "text-faint"}`}>{t.icon}</span>
                <span className={`relative font-medium ${active ? "text-white" : "text-faint"}`}>{t.label}</span>
              </button>
            );
          })}
        </div>
      </nav>

      <Settings open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <StatusModal open={statusOpen} onClose={() => setStatusOpen(false)} />
      <OtpModal />
    </div>
  );
}

function IconHome() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9.5V21h14V9.5" /></svg>;
}
function IconSignal() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 12a8 8 0 0 1 8-8" /><path d="M4 18a14 14 0 0 1 14-14" opacity=".5" /><circle cx="5" cy="19" r="1.4" fill="currentColor" stroke="none" /></svg>;
}
function IconChart() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19V5" /><path d="M4 19h16" /><path d="M7 15l3-4 3 2 4-6" /></svg>;
}
function IconClock() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="8" /><path d="M12 8v4l2.5 2" /></svg>;
}
function IconFlask() {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 3h6" /><path d="M10 3v6.5L4.5 19a1.5 1.5 0 0 0 1.3 2.2h12.4a1.5 1.5 0 0 0 1.3-2.2L14 9.5V3" /><path d="M8.5 15h7" /></svg>;
}
function IconShield() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>;
}
function IconGear() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>;
}
function IconLogout() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></svg>;
}
function IconFlow() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="5" cy="6" r="2.2" /><circle cx="19" cy="6" r="2.2" /><circle cx="12" cy="18" r="2.2" /><path d="M7 6h10" /><path d="M6 8.2 11 15.8" /><path d="M18 8.2 13 15.8" /></svg>;
}
