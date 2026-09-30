/**
 * The chart's toolbar: asset picker, timeframe picker, indicator toggles.
 *
 * The asset list comes from the server, not a hardcoded array, so the chart can
 * never offer an asset the engine does not know about. Payout and the OTC flag
 * are read straight off that same list.
 */

import { memo, useState } from "react";
import type { AssetInfo } from "../types";
import type { IndicatorToggles } from "./indicators";
import { timeframeLabel } from "./timeFormat";

interface Props {
  assets: AssetInfo[];
  asset: string;
  timeframes: string[];
  timeframe: string;
  toggles: IndicatorToggles;
  onAsset: (a: string) => void;
  onTimeframe: (t: string) => void;
  onToggle: (key: keyof IndicatorToggles) => void;
  onReload: () => void;
  autoTradeOn: boolean;
}

const INDICATOR_LABELS: Array<{ key: keyof IndicatorToggles; label: string; swatch: string }> = [
  { key: "ema9", label: "EMA 9", swatch: "var(--color-accent)" },
  { key: "ema21", label: "EMA 21", swatch: "var(--color-gold)" },
  { key: "ema50", label: "EMA 50", swatch: "var(--color-accent-2)" },
  { key: "bollinger", label: "BB 20·2σ", swatch: "var(--color-ink-dim)" },
];

function ChartToolbarInner({
  assets,
  asset,
  timeframes,
  timeframe,
  toggles,
  onAsset,
  onTimeframe,
  onToggle,
  onReload,
  autoTradeOn,
}: Props) {
  const [moreOpen, setMoreOpen] = useState(false);

  const current = assets.find((a) => a.symbol === asset);
  const payout = current?.payout;

  return (
    <div className="flex flex-wrap items-center gap-1.5 border-b px-2 py-1.5"
         style={{ borderColor: "var(--color-hair)" }}>
      {/* Asset */}
      <select
        value={asset}
        onChange={(e) => onAsset(e.target.value)}
        className="nums max-w-[9rem] rounded-md border bg-transparent px-1.5 py-1 text-[11px] outline-none"
        style={{ borderColor: "var(--color-hair)", color: "var(--color-ink)" }}
        aria-label="Chart asset"
      >
        {assets.map((a) => (
          <option key={a.symbol} value={a.symbol} style={{ background: "var(--color-bg-2)" }}>
            {a.name || a.symbol}
            {a.is_otc ? " · OTC" : ""}
          </option>
        ))}
      </select>

      {/* Payout, from the same asset list the engine uses */}
      {typeof payout === "number" && (
        <span className="nums text-[10px]" style={{ color: "var(--color-ink-faint)" }}>
          {payout.toFixed(0)}%
        </span>
      )}
      {current && !current.is_open && (
        <span className="rounded px-1 text-[9px] uppercase tracking-wide"
              style={{ background: "var(--color-panel-2)", color: "var(--color-ink-faint)" }}>
          closed
        </span>
      )}

      {/* Timeframes */}
      <div className="flex items-center gap-0.5 rounded-md border p-0.5"
           style={{ borderColor: "var(--color-hair)" }}>
        {timeframes.map((tf) => {
          const active = tf === timeframe;
          return (
            <button
              key={tf}
              onClick={() => onTimeframe(tf)}
              className="nums rounded px-1.5 py-0.5 text-[10px] transition-colors"
              style={{
                background: active ? "var(--color-panel-2)" : "transparent",
                color: active ? "var(--color-accent)" : "var(--color-ink-faint)",
              }}
              aria-pressed={active}
              title={`${timeframeLabel(tf)} candles`}
            >
              {timeframeLabel(tf)}
            </button>
          );
        })}
      </div>

      <div className="flex-1" />

      {/* Auto-trade status. Display only -- this component cannot start or
          stop trading, and has no access to anything that could. */}
      <span
        className="flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px]"
        style={{
          borderColor: "var(--color-hair)",
          color: autoTradeOn ? "var(--color-up)" : "var(--color-ink-faint)",
        }}
        title="Read-only. Reflects the engine's mode; the chart cannot change it."
      >
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ background: autoTradeOn ? "var(--color-up)" : "var(--color-ink-faint)" }}
        />
        {autoTradeOn ? "AUTO ON" : "AUTO OFF"}
      </span>

      {/* Indicator toggles */}
      <div className="relative">
        <button
          onClick={() => setMoreOpen((v) => !v)}
          className="rounded-md border px-1.5 py-0.5 text-[10px]"
          style={{ borderColor: "var(--color-hair)", color: "var(--color-ink-dim)" }}
          aria-expanded={moreOpen}
          aria-label="Indicator visibility"
        >
          Indicators
        </button>
        {moreOpen && (
          <div
            className="absolute right-0 top-full z-20 mt-1 w-40 rounded-lg border p-1 shadow-lg"
            style={{
              borderColor: "var(--color-hair)",
              background: "var(--color-bg-2)",
            }}
          >
            {INDICATOR_LABELS.map(({ key, label, swatch }) => (
              <label
                key={key}
                className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-[11px]"
                style={{ color: "var(--color-ink-dim)" }}
              >
                <input
                  type="checkbox"
                  checked={toggles[key]}
                  onChange={() => onToggle(key)}
                  className="accent-current"
                />
                <span className="inline-block h-2 w-2 rounded-sm" style={{ background: swatch }} />
                {label}
              </label>
            ))}
          </div>
        )}
      </div>

      <button
        onClick={onReload}
        className="rounded-md border px-1.5 py-0.5 text-[10px]"
        style={{ borderColor: "var(--color-hair)", color: "var(--color-ink-dim)" }}
        title="Reload history and reconnect"
        aria-label="Reload chart"
      >
        ⟳
      </button>
    </div>
  );
}

export const ChartToolbar = memo(ChartToolbarInner);
export default ChartToolbar;
