/**
 * The chart's status strip and crosshair readout.
 *
 * Deliberately compact: it sits over the chart in the dashboard's 6-column
 * slot, which is `h-[46vh]` on small screens. A tall status bar would eat the
 * drawing area, and the brief asks for a loading state that does not block the
 * rest of the dashboard.
 */

import { memo } from "react";
import type { HoverInfo } from "./MarketChart";
import { fmtPrice, precisionFor } from "./chartFormat";
import { timeLabel } from "./timeFormat";
import type { ChartPhase } from "./useChartCandles";

interface Props {
  phase: ChartPhase;
  error: string | null;
  connected: boolean;
  rejected: number;
  hover: HoverInfo | null;
  lastUpdateAt: number | null;
}

/** Dot + text, coloured from the theme tokens. */
function Chip({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span className="flex items-center gap-1">
      <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {children}
    </span>
  );
}

function ChartStatusInner({ phase, error, connected, rejected, hover, lastUpdateAt }: Props) {
  // ── hard failure: say so, do not render an empty canvas
  // An empty chart is indistinguishable from "this market has not traded",
  // which is exactly how a real outage hides from the user.
  if (phase === "error") {
    return (
      <div
        className="pointer-events-auto flex h-full w-full flex-col items-center justify-center gap-2 px-4 text-center"
        role="alert"
      >
        <div className="text-[12px]" style={{ color: "var(--color-down)" }}>
          Chart unavailable
        </div>
        <div className="max-w-xs text-[10px] leading-relaxed" style={{ color: "var(--color-ink-faint)" }}>
          {error ?? "Market data could not be loaded."}
        </div>
      </div>
    );
  }

  const precision = hover ? precisionFor(hover.close) : 2;

  return (
    <div className="pointer-events-none flex flex-col gap-1 px-2 pt-1.5 text-[10px]">
      {/* Crosshair readout: OHLC plus the indicator values at that bar */}
      {hover ? (
        <div className="nums flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span style={{ color: "var(--color-ink-faint)" }}>{timeLabel(hover.time)}</span>
          <span style={{ color: "var(--color-ink-dim)" }}>
            O <span style={{ color: "var(--color-ink)" }}>{fmtPrice(hover.open, precision)}</span>
          </span>
          <span style={{ color: "var(--color-ink-dim)" }}>
            H <span style={{ color: "var(--color-ink)" }}>{fmtPrice(hover.high, precision)}</span>
          </span>
          <span style={{ color: "var(--color-ink-dim)" }}>
            L <span style={{ color: "var(--color-ink)" }}>{fmtPrice(hover.low, precision)}</span>
          </span>
          <span style={{ color: "var(--color-ink-dim)" }}>
            C{" "}
            <span style={{ color: hover.change >= 0 ? "var(--color-up)" : "var(--color-down)" }}>
              {fmtPrice(hover.close, precision)}
            </span>
          </span>
          {hover.ema9 !== undefined && (
            <span style={{ color: "var(--color-accent)" }}>E9 {fmtPrice(hover.ema9, precision)}</span>
          )}
          {hover.ema21 !== undefined && (
            <span style={{ color: "var(--color-gold)" }}>E21 {fmtPrice(hover.ema21, precision)}</span>
          )}
          {hover.ema50 !== undefined && (
            <span style={{ color: "var(--color-accent-2)" }}>E50 {fmtPrice(hover.ema50, precision)}</span>
          )}
          {hover.boll && (
            <span style={{ color: "var(--color-ink-faint)" }}>
              BB {fmtPrice(hover.boll.lower, precision)}/{fmtPrice(hover.boll.upper, precision)}
            </span>
          )}
        </div>
      ) : null}

      {/* Connection + data health, one compact line */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5" style={{ color: "var(--color-ink-faint)" }}>
        {phase === "loading" && (
          <Chip color="var(--color-accent)">loading…</Chip>
        )}
        {phase === "live" && connected && (
          <Chip color="var(--color-up)">live</Chip>
        )}
        {phase === "live" && !connected && (
          <Chip color="var(--color-gold)">reconnecting — will resync</Chip>
        )}
        {rejected > 0 && (
          <span title="Candles the server rejected as malformed">
            {rejected} bad candle{rejected === 1 ? "" : "s"} dropped
          </span>
        )}
        {lastUpdateAt !== null && (
          <span className="nums">
            last {new Date(lastUpdateAt * 1000).toISOString().slice(11, 19)}Z
          </span>
        )}
      </div>
    </div>
  );
}

export const ChartStatus = memo(ChartStatusInner);
export default ChartStatus;
