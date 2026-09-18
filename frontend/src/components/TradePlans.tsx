import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { Signal, TradePlan } from "../types";
import { GlassCard, Pill, SectionTitle } from "./ui";

function clockTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Quotex's expiry selector is in minutes, not seconds. */
function expiryLabel(seconds: number) {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function Field({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-faint">{label}</span>
      <b style={color ? { color } : undefined}>{value}</b>
    </div>
  );
}

/**
 * One resolved trade: when to enter, when it expires, how much is staked.
 *
 * Every value comes from the backend's own TradePlan, which is built by the
 * same functions the auto-executor runs (`_candle_phase` for entry timing,
 * `RiskManager.stake_for` for size). Nothing is recomputed in the browser, so
 * what this panel shows and what the bot does cannot drift apart.
 */
function PlanCard({ sig, plan, now, onDone }: { sig: Signal; plan: TradePlan; now: number; onDone: () => void }) {
  const pushToast = useStore((s) => s.pushToast);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isCall = plan.direction === "call";
  const color = isCall ? "var(--color-up)" : "var(--color-down)";
  // Read the pinned instant. Never re-derive a countdown in the browser --
  // the backend owns the one schedule that auto-trade and the manual button
  // both use.
  const scheduled = plan.scheduled_entry_at ?? plan.entry_at;
  const entryIn = Math.max(0, Math.ceil(scheduled - now));
  const windowLeft = plan.valid_until ? Math.max(0, Math.ceil(plan.valid_until - now)) : null;
  const windowOpen = windowLeft === null || windowLeft > 0;

  // Pressing the main button used to fire IMMEDIATELY while the card above it
  // said "enter at 10:07:00" -- the label even admitted it ("Enter now (12s
  // early)"). The plan's instant is now honoured by default and entering early
  // is a separate, deliberate press.
  const enter = async (enterNow: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await api.execute(sig.id, { enterNow });
      pushToast("info", `Order sent: ${plan.asset} ${plan.direction.toUpperCase()}`);
      onDone();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Execution failed";
      setError(msg);
      pushToast("warn", msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="tile relative overflow-hidden p-3">
      <div className="absolute left-0 top-0 h-full w-[3px]" style={{ background: color }} />

      <div className="mb-2 flex items-center gap-2">
        <span className="text-[13.5px] font-semibold">{plan.asset.replace("_otc", "")}</span>
        <Pill tone={isCall ? "up" : "down"}>{isCall ? "▲ CALL" : "▼ PUT"}</Pill>
        <span className="nums text-[11px] text-faint">{sig.confidence}% conf</span>
        <span className="ml-auto shrink-0">
          {plan.will_auto_execute ? (
            <span className="rounded-full bg-[var(--color-up)]/15 px-2 py-0.5 text-[10px] font-bold text-[var(--color-up)]">
              AUTO {entryIn > 0 ? `IN ${entryIn}s` : "NOW"}
            </span>
          ) : (
            <span className="rounded-full bg-[var(--color-gold)]/15 px-2 py-0.5 text-[10px] font-bold text-[var(--color-gold)]">
              MANUAL ONLY
            </span>
          )}
        </span>
      </div>

      <div className="nums grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11.5px]">
        <Field label="Enter at" value={clockTime(plan.entry_at)} />
        <Field label="Expires" value={clockTime(plan.expiry_at)} />
        <Field label="Expiry time" value={expiryLabel(plan.duration_seconds)} />
        <Field label="Payout" value={`${plan.payout_pct}%`} />
        <Field label="Stake" value={`$${plan.stake.toFixed(2)}`} />
        <Field label="If it wins" value={`+$${plan.potential_profit.toFixed(2)}`} color="var(--color-up)" />
      </div>

      {plan.waits_for_candle_open && (
        <div className="mt-2 text-[10.5px] text-gold">
          Waiting for the next candle open ({plan.candle_phase} phase) — enter at{" "}
          {clockTime(plan.entry_at)}, not now.
        </div>
      )}
      {windowLeft !== null && (
        <div className="mt-1 text-[10.5px] text-faint">
          {windowLeft > 0 ? `Entry window closes in ${windowLeft}s` : "Entry window closed"}
        </div>
      )}
      {plan.blocked_reason && (
        <div className="mt-1.5 text-[10.5px] text-down">
          Auto-trade will not fire: {plan.blocked_reason}
        </div>
      )}
      {plan.stake_is_estimate && (
        <div className="mt-1 text-[10px] text-faint">
          Stake is projected from the balance right now — if a trade settles before entry, the bot
          recomputes it.
        </div>
      )}

      <div className="mt-2.5 grid grid-cols-2 gap-2">
        <button
          disabled={busy || !windowOpen}
          onClick={() => enter(false)}
          className="rounded-xl bg-[var(--color-up)]/15 py-2 text-[12.5px] font-semibold text-[var(--color-up)] ring-1 ring-[var(--color-up)]/35 transition active:scale-95 disabled:opacity-40"
        >
          {busy
            ? scheduled
              ? `Waiting for ${clockTime(scheduled)}…`
              : "Sending…"
            : !windowOpen
              ? "Window closed"
              : entryIn > 0
                ? `Enter at ${clockTime(plan.entry_at)} (${entryIn}s)`
                : "Enter Trade"}
        </button>
        <button
          disabled={busy}
          onClick={() => { api.skip(sig.id).catch(() => {}); onDone(); }}
          className="rounded-xl bg-white/4 py-2 text-[12.5px] font-semibold text-dim ring-1 ring-white/8 transition active:scale-95 disabled:opacity-40"
        >
          Skip
        </button>
      </div>
      {entryIn > 0 && windowOpen && (
        <button
          disabled={busy}
          onClick={() => enter(true)}
          className="mt-1.5 w-full rounded-xl bg-white/4 py-1.5 text-[11px] font-semibold text-gold ring-1 ring-[var(--color-gold)]/25 transition active:scale-95 disabled:opacity-40"
        >
          Enter now instead ({entryIn}s early)
        </button>
      )}
      {entryIn > 0 && windowOpen && (
        <div className="mt-1 text-[10px] text-faint">
          The main button holds the order until {clockTime(plan.entry_at)} — the same instant
          auto-trade aims at. Entering now means a different candle and a different entry price.
        </div>
      )}
      {plan.expiry_basis === "broker_receipt" && (
        <div className="mt-1 text-[10px] text-faint">
          Expiry is counted by Quotex from when it receives the order, so it is not pinned to the
          candle grid — a late fill shifts the expiry by the same amount.
        </div>
      )}
      {error && <div className="mt-1.5 text-[11px] text-down">{error}</div>}
    </div>
  );
}

/**
 * The Pipeline page's answer to "what exactly will the bot do with this
 * signal?" — the concrete trade each finalized signal resolves to, including
 * the reason when it will NOT be placed. Previously that lived only in the
 * server logs.
 */
export function TradePlans() {
  // Seed from the store so a brand-new signal shows instantly over the
  // websocket, then keep it fresh by polling. The websocket only pushes once,
  // at signal creation, but `stake` and `blocked_reason` are time-dependent --
  // a plan frozen at creation would keep claiming a stake the bot would no
  // longer use, or a block that has since cleared. /api/signals rebuilds the
  // plan on every request for exactly this reason.
  const storeSignals = useStore((s) => s.signals);
  const [fetched, setFetched] = useState<Signal[] | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      api.signals()
        .then((r) => { if (!cancelled) setFetched(r); })
        .catch(() => { /* transient — keep showing the last good plans */ });
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const signals = fetched ?? storeSignals;
  const live = useMemo(
    () => signals.filter((s) => (s.status === "new" || s.status === "approved") && s.trade_plan),
    [signals],
  );

  return (
    <GlassCard>
      <SectionTitle right={<span className="text-[11px] text-faint">{live.length} live</span>}>
        Trade Plan
      </SectionTitle>
      <div className="mb-2 text-[11px] text-faint">
        Exactly what auto-trade would place — entry time, expiry and stake, from the same code the
        executor runs. Use these numbers to mirror the bot manually on Quotex.
      </div>
      {live.length === 0 ? (
        <div className="py-6 text-center text-[12px] text-faint">
          No live signal right now — a plan appears here the moment one is finalized.
        </div>
      ) : (
        <div className="space-y-1.5">
          {live.map((sig) => (
            <PlanCard
            key={sig.id}
            sig={sig}
            plan={sig.trade_plan as TradePlan}
            now={now}
            onDone={() => setFetched(null)}
          />
          ))}
        </div>
      )}
    </GlassCard>
  );
}
