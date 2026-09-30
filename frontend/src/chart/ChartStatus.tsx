/**
 * The chart's status strip, OHLC readout and trade detail.
 *
 * Deliberately compact: it sits over the chart in the dashboard's 6-column
 * slot, which is `h-[46vh]` on small screens. A tall status bar would eat the
 * drawing area, and the brief asks for a loading state that does not block the
 * rest of the dashboard.
 *
 * The OHLC row is always present. With the crosshair on a bar it shows that
 * bar; otherwise it shows the newest one, which is what makes the header track
 * a forming candle in real time. Either way the numbers come from the candle
 * the chart is actually drawing -- nothing here is hardcoded or recomputed
 * from a second source.
 */

import { memo } from "react";
import type { HoverInfo } from "./MarketChart.ts";
import type { Trade } from "../types.ts";
import { fmtPrice, precisionFor } from "./chartFormat.ts";
import { timeLabel } from "./timeFormat.ts";
import type { ChartCandle } from "./candleMath.ts";
import type { ChartPhase } from "./useChartCandles.ts";

interface Props {
  phase: ChartPhase;
  error: string | null;
  connected: boolean;
  rejected: number;
  hover: HoverInfo | null;
  lastUpdateAt: number | null;
  /** Newest candle, used when the crosshair is not over a bar. */
  latest: ChartCandle | null;
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

function OHLC({
  o,
  h,
  l,
  c,
  precision,
}: {
  o: number;
  h: number;
  l: number;
  c: number;
  precision: number;
}) {
  return (
    <span className="nums flex flex-wrap items-center gap-x-2">
      <span style={{ color: "var(--color-ink-dim)" }}>
        O <span style={{ color: "var(--color-ink)" }}>{fmtPrice(o, precision)}</span>
      </span>
      <span style={{ color: "var(--color-ink-dim)" }}>
        H <span style={{ color: "var(--color-ink)" }}>{fmtPrice(h, precision)}</span>
      </span>
      <span style={{ color: "var(--color-ink-dim)" }}>
        L <span style={{ color: "var(--color-ink)" }}>{fmtPrice(l, precision)}</span>
      </span>
      <span style={{ color: "var(--color-ink-dim)" }}>
        C <span style={{ color: "var(--color-ink)" }}>{fmtPrice(c, precision)}</span>
      </span>
    </span>
  );
}

/**
 * The entry/result block for a trade on the bar under the crosshair.
 *
 * IN THE MONEY / OUT OF THE MONEY comes from the broker's own `status`, which
 * the settlement loop fills from the provider's check_result. It is never
 * inferred here from the candles -- if the broker says the trade lost, the
 * chart says it lost, even if the bars look like it should have won.
 */
function TradeDetail({ trade, precision }: { trade: Trade; precision: number }) {
  const isCall = trade.direction === "call";
  const settled = trade.status === "win" || trade.status === "loss";
  const resultLabel =
    trade.status === "win" ? "IN THE MONEY"
    : trade.status === "loss" ? "OUT OF THE MONEY"
    : trade.status === "draw" ? "DRAW"
    : trade.status === "open" || trade.status === "pending" ? "ACTIVE"
    : trade.status.toUpperCase();
  const resultColor =
    trade.status === "win" ? "var(--color-up)"
    : trade.status === "loss" ? "var(--color-down)"
    : trade.status === "draw" ? "var(--color-gold)"
    : "var(--color-accent)";

  const hasCandle =
    typeof trade.entry_candle_open === "number" &&
    typeof trade.entry_candle_high === "number" &&
    typeof trade.entry_candle_low === "number" &&
    typeof trade.entry_candle_close === "number";

  return (
    <div
      className="pointer-events-auto mt-1 w-fit max-w-[16rem] rounded-lg border px-2 py-1.5 text-[10px] leading-relaxed"
      style={{ borderColor: "var(--color-hair)", background: "var(--color-bg-2)" }}
    >
      <div className="flex items-center gap-2">
        <span className="font-semibold" style={{ color: isCall ? "var(--color-up)" : "var(--color-down)" }}>
          {isCall ? "▲ CALL" : "▼ PUT"}
        </span>
        <span className="nums" style={{ color: "var(--color-ink-dim)" }}>
          Entry {fmtPrice(trade.open_price, precision)}
        </span>
      </div>

      {trade.timeframe && (
        <div className="nums" style={{ color: "var(--color-ink-faint)" }}>
          Time {new Date(trade.created_at * 1000).toISOString().slice(11, 19)}Z · {trade.timeframe}
        </div>
      )}

      {hasCandle ? (
        <div className="mt-0.5" style={{ color: "var(--color-ink-faint)" }}>
          Entry candle
          <OHLC
            o={trade.entry_candle_open as number}
            h={trade.entry_candle_high as number}
            l={trade.entry_candle_low as number}
            c={trade.entry_candle_close as number}
            precision={precision}
          />
        </div>
      ) : (
        <div style={{ color: "var(--color-ink-faint)" }}>
          Entry candle not recorded for this trade.
        </div>
      )}

      <div className="mt-0.5 flex items-center gap-2">
        <span className="font-semibold" style={{ color: resultColor }}>{resultLabel}</span>
        {settled && Number.isFinite(trade.profit) && (
          <span
            className="nums font-semibold"
            style={{ color: trade.profit >= 0 ? "var(--color-up)" : "var(--color-down)" }}
          >
            {trade.profit >= 0 ? "+" : "\u2212"}${Math.abs(trade.profit).toFixed(2)}
          </span>
        )}
      </div>
    </div>
  );
}

function ChartStatusInner({
  phase,
  error,
  connected,
  rejected,
  hover,
  lastUpdateAt,
  latest,
}: Props) {
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

  // The crosshair wins when it is on a bar; otherwise the newest bar is shown,
  // so the header always carries a live OHLC reading.
  const row = hover
    ? { time: hover.time, o: hover.open, h: hover.high, l: hover.low, c: hover.close, change: hover.change, live: false }
    : latest
      ? {
          time: latest.timestamp,
          o: latest.open,
          h: latest.high,
          l: latest.low,
          c: latest.close,
          change: latest.close - latest.open,
          live: true,
        }
      : null;

  const precision = row ? precisionFor(row.c) : 2;

  return (
    <div className="pointer-events-none flex flex-col gap-1 px-2 pt-1.5 text-[10px]">
      {row && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span style={{ color: "var(--color-ink-faint)" }}>
            {timeLabel(row.time)}
            {row.live && <span title="Newest candle, updating live"> · live</span>}
          </span>
          <OHLC o={row.o} h={row.h} l={row.l} c={row.c} precision={precision} />
          <span
            className="nums"
            style={{ color: row.change >= 0 ? "var(--color-up)" : "var(--color-down)" }}
          >
            {row.change >= 0 ? "+" : "\u2212"}
            {row.o !== 0 ? `${Math.abs((row.change / row.o) * 100).toFixed(3)}%` : "0.000%"}
          </span>
          {hover?.ema9 !== undefined && (
            <span className="nums" style={{ color: "var(--color-accent)" }}>E9 {fmtPrice(hover.ema9, precision)}</span>
          )}
          {hover?.ema21 !== undefined && (
            <span className="nums" style={{ color: "var(--color-gold)" }}>E21 {fmtPrice(hover.ema21, precision)}</span>
          )}
          {hover?.ema50 !== undefined && (
            <span className="nums" style={{ color: "var(--color-accent-2)" }}>E50 {fmtPrice(hover.ema50, precision)}</span>
          )}
          {hover?.boll && (
            <span className="nums" style={{ color: "var(--color-ink-faint)" }}>
              BB {fmtPrice(hover.boll.lower, precision)}/{fmtPrice(hover.boll.upper, precision)}
            </span>
          )}
        </div>
      )}

      {/* Entry + result for a trade on the bar under the crosshair. Updates in
          place when the trade settles: the store replaces the trade object, so
          the same block re-renders with the broker's verdict. */}
      {hover?.trade && <TradeDetail trade={hover.trade} precision={precision} />}

      {/* Connection + data health, one compact line */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5" style={{ color: "var(--color-ink-faint)" }}>
        {phase === "loading" && <Chip color="var(--color-accent)">loading…</Chip>}
        {phase === "live" && connected && <Chip color="var(--color-up)">live</Chip>}
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
