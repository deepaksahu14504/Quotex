import { useMemo, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { STRATEGY_LABELS } from "../types";
import { GlassCard, Pill, SectionTitle } from "./ui";

type Filter = "all" | "win" | "loss";

export function History() {
  const trades = useStore((s) => s.trades);
  const refreshTrades = useStore((s) => s.refreshTrades);
  const pushToast = useStore((s) => s.pushToast);
  const [filter, setFilter] = useState<Filter>("all");
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

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
          return (
            <div
              key={t.id}
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
          );
        })}
      </div>
    </GlassCard>
  );
}
