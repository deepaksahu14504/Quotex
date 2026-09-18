import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { StatusLevel, SystemStatus } from "../types";

const COLOR: Record<StatusLevel, string> = {
  ok: "var(--color-up)",
  warn: "var(--color-gold)",
  error: "var(--color-down)",
  info: "var(--color-ink-dim)",
};

function Dot({ level }: { level: StatusLevel }) {
  return (
    <span
      className="relative grid h-7 w-7 shrink-0 place-items-center rounded-full"
      style={{ background: `${COLOR[level]}1f` }}
    >
      <span className="h-2.5 w-2.5 rounded-full" style={{ background: COLOR[level], boxShadow: `0 0 8px ${COLOR[level]}` }} />
    </span>
  );
}

function fmtUptime(s: number) {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function StatusModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [restartMsg, setRestartMsg] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      setStatus(await api.status());
      setLoadError(null);
    } catch (e) {
      // Previously this swallowed the reason and left the panel on
      // "Loading status…" forever -- which is exactly how a 500 from
      // /api/status stayed invisible.
      setStatus(null);
      setLoadError(e instanceof Error ? e.message : "Status request failed");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [open]);

  const overall = status?.overall ?? "info";
  const overallLabel = overall === "ok" ? "All systems operational" : overall === "warn" ? "Degraded — needs attention" : overall === "error" ? "Problem detected" : "Checking…";
  // "engine" is the check pipeline_health() feeds into (see /api/status on
  // the backend) -- error here specifically means a background loop is
  // stuck or the scan heartbeat has gone quiet too long, i.e. exactly what
  // Restart Pipeline exists to fix. Kept as a derived read of the existing
  // 5s status poll rather than a second poll loop against pipelineHealth().
  const engineCheck = status?.checks.find((c) => c.key === "engine");
  const pipelineStuck = engineCheck?.status === "error";

  const doRestart = async () => {
    setRestarting(true);
    setRestartMsg(null);
    try {
      const res = await api.pipelineRestart();
      setRestartMsg(res.message);
      await refresh();
    } catch {
      setRestartMsg("Restart failed — try again in a moment.");
    } finally {
      setRestarting(false);
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
          <motion.div
            initial={{ y: 40, opacity: 0, scale: 0.98 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 40, opacity: 0 }}
            transition={{ type: "spring", stiffness: 320, damping: 32 }}
            className="glass glass-2 relative z-10 max-h-[88vh] w-full max-w-lg overflow-y-auto p-5 safe-bottom"
          >
            <div className="mb-4 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Dot level={overall} />
                <div>
                  <h2 className="text-lg font-bold leading-tight">System Status</h2>
                  <div className="text-[12px]" style={{ color: COLOR[overall] }}>{overallLabel}</div>
                </div>
              </div>
              <button onClick={onClose} className="rounded-full bg-white/10 px-3 py-1 text-sm">Close</button>
            </div>

            <div className="space-y-2">
              {!status && !loadError && <div className="py-10 text-center text-dim">Loading status…</div>}
              {!status && loadError && (
                <div className="py-8 text-center text-[12px] text-down">
                  Status unavailable — {loadError}
                  <div className="mt-1 text-faint">The backend /api/status call is failing. Check server logs.</div>
                </div>
              )}
              {status?.checks.map((c, i) => (
                <motion.div
                  key={c.key}
                  initial={{ opacity: 0, x: 12 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: i * 0.03 }}
                  className="tile flex items-center gap-3 p-3"
                >
                  <Dot level={c.status} />
                  <div className="min-w-0 flex-1">
                    <div className="text-[13.5px] font-semibold">{c.label}</div>
                    <div className="truncate text-[11.5px] text-faint">{c.detail}</div>
                  </div>
                  <span className="text-[11px] font-bold uppercase tracking-wide" style={{ color: COLOR[c.status] }}>
                    {c.status}
                  </span>
                </motion.div>
              ))}
            </div>

            <div className="mt-4 flex items-center justify-between text-[11px] text-faint">
              <span>{status ? `Uptime ${fmtUptime(status.uptime)}` : ""}</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={doRestart}
                  disabled={restarting}
                  className={
                    pipelineStuck
                      ? "rounded-lg bg-[var(--color-down)]/15 px-3 py-1.5 font-semibold text-[var(--color-down)] ring-1 ring-[var(--color-down)]/40 transition active:scale-95 disabled:opacity-60"
                      : "rounded-lg bg-white/8 px-3 py-1.5 font-semibold text-dim ring-1 ring-white/10 transition active:scale-95 disabled:opacity-60"
                  }
                >
                  {restarting ? "Restarting…" : "Restart Pipeline"}
                </button>
                <button onClick={refresh} className="rounded-lg bg-white/8 px-3 py-1.5 font-semibold text-dim ring-1 ring-white/10 transition active:scale-95">
                  {loading ? "Refreshing…" : "Refresh"}
                </button>
              </div>
            </div>
            {restartMsg && (
              <div className="mt-2 text-right text-[11px] text-faint">{restartMsg}</div>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
