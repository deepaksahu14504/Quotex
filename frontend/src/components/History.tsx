import { useMemo, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { STRATEGY_LABELS, type Trade } from "../types";
import { fmtPrice, precisionFor } from "../chart/chartFormat.ts";
import { timeLabel } from "../chart/timeFormat.ts";
import { GlassCard, Pill, SectionTitle } from "./ui";

type Filter = "all" | "win" | "loss";

export function History() {
  const trades = useStore((s) => s.trades);
  const refreshTrades = useStore((s) => s.refreshTrades);
  const pushToast = useStore((s) => s.pushToast);
  const [filter, setFilter] = useState<Filter>("all");
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  /** Trades whose entry-candle detail is open. */
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const filtered = useMemo(
    () => trades.filter((t) => filter === "all" || t.status === filter),
    [trades, filter]
  );

  const toggleSel = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const deleteOne = async (id: string) => {
    await api.deleteTrade(id);
    await refreshTrades();
  };

  const deleteSelected = async () => {
    if (selected.size === 0) return;
    if (!confirm(`Delete ${selected.size} selected trade(s)? This cannot be undone.`)) return;
    await api.deleteSelectedTrades([...selected]);
    setSelected(new Set());
    setSelecting(false);
    await refreshTrades();
    pushToast("info", "Selected trades deleted");
  };

  const clearAll = async () => {
    if (!confirm("Delete ALL trade history? This cannot be undone.")) return;
    await api.deleteAllTrades();
    setSelected(new Set());
    setSelecting(false);
    await refreshTrades();
    pushToast("info", "All trade history cleared");
  };

  return (
    <GlassCard className="flex flex-col">
      <SectionTitle
        right={
          <div className="flex items-center gap-2">
            <div className="flex gap-1 rounded-xl bg-black/20 p-1">
              {(["all", "win", "loss"] as Filter[]).map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`rounded-lg px-2.5 py-1 text-[11px] font-semibold capitalize transition ${
                    filter === f ? "bg-white/10 text-white" : "text-faint"
                  }`}
                >
                  {f}
                </button>
              ))}
            </div>
            <button
              onClick={() => { setSelecting((v) => !v); setSelected(new Set()); }}
              className={`rounded-xl px-2.5 py-1.5 text-[11px] font-semibold ring-1 transition ${
                selecting ? "bg-[var(--color-accent)]/15 text-[var(--color-accent)] ring-[var(--color-accent)]/30" : "bg-white/5 text-dim ring-white/8"
              }`}
            >
              {selecting ? "Cancel" : "Select"}
            </button>
          </div>
        }
      >
        Trade History
      </SectionTitle>

      {/* Action bar */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-[11px] text-faint">{filtered.length} trades</span>
        <div className="flex gap-2">
          {selecting && (
            <button
              onClick={deleteSelected}
              disabled={selected.size === 0}
              className="rounded-lg bg-[var(--color-down)]/15 px-3 py-1.5 text-[11px] font-semibold text-[var(--color-down)] ring-1 ring-[var(--color-down)]/30 transition disabled:opacity-40"
            >
              Delete selected ({selected.size})
            </button>
          )}
          <button
            onClick={clearAll}
            disabled={trades.length === 0}
            className="rounded-lg bg-white/5 px-3 py-1.5 text-[11px] font-semibold text-dim ring-1 ring-white/8 transition hover:text-[var(--color-down)] disabled:opacity-40"
          >
            Clear all
          </button>
        </div>
      </div>

      <div className="max-h-[64vh] space-y-1.5 overflow-y-auto pr-1">
        {filtered.length === 0 && <div className="py-14 text-center text-dim">No trades to show.</div>}
        {filtered.map((t) => {
          const tone = t.status === "win" ? "up" : t.status === "loss" ? "down" : "neutral";
          const win = t.profit > 0;
          const isSel = selected.has(t.id);
          const prec = precisionFor(t.entry_candle_close ?? t.open_price ?? 0);
          return (
            <div key={t.id}>
            <div
              onClick={() => selecting && toggleSel(t.id)}
              className={`tile flex items-center justify-between px-3 py-2.5 transition ${
                selecting ? "cursor-pointer" : ""
              } ${isSel ? "ring-1 ring-[var(--color-accent)]/50" : ""}`}
            >
              <div className="flex items-center gap-3">
                {selecting && (
                  <span className={`grid h-5 w-5 place-items-center rounded-md border text-[10px] ${isSel ? "border-[var(--color-accent)] bg-[var(--color-accent)] text-black" : "border-white/20"}`}>
                    {isSel ? "✓" : ""}
                  </span>
                )}
                <span
                  className="grid h-9 w-9 place-items-center rounded-xl text-xs font-bold"
                  style={{
                    color: t.direction === "call" ? "var(--color-up)" : "var(--color-down)",
                    backgroundColor: t.direction === "call" ? "rgba(43,220,160,0.12)" : "rgba(255,90,118,0.12)",
                  }}
                >
                  {t.direction === "call" ? "▲" : "▼"}
                </span>
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[13px] font-semibold">{t.asset.replace("_otc", "")}</span>
                    {t.timeframe && <span className="nums rounded bg-white/6 px-1.5 py-0.5 text-[9.5px] text-faint">{t.timeframe}</span>}
                  </div>
                  <div className="nums text-[10.5px] text-faint">
                    ${t.amount} · {t.duration}s{t.confidence ? ` · ${t.confidence}%` : ""}
                  </div>
                  {t.strategies && t.strategies.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {t.strategies.slice(0, 3).map((s) => (
                        <span key={s} className="rounded-md bg-[var(--color-accent)]/10 px-1.5 py-0.5 text-[9.5px] font-medium text-[var(--color-accent)]/90">
                          {STRATEGY_LABELS[s] ?? s}
                        </span>
                      ))}
                      {t.strategies.length > 3 && (
                        <span className="rounded-md bg-white/6 px-1.5 py-0.5 text-[9.5px] text-faint">+{t.strategies.length - 3}</span>
                      )}
                    </div>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Pill tone={tone as any}>{t.status.toUpperCase()}</Pill>
                <div className={`nums w-16 text-right text-[13px] font-semibold ${win ? "text-up" : t.profit < 0 ? "text-down" : "text-dim"}`}>
                  {t.profit >= 0 ? "+" : "-"}${Math.abs(t.profit).toFixed(2)}
                </div>
                {!selecting && (
                  <button
                    onClick={(e) => { e.stopPropagation(); deleteOne(t.id); }}
                    className="grid h-7 w-7 place-items-center rounded-lg text-faint transition hover:bg-[var(--color-down)]/15 hover:text-[var(--color-down)]"
                    aria-label="Delete trade"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M19 6l-1 14H6L5 6" /></svg>
                  </button>
                )}
              </div>
            </div>

            <button
              onClick={() => toggleExpanded(t.id)}
              className="nums mt-0.5 flex w-full items-center gap-1.5 rounded-lg px-3 py-1 text-left text-[10px] transition hover:bg-white/4"
              style={{ color: "var(--color-ink-faint)" }}
              aria-expanded={expanded.has(t.id)}
            >
              <span style={{ transform: expanded.has(t.id) ? "rotate(90deg)" : "none", display: "inline-block" }}>›</span>
              Entry candle
              {typeof t.entry_candle_close === "number" ? (
                <span className="nums" style={{ color: "var(--color-ink-dim)" }}>
                  O {fmtPrice(t.entry_candle_open, prec)} · H {fmtPrice(t.entry_candle_high, prec)} ·
                  L {fmtPrice(t.entry_candle_low, prec)} · C {fmtPrice(t.entry_candle_close, prec)}
                </span>
              ) : (
                <span>not recorded</span>
              )}
            </button>

            {expanded.has(t.id) && <TradeEntryDetail t={t} />}
            </div>
          );
        })}
      </div>
    </GlassCard>
  );
}


/**
 * Full entry/exit record for one trade.
 *
 * Every value here is the one already stored on the trade: the entry price and
 * timestamp the execution recorded, the entry candle's OHLC captured at that
 * moment from the same source the chart reads, and the broker's own result and
 * P/L. Nothing is derived here, so this cannot disagree with the chart marker
 * or with the broker.
 */
function TradeEntryDetail({ t }: { t: Trade }) {
  const prec = precisionFor(t.entry_candle_close ?? t.open_price ?? 0);
  const settled = t.status === "win" || t.status === "loss";

  const resultLabel =
    t.status === "win" ? "IN THE MONEY"
    : t.status === "loss" ? "OUT OF THE MONEY"
    : t.status === "draw" ? "DRAW"
    : t.status === "open" || t.status === "pending" ? "ACTIVE"
    : t.status.toUpperCase();
  const resultColor =
    t.status === "win" ? "var(--color-up)"
    : t.status === "loss" ? "var(--color-down)"
    : t.status === "draw" ? "var(--color-gold)"
    : "var(--color-accent)";

  const hasCandle = typeof t.entry_candle_open === "number";
  const expiry =
    typeof t.closed_at === "number" ? t.closed_at
    : typeof t.created_at === "number" && t.duration ? t.created_at + t.duration
    : null;

  const rows: Array<[string, React.ReactNode]> = [
    ["Asset", t.asset.replace("_otc", "") + (t.asset.endsWith("_otc") ? " · OTC" : "")],
    ["Direction", (t.direction === "call" ? "▲ CALL" : "▼ PUT")],
    ["Timeframe", t.timeframe ?? "—"],
    ["Entry time", timeLabel(t.created_at)],
    ["Entry price", fmtPrice(t.open_price, prec)],
  ];
  if (hasCandle) {
    rows.push(
      ["Entry candle time", timeLabel(t.entry_candle_timestamp as number)],
      ["Entry candle O", fmtPrice(t.entry_candle_open, prec)],
      ["Entry candle H", fmtPrice(t.entry_candle_high, prec)],
      ["Entry candle L", fmtPrice(t.entry_candle_low, prec)],
      ["Entry candle C", fmtPrice(t.entry_candle_close, prec)],
    );
  } else {
    rows.push(["Entry candle", "not recorded for this trade"]);
  }
  rows.push(["Expiry / exit", expiry !== null ? timeLabel(expiry) : "—"]);

  return (
    <div
      className="mb-1 rounded-lg border px-3 py-2 text-[10.5px]"
      style={{ borderColor: "var(--color-hair)", background: "var(--color-panel)" }}
    >
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 sm:grid-cols-3">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-2">
            <span style={{ color: "var(--color-ink-faint)" }}>{label}</span>
            <span className="nums" style={{ color: "var(--color-ink-dim)" }}>{value}</span>
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex items-center gap-3 border-t pt-1.5" style={{ borderColor: "var(--color-hair)" }}>
        <span className="font-semibold" style={{ color: resultColor }}>{resultLabel}</span>
        {settled && Number.isFinite(t.profit) && (
          <span
            className="nums font-semibold"
            style={{ color: t.profit >= 0 ? "var(--color-up)" : "var(--color-down)" }}
          >
            P/L {t.profit >= 0 ? "+" : "\u2212"}${Math.abs(t.profit).toFixed(2)}
          </span>
        )}
        {!settled && (
          <span className="nums" style={{ color: "var(--color-ink-faint)" }}>
            stake ${t.amount} · payout {t.payout?.toFixed(0)}%
          </span>
        )}
      </div>
    </div>
  );
}
