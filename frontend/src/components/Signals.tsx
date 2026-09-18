import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";
import { api } from "../api";
import { sound } from "../sound";
import { useStore } from "../store";
import type { Signal, Trade } from "../types";
import { ConfidenceRing, GlassCard, Pill, SectionTitle } from "./ui";

function timeAgo(ts: number) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function clockTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Quotex expiry selector wants minutes, not seconds. */
function expiryLabel(seconds: number) {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function OpenPositionRow({ t, now }: { t: Trade; now: number }) {
  const isCall = t.direction === "call";
  const color = isCall ? "var(--color-up)" : "var(--color-down)";
  const expiry = t.created_at + t.duration;
  const remaining = Math.max(0, Math.ceil(expiry - now));
  const pct = Math.min(100, Math.max(0, ((t.duration - remaining) / t.duration) * 100));
  return (
    <div className="tile relative overflow-hidden p-3">
      <div className="absolute left-0 top-0 h-full w-[3px]" style={{ background: color }} />
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold">{t.asset.replace("_otc", "")}</span>
          <Pill tone={isCall ? "up" : "down"}>{isCall ? "▲ CALL" : "▼ PUT"}</Pill>
        </div>
        <span className="nums text-[12px] font-semibold" style={{ color }}>{remaining}s left</span>
      </div>
      <div className="mt-1.5 flex items-center justify-between text-[10.5px] text-faint">
        <span className="nums">${t.amount} · {t.confidence ?? "—"}% conf · payout {t.payout}%</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/6">
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

function RegimeBadge({ regime }: { regime: string }) {
  const map: Record<string, { label: string; icon: string; color: string }> = {
    trend: { label: "Trending", icon: "📈", color: "var(--color-up)" },
    range: { label: "Ranging", icon: "🔄", color: "var(--color-gold)" },
    mixed: { label: "Mixed", icon: "⚡", color: "var(--color-ink-dim)" },
  };
  const m = map[regime] ?? map.mixed;
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-white/6 px-1.5 py-0.5" style={{ color: m.color }}>
      {m.icon} {m.label}
    </span>
  );
}

function SignalRow({ sig, now }: { sig: Signal; now: number }) {
  const mode = useStore((s) => s.state?.mode);
  const pushToast = useStore((s) => s.pushToast);
  const [entering, setEntering] = useState(false);
  const [enterError, setEnterError] = useState<string | null>(null);
  const isCall = sig.direction === "call";
  const live = sig.status === "new";
  // Entry window: how long this signal is still enterable. expires_at comes
  // from the backend using the SAME function the execution gate uses, so this
  // countdown is the real deadline rather than a UI guess.
  const secondsLeft = sig.expires_at ? Math.max(0, Math.ceil(sig.expires_at - now)) : null;
  const windowTone = secondsLeft === null ? "var(--color-ink-faint)"
    : secondsLeft <= 10 ? "var(--color-down)"
    : secondsLeft <= 30 ? "var(--color-gold)"
    : "var(--color-up)";
  const color = isCall ? "var(--color-up)" : "var(--color-down)";
  const setupReasons = sig.reasons.filter((r) => !/^Regime/.test(r));
  const primaryReason = setupReasons[0] ?? sig.reasons[0];
  const extraCount = Math.max(0, setupReasons.length - 1);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.97 }}
      transition={{ type: "spring", stiffness: 320, damping: 30 }}
      className="tile relative overflow-hidden p-4"
      style={{ opacity: live ? 1 : 0.55 }}
    >
      <div className="absolute left-0 top-0 h-full w-[3px]" style={{ background: color, boxShadow: `0 0 12px ${color}` }} />
      <div className="flex items-center gap-3">
        <ConfidenceRing value={sig.confidence} size={46} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[14px] font-semibold tracking-tight">{sig.asset.replace("_otc", "")}</span>
            <Pill tone={isCall ? "up" : "down"}>{isCall ? "▲ CALL" : "▼ PUT"}</Pill>
            <span className="nums ml-auto shrink-0 text-[11px] text-faint">{sig.payout}%</span>
          </div>
          {primaryReason && (
            <div className="mt-1 truncate text-[12px] text-dim">
              {primaryReason}
              {extraCount > 0 && <span className="text-faint"> · +{extraCount}</span>}
            </div>
          )}
          <div className="nums mt-0.5 flex items-center gap-1.5 text-[10.5px] text-faint">
            <span>
              {clockTime(sig.created_at)} · {sig.timeframe} · {timeAgo(sig.created_at)}
              {!live && ` · ${sig.status}`}
            </span>
            {sig.regime && <RegimeBadge regime={sig.regime} />}
          </div>
          {live && (
            <div className="nums mt-1.5 flex items-center gap-2 text-[11px]">
              <span className="rounded-md bg-white/8 px-1.5 py-0.5">
                Quotex expiry <b>{expiryLabel(sig.duration)}</b>
              </span>
              {secondsLeft !== null && (
                <span className="rounded-md px-1.5 py-0.5 font-semibold"
                      style={{ color: windowTone, background: `${windowTone}1f` }}>
                  {secondsLeft > 0 ? `enter within ${secondsLeft}s` : "window closed"}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {live && mode !== "auto" && (
        <div className="mt-3 grid grid-cols-2 gap-2">
          <button
            disabled={entering}
            onClick={async () => {
              // BUG FIX: this used to be `api.execute(sig.id)` with no await
              // and no catch. The promise was dropped on the floor, so the
              // server's 409 -- which carries the actual reason -- was thrown
              // away and pressing Enter Trade looked like it did nothing.
              sound.play("click");
              setEntering(true);
              setEnterError(null);
              try {
                await api.execute(sig.id);
                pushToast("info", `Order sent: ${sig.asset} ${sig.direction.toUpperCase()}`);
              } catch (e) {
                const msg = e instanceof Error ? e.message : "Execution failed";
                setEnterError(msg);
                pushToast("warn", msg);
              } finally {
                setEntering(false);
              }
            }}
            className="rounded-xl bg-[var(--color-up)]/15 py-2.5 text-[13px] font-semibold text-[var(--color-up)] ring-1 ring-[var(--color-up)]/35 transition active:scale-95 disabled:opacity-50"
          >
            {entering ? "Sending…" : "Enter Trade"}
          </button>
          <button
            disabled={entering}
            onClick={() => { sound.play("click"); api.skip(sig.id).catch(() => {}); }}
            className="rounded-xl bg-white/4 py-2.5 text-[13px] font-semibold text-dim ring-1 ring-white/8 transition active:scale-95 disabled:opacity-50"
          >
            Skip
          </button>
          {enterError && (
            <div className="col-span-2 text-[11px] text-down">{enterError}</div>
          )}
        </div>
      )}
    </motion.div>
  );
}

export function Signals() {
  const signals = useStore((s) => s.signals);
  const trades = useStore((s) => s.trades);
  const mode = useStore((s) => s.state?.mode);
  const [now, setNow] = useState(Date.now() / 1000);

  const openPositions = trades.filter((t) => t.status === "open");
  const liveSignals = signals.filter((s) => s.status === "new");

  useEffect(() => {
    if (openPositions.length === 0) return;
    const id = setInterval(() => setNow(Date.now() / 1000), 500);
    return () => clearInterval(id);
  }, [openPositions.length]);

  return (
    <GlassCard className="flex h-full flex-col">
      {/* Open positions = trades the system has ENTERED */}
      {openPositions.length > 0 && (
        <div className="mb-4">
          <SectionTitle right={<Pill tone="up">{openPositions.length} open</Pill>}>Open Positions</SectionTitle>
          <div className="space-y-2">
            {openPositions.map((t) => <OpenPositionRow key={t.id} t={t} now={now} />)}
          </div>
        </div>
      )}

      {/* Live signals = potential trades the engine FOUND (not yet entered) */}
      <div className="mb-1 flex items-center justify-between">
        <div>
          <h2 className="text-[15px] font-semibold tracking-tight text-white/90">Live Signals</h2>
          <p className="text-[11px] text-faint">
            Opportunities found by the engine · {mode === "auto" ? "auto-entering" : "tap Enter to trade"}
          </p>
        </div>
        <Pill tone={liveSignals.length ? "accent" : "neutral"}>{liveSignals.length} live</Pill>
      </div>

      <div className="mt-2 flex-1 space-y-2.5 overflow-y-auto pr-1">
        {signals.length === 0 && (
          <div className="flex h-48 flex-col items-center justify-center text-center text-dim">
            <div className="mb-3 grid h-12 w-12 place-items-center rounded-2xl border border-white/10">
              <motion.div animate={{ rotate: 360 }} transition={{ duration: 3, repeat: Infinity, ease: "linear" }} className="h-5 w-5 rounded-full border-2 border-white/15 border-t-[var(--color-accent)]" />
            </div>
            <div className="text-sm">Scanning markets…</div>
            <div className="mt-1 text-[11px] text-faint">High-probability setups will appear here</div>
          </div>
        )}
        <AnimatePresence initial={false}>
          {signals.map((sig) => <SignalRow key={sig.id} sig={sig} now={now} />)}
        </AnimatePresence>
      </div>
    </GlassCard>
  );
}
