import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { formatIndicatorValue } from "../format";
import { useStore } from "../store";
import type { AssetPipelineSnapshot, DriftStatus, PipelineStageMetric, PipelineTraceDetail, PipelineTraceSummary, RejectionReport } from "../types";
import { GlassCard, Pill, SectionTitle } from "./ui";
import { TradePlans } from "./TradePlans";

// ── Status -> color/label mapping (the only place this mapping lives) ──
// Matches the existing design system's tokens exactly -- no new colors
// introduced. Every stage's color is a pure function of a string the
// backend already sent; nothing here decides pass/fail itself.
const STATUS_TONE: Record<string, { tone: "up" | "down" | "warn" | "accent" | "neutral"; label: string }> = {
  passed: { tone: "up", label: "PASSED" },
  running: { tone: "accent", label: "RUNNING" },
  waiting: { tone: "warn", label: "WAITING" },
  rejected: { tone: "down", label: "REJECTED" },
  failed: { tone: "down", label: "FAILED" },
  success: { tone: "up", label: "PASSED" }, // backend StageMetric.status uses success/failure/skipped
  failure: { tone: "down", label: "FAILED" },
  skipped: { tone: "neutral", label: "SKIPPED" },
};

function statusOf(status: string | undefined | null) {
  return STATUS_TONE[status ?? "waiting"] ?? STATUS_TONE.waiting;
}

// Single source of truth for tone -> CSS var, so no ternary chain is
// repeated across the file (previously duplicated 5+ times).
const TONE_VAR: Record<string, string> = {
  up: "var(--color-up)", down: "var(--color-down)",
  accent: "var(--color-accent)", warn: "var(--color-gold)", neutral: "var(--color-ink-dim)",
};
function toneColor(tone: string): string {
  return TONE_VAR[tone] ?? TONE_VAR.neutral;
}
// Matches Pill's own established `bg-[var(--color-x)]/NN` arbitrary-value
// Tailwind pattern (see ui.tsx) instead of the CSS color-mix() function,
// which has no precedent elsewhere in this codebase.
const TONE_BG_CLASS: Record<string, string> = {
  up: "bg-[var(--color-up)]/18", down: "bg-[var(--color-down)]/18",
  accent: "bg-[var(--color-accent)]/18", warn: "bg-[var(--color-gold)]/18", neutral: "bg-white/8",
};
function toneBgClass(tone: string): string {
  return TONE_BG_CLASS[tone] ?? TONE_BG_CLASS.neutral;
}

function timeAgo(ts: number | null) {
  if (!ts) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

const STAGE_LABELS: Record<string, string> = {
  data_fetch: "Data Fetch", indicator_calc: "Indicators", strategy_eval: "Strategy Vote",
  market_context: "Market Context", confidence_score: "Confidence", precision_gate: "Precision Gate",
  signal_revalidate: "Revalidation", pattern_risk: "Pattern Risk (shadow)", recovery_check: "Recovery (shadow)",
  queue_add: "Queued", queue_wait: "Queue Wait", risk_check: "Risk Check", asset_check: "Correlation Guard",
  execution: "Execution", completion: "Complete",
};

// ── Live per-asset pipeline card ────────────────────────────────────────
// Why an attempt stopped where it did, in words rather than a stage id.
// A trace that ends anywhere before `completion` never became a tradeable
// signal at all -- which is why most Replay rows have no matching signal card.
const STOP_REASON: Record<string, string> = {
  data_fetch: "Couldn't get candles for this asset",
  indicator_calc: "Not enough history to compute indicators",
  strategy_eval: "No strategy voted for a trade here",
  market_context: "Market regime blocked it (dead/choppy market)",
  confidence_score: "Confidence came in below your threshold",
  precision_gate: "Too many strategies voted the other way",
  signal_revalidate: "Setup decayed before the candle opened",
  queue_add: "Never reached the execution queue",
  queue_wait: "Expired while waiting for its entry moment",
  risk_check: "Risk gate refused it (limits, cooldown, or paused)",
  asset_check: "That asset already had an open trade",
  execution: "The broker order failed or timed out",
  completion: "Completed",
};

function AssetPipelineCard({ snap }: { snap: AssetPipelineSnapshot }) {
  const [open, setOpen] = useState(false);
  const st = statusOf(snap.stage);

  return (
    <motion.div layout className="tile overflow-hidden p-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between p-3 text-left"
      >
        <div className="flex items-center gap-2.5">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: toneColor(st.tone), boxShadow: `0 0 8px -1px ${toneColor(st.tone)}` }}
          />
          <span className="text-[13px] font-semibold">{snap.asset.replace("_otc", "")}</span>
          <span className="text-[10.5px] text-faint">{snap.timeframe}</span>
          {snap.regime && <Pill tone="neutral">{snap.regime}</Pill>}
        </div>
        <div className="flex items-center gap-2">
          <Pill tone={st.tone}>{st.label}</Pill>
          <span className="nums text-[10.5px] text-faint">{timeAgo(snap.updated_at)}</span>
        </div>
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden border-t border-white/8"
          >
            <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-4">
              <MiniStat label="WS" value={snap.ws_connected ? "Connected" : "Down"} tone={snap.ws_connected ? "up" : "down"} />
              <MiniStat label="Close" value={snap.close != null ? snap.close.toFixed(5) : "—"} />
              <MiniStat label="Candle" value={timeAgo(snap.last_candle_ts)} />
              <MiniStat label="Volatility" value={snap.volatility != null ? snap.volatility.toFixed(3) : "—"} />
            </div>
            {snap.stage_reason && (
              <div className="mx-3 mb-2 rounded-lg bg-white/4 px-2.5 py-1.5 text-[11px] text-faint">
                {snap.stage_reason}
              </div>
            )}
            {Object.keys(snap.indicators).length > 0 && (
              <div className="mx-3 mb-3 flex flex-wrap gap-1.5">
                {Object.entries(snap.indicators).map(([k, v]) => (
                  <span key={k} className="nums rounded-md bg-white/5 px-2 py-1 text-[10.5px] text-white/70">
                    {k} <span className="text-white/95">{typeof v === "number" ? formatIndicatorValue(k, v) : v}</span>
                  </span>
                ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  const color = tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-white";
  return (
    <div className="rounded-lg bg-white/4 px-2.5 py-1.5">
      <div className="text-[9.5px] uppercase tracking-wide text-faint">{label}</div>
      <div className={`nums text-[12px] font-semibold ${color}`}>{value}</div>
    </div>
  );
}

// ── One row inside a signal's decision tree ─────────────────────────────
function StageRow({ stage }: { stage: PipelineStageMetric }) {
  const st = statusOf(stage.status);
  return (
    <div className="flex items-start gap-2.5 py-2">
      <div className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${toneBgClass(st.tone)}`}>
        <span className="h-1.5 w-1.5 rounded-full" style={{ background: toneColor(st.tone) }} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[12.5px] font-medium">{STAGE_LABELS[stage.stage] ?? stage.stage}</span>
          <span className="nums shrink-0 text-[10px] text-faint">{stage.duration_ms?.toFixed(0) ?? 0}ms</span>
        </div>
        {stage.error && <div className="mt-0.5 text-[11px] text-down">{stage.error}</div>}
        {!stage.error && stage.data && Object.keys(stage.data).length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {Object.entries(stage.data).slice(0, 6).map(([k, v]) => (
              <span key={k} className="nums rounded bg-white/5 px-1.5 py-0.5 text-[10px] text-white/60">
                {k}: {typeof v === "object" ? JSON.stringify(v).slice(0, 40) : String(v)}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function TraceDetailView({ stages }: { stages: PipelineStageMetric[] }) {
  if (stages.length === 0) return <div className="py-6 text-center text-[12px] text-faint">No stages recorded yet.</div>;
  return <div className="divide-y divide-white/6">{stages.map((s, i) => <StageRow key={`${s.stage}-${i}`} stage={s} />)}</div>;
}

// ── Enter control for a replay row ──────────────────────────────────────
/**
 * A Replay row is a HISTORICAL trace, so most of them cannot be entered:
 * either the pipeline rejected the attempt (it never became a signal), or the
 * signal's entry window has since closed. This control therefore only offers
 * "Enter" for a trace that still matches a live, in-window signal; everything
 * else shows why it is not enterable instead of a button that would place a
 * trade on a setup that no longer exists.
 *
 * The UI is not the guard, though -- /api/signals/{id}/execute re-checks
 * status, entry window, risk gate and execution state server-side and refuses
 * with a reason. This is a convenience, not a permission.
 */
function ReplayEnterButton({ signalId, expired }: { signalId: string; expired: boolean }) {
  const liveSignal = useStore((s) => s.signals.find((x) => x.id === signalId));
  const pushToast = useStore((s) => s.pushToast);
  const [busy, setBusy] = useState(false);

  const enterable = !!liveSignal && (liveSignal.status === "new" || liveSignal.status === "approved") && !expired;

  if (!enterable) {
    return (
      <span className="shrink-0 text-[10px] text-faint">
        {liveSignal ? (liveSignal.status === "executed" ? "traded" : liveSignal.status) : expired ? "window closed" : "not a signal"}
      </span>
    );
  }

  return (
    <button
      disabled={busy}
      onClick={async (e) => {
        e.stopPropagation();   // don't open the trace drawer
        setBusy(true);
        try {
          await api.execute(signalId);
          pushToast("info", "Order sent");
        } catch (err) {
          pushToast("warn", err instanceof Error ? err.message : "Execution failed");
        } finally {
          setBusy(false);
        }
      }}
      className="shrink-0 rounded-lg bg-[var(--color-up)]/15 px-2.5 py-1 text-[10.5px] font-bold text-[var(--color-up)] ring-1 ring-[var(--color-up)]/35 disabled:opacity-50"
    >
      {busy ? "…" : "ENTER"}
    </button>
  );
}

// ── Replay list + detail drawer ─────────────────────────────────────────
function ReplayPanel() {
  const [signals, setSignals] = useState<PipelineTraceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<PipelineTraceDetail | null>(null);
  const [selectedLoading, setSelectedLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.pipelineReplay(100).then((r) => { if (!cancelled) setSignals(r.signals); }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  async function openTrace(signalId: string) {
    setSelectedLoading(true);
    try {
      const detail = await api.pipelineTrace(signalId);
      setSelected(detail);
    } catch {
      /* trace may have aged out of the in-memory buffer */
    } finally {
      setSelectedLoading(false);
    }
  }

  return (
    <GlassCard>
      <SectionTitle right={<span className="text-[11px] text-faint">{signals.length} recent</span>}>Replay: Last 100 Signals</SectionTitle>
      {loading ? (
        <div className="py-6 text-center text-[12px] text-faint">Loading…</div>
      ) : signals.length === 0 ? (
        <div className="py-6 text-center text-[12px] text-faint">No signal attempts traced yet this session.</div>
      ) : (
        <div className="max-h-[360px] space-y-1.5 overflow-y-auto pr-1">
          {signals.map((sig) => {
            const isCall = sig.direction === "call";
            return (
              /* A <button> cannot legally contain another <button>, and the row
                 now holds an Enter control, so the row itself becomes a
                 clickable div. */
              <div
                key={sig.signal_id}
                role="button"
                tabIndex={0}
                onClick={() => openTrace(sig.signal_id)}
                className="flex w-full cursor-pointer items-center justify-between rounded-lg bg-white/4 px-2.5 py-2 text-left transition hover:bg-white/8"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[12px] font-medium">{sig.asset.replace("_otc", "")}</span>
                    <Pill tone={isCall ? "up" : "down"}>{isCall ? "▲" : "▼"}</Pill>
                    <span className="text-[10.5px] text-faint">{sig.final_stage ? (STAGE_LABELS[sig.final_stage] ?? sig.final_stage) : "—"}</span>
                  </div>
                  {!sig.successful && sig.final_stage && (
                    <div className="mt-0.5 truncate text-[10.5px] text-dim">
                      {STOP_REASON[sig.final_stage] ?? "Stopped before completion"}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <ReplayEnterButton
                    signalId={sig.signal_id}
                    expired={Date.now() / 1000 - sig.created_at > 300}
                  />
                  <Pill tone={sig.successful ? "up" : "down"}>{sig.successful ? "OK" : "STOPPED"}</Pill>
                  <span className="nums text-right text-[10.5px] text-faint">
                    {/* Exact clock time, not just "4m ago" -- when matching a
                        signal against a Quotex entry, the wall-clock time is
                        what you actually compare. */}
                    <span className="block">{new Date(sig.created_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
                    <span className="block text-faint">{timeAgo(sig.created_at)}</span>
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <AnimatePresence>
        {(selected || selectedLoading) && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 sm:items-center"
            onClick={() => setSelected(null)}
          >
            <motion.div
              initial={{ y: 40, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              exit={{ y: 20, opacity: 0 }}
              transition={{ type: "spring", stiffness: 380, damping: 34 }}
              onClick={(e) => e.stopPropagation()}
              className="glass-2 max-h-[80vh] w-full max-w-lg overflow-y-auto rounded-t-2xl p-4 sm:rounded-2xl"
            >
              {selectedLoading && <div className="py-8 text-center text-[12px] text-faint">Loading decision tree…</div>}
              {selected && (
                <>
                  <div className="mb-3 flex items-center justify-between">
                    <div>
                      <div className="text-[14px] font-semibold">{selected.asset.replace("_otc", "")} · {selected.direction.toUpperCase()}</div>
                      <div className="text-[10.5px] text-faint">{selected.total_duration_ms.toFixed(0)}ms total · {selected.stages.length} stages</div>
                    </div>
                    <Pill tone={selected.successful ? "up" : "down"}>{selected.successful ? "COMPLETED" : "STOPPED"}</Pill>
                  </div>
                  {selected.bugs_detected && selected.bugs_detected.length > 0 && (
                    <div className="mb-3 rounded-lg bg-[var(--color-down)]/10 px-2.5 py-1.5 text-[11px] text-down">
                      {selected.bugs_detected.join(" · ")}
                    </div>
                  )}
                  <TraceDetailView stages={selected.stages} />
                </>
              )}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </GlassCard>
  );
}

// ── Live in-flight traces (animate as they happen) ──────────────────────
function LiveTracesPanel() {
  const live = useStore((s) => s.pipelineLiveTraces);
  const entries = useMemo(
    () => Object.entries(live).sort((a, b) => b[1].startedAt - a[1].startedAt).slice(0, 8),
    [live],
  );
  if (entries.length === 0) return null;
  return (
    <GlassCard>
      <SectionTitle>Live Signal Attempts</SectionTitle>
      <div className="space-y-2">
        <AnimatePresence>
          {entries.map(([id, t]) => (
            <motion.div
              key={id}
              layout
              initial={{ opacity: 0, x: -12 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              className="tile p-3"
            >
              <div className="mb-1.5 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-[12.5px] font-semibold">{t.asset.replace("_otc", "")}</span>
                  <Pill tone={t.direction === "call" ? "up" : "down"}>{t.direction.toUpperCase()}</Pill>
                </div>
                <Pill tone={!t.completed ? "accent" : t.successful ? "up" : "down"}>
                  {!t.completed ? "RUNNING" : t.successful ? "COMPLETED" : "STOPPED"}
                </Pill>
              </div>
              <div className="flex flex-wrap gap-1">
                {t.stages.map((s, i) => {
                  const st = statusOf(s.status);
                  return (
                    <span key={i} className={`rounded-full px-2 py-0.5 text-[9.5px] font-medium ${toneBgClass(st.tone)}`}
                          style={{ color: toneColor(st.tone) }}>
                      {STAGE_LABELS[s.stage] ?? s.stage}
                    </span>
                  );
                })}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </GlassCard>
  );
}

// ── Top-level page ───────────────────────────────────────────────────────
// ── Rejection Analysis (Decision Intelligence Engine) ──────────────────
// Answers "was rejecting this trade the right call" with real evidence:
// what would have happened if a rejected signal had been taken anyway,
// broken down by which gate rejected it, judged against the PAYOUT-
// IMPLIED breakeven rate (not 50%) since binary options have a
// structural house edge. Empty until decision_engine_enabled is on and
// enough rejected signals have had time to resolve.
function RejectionAnalysisPanel() {
  const [report, setReport] = useState<RejectionReport | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api.rejectionsReport().then((r) => { if (!cancelled) setReport(r); }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  if (loading) return null;
  if (!report || report.cells.length === 0) {
    return (
      <GlassCard>
        <SectionTitle>Rejection Analysis</SectionTitle>
        <div className="py-6 text-center text-[12px] text-faint">
          No data yet -- turn on "Decision logging" in Settings and give rejected signals time to resolve.
        </div>
      </GlassCard>
    );
  }

  const verdictTone = (verdict: string): "up" | "down" | "warn" | "neutral" =>
    verdict.startsWith("Well-calibrated") ? "up" : verdict.startsWith("Possibly too conservative") ? "warn" : "neutral";

  return (
    <GlassCard>
      <SectionTitle right={<span className="text-[11px] text-faint">{report.sample_size} resolved</span>}>Rejection Analysis</SectionTitle>
      <div className="mb-3 text-[11px] text-faint">{report.note}</div>
      <div className="space-y-1.5">
        {report.cells.map((c) => {
          const tone = verdictTone(c.verdict);
          return (
            <div key={`${c.gate}|${c.regime}`} className="tile p-3">
              <div className="mb-1 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-[12.5px] font-semibold">{c.gate}</span>
                  <Pill tone="neutral">{c.regime}</Pill>
                </div>
                <Pill tone={tone}>{c.win_rate}% [{c.wilson_lower}-{c.wilson_upper}]</Pill>
              </div>
              <div className="text-[11px] text-faint">{c.verdict}</div>
              <div className="nums mt-1 text-[10px] text-white/50">
                {c.total} rejected · {c.wins}W/{c.losses}L · sim. PnL {c.total_pnl >= 0 ? "+" : ""}{c.total_pnl}
              </div>
            </div>
          );
        })}
      </div>
    </GlassCard>
  );
}

// ── Drift Detection status ──────────────────────────────────────────────
// Page-Hinkley accumulated evidence per (strategy, regime) cell -- lets a
// user see a cell approaching its alert threshold before it actually
// fires. Empty until drift_detection_enabled is on.
function DriftStatusPanel() {
  const [status, setStatus] = useState<DriftStatus | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api.driftStatus().then((r) => { if (!cancelled) setStatus(r); }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  if (loading || !status || status.cells.length === 0) return null;

  return (
    <GlassCard>
      <SectionTitle right={<span className="text-[11px] text-faint">threshold {status.threshold}</span>}>Drift Detection</SectionTitle>
      <div className="mb-2 text-[11px] text-faint">
        Page-Hinkley accumulated evidence of a win-rate shift, per strategy/regime -- fires before the rolling-window mute check would notice.
      </div>
      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
        {status.cells.map((c) => {
          const pct = Math.min(100, (c.cumulative_sum / c.threshold) * 100);
          return (
            <div key={`${c.strategy}|${c.regime}`} className="tile p-2.5">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-[11.5px] font-medium">{c.strategy}</span>
                <Pill tone="neutral">{c.regime}</Pill>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-white/8">
                <div className="h-full rounded-full" style={{
                  width: `${pct}%`,
                  background: pct >= 80 ? "var(--color-down)" : pct >= 50 ? "var(--color-gold)" : "var(--color-accent)",
                }} />
              </div>
              <div className="nums mt-1 text-[10px] text-faint">
                {c.cumulative_sum.toFixed(2)} / {c.threshold} · n={c.sample_count}
                {c.total_alerts > 0 && ` · ${c.total_alerts} alert${c.total_alerts > 1 ? "s" : ""}`}
              </div>
            </div>
          );
        })}
      </div>
    </GlassCard>
  );
}

export function PipelineDashboard() {
  const snapshots = useStore((s) => s.pipelineSnapshots);
  const analytics = useStore((s) => s.analytics);
  const assets = useMemo(
    () => Object.values(snapshots).sort((a, b) => a.asset.localeCompare(b.asset)),
    [snapshots],
  );

  return (
    <div className="space-y-4 pb-4">
      {/* First on the page: the concrete trade each live signal resolves to.
          This is the number a manual entry is copied from, so it goes above
          the diagnostics rather than below them. */}
      <TradePlans />

      {analytics?.recovery && (
        <GlassCard>
          <SectionTitle>Recovery Engine (shadow, advisory only)</SectionTitle>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <MiniStat label="Recovery Score" value={analytics.recovery.recovery_score.toFixed(0)} />
            <MiniStat label="Sample" value={String(analytics.recovery.sample_size)} />
            <MiniStat label="Wilson Lower" value={`${analytics.recovery.wilson_lower.toFixed(1)}%`} />
            <MiniStat label="Resume?" value={analytics.recovery.resume_recommended ? "Yes" : "Not yet"} tone={analytics.recovery.resume_recommended ? "up" : "down"} />
          </div>
          {analytics.recovery.note && <div className="mt-2 text-[11px] text-faint">{analytics.recovery.note}</div>}
        </GlassCard>
      )}

      <GlassCard>
        <SectionTitle right={<span className="text-[11px] text-faint">{assets.length} assets</span>}>Live Pipeline</SectionTitle>
        {assets.length === 0 ? (
          <div className="py-8 text-center text-[12px] text-faint">
            No asset activity yet this session — the pipeline populates as the scan loop runs.
          </div>
        ) : (
          <div className="space-y-1.5">
            {assets.map((snap) => (
              <AssetPipelineCard key={`${snap.asset}|${snap.timeframe}`} snap={snap} />
            ))}
          </div>
        )}
      </GlassCard>

      <LiveTracesPanel />
      <DriftStatusPanel />
      <RejectionAnalysisPanel />
      <ReplayPanel />
    </div>
  );
}
