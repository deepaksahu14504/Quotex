import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { SettingsAdvice, SettingsRecommendation } from "../types";

const EFFECT_LABEL: Record<string, string> = {
  more_signals: "More signals",
  fewer_signals: "Keep as-is",
  safety: "Safety",
  reliability: "Reliability",
};

const EFFECT_COLOR: Record<string, string> = {
  more_signals: "var(--color-accent)",
  fewer_signals: "var(--color-ink-dim)",
  safety: "var(--color-down)",
  reliability: "var(--color-gold)",
};

const EVIDENCE_LABEL: Record<string, string> = {
  measured: "measured from your own resolved trades",
  observed: "observed from live telemetry",
  heuristic: "general guidance, not your data",
  needs_data: "not enough data yet",
};

function Card({ r, onApplied }: { r: SettingsRecommendation; onApplied: () => void }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const apply = async () => {
    if (!r.setting) return;
    setBusy(true);
    setMsg(null);
    try {
      await api.applyRecommendation(r.setting, r.suggested);
      setMsg("Applied");
      onApplied();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="tile flex flex-col gap-2 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[13px] font-semibold">{r.label}</div>
          <div className="text-[11px] text-faint">
            {EVIDENCE_LABEL[r.evidence] ?? r.evidence}
            {r.samples > 0 && ` · n=${r.samples}`}
          </div>
        </div>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide"
          style={{ color: EFFECT_COLOR[r.effect], background: `${EFFECT_COLOR[r.effect]}1f` }}
        >
          {EFFECT_LABEL[r.effect] ?? r.effect}
        </span>
      </div>

      <div className="text-[12px] text-dim">{r.why}</div>
      {r.risk && <div className="text-[11px] text-gold">Trade-off: {r.risk}</div>}

      {r.setting && (
        <div className="flex items-center justify-between gap-2 pt-1">
          <code className="truncate text-[11px] text-faint">
            {r.setting}: {String(r.current)} → {String(r.suggested)}
          </code>
          {r.applyable && (
            <button
              onClick={apply}
              disabled={busy}
              className="shrink-0 rounded-lg bg-white/10 px-3 py-1 text-[12px] font-semibold ring-1 ring-white/15 disabled:opacity-50"
            >
              {busy ? "Applying…" : "Apply"}
            </button>
          )}
        </div>
      )}
      {msg && <div className="text-[11px] text-faint">{msg}</div>}
    </div>
  );
}

export function Recommendations({ onApplied }: { onApplied?: () => void }) {
  const [advice, setAdvice] = useState<SettingsAdvice | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setAdvice(await api.settingsRecommendations());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load recommendations");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <div className="text-[12px] text-down">{error}</div>;
  if (!advice) return <div className="text-[12px] text-faint">Analysing your settings…</div>;

  return (
    <div className="flex flex-col gap-2">
      <div className="text-[12px] text-dim">{advice.summary}</div>
      <div className="text-[11px] text-faint">
        {advice.resolved_samples} resolved rejected signals · breakeven win rate at your average payout (
        {advice.typical_payout_pct}%) is {advice.breakeven_win_rate}%, not 50%.
      </div>
      {advice.recommendations.length === 0 && (
        <div className="text-[12px] text-faint">Nothing to change right now.</div>
      )}
      {advice.recommendations.map((r) => (
        <Card
          key={r.id}
          r={r}
          onApplied={() => {
            load();
            onApplied?.();
          }}
        />
      ))}
    </div>
  );
}
