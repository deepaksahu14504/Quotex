/**
 * Time formatting for the chart's axis and crosshair.
 *
 * Every timestamp in this app is an epoch value in SECONDS -- the backend
 * divides the broker's milliseconds on the way in (market.py:366/372/400/410)
 * and the chart never multiplies it back. Keeping the unit consistent in one
 * place is what stops the classic mixed-unit bug where a chart plots 1970
 * because something was treated as milliseconds when it was seconds.
 *
 * The axis labels are timeframe-aware for the same reason a stock chart shows
 * months and an intraday chart shows minutes: a 1D chart labelling every bar
 * with "14:35" is noise, and a 1m chart labelling bars with "12 Oct" tells the
 * user nothing about when the bar formed.
 */

import type { UTCTimestamp } from "lightweight-charts";

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/** Format seconds as a fixed-width HH:MM in UTC. */
export function hmLabel(seconds: number): string {
  const d = new Date(seconds * 1000);
  const h = String(d.getUTCHours()).padStart(2, "0");
  const m = String(d.getUTCMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

/** Format seconds as "12 Oct" in UTC. */
export function dayLabel(seconds: number): string {
  const d = new Date(seconds * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d.getUTCDate()} ${months[d.getUTCMonth()]}`;
}

/** Full label for the crosshair readout: "12 Oct 2025 14:35 UTC". */
export function timeLabel(seconds: number): string {
  const d = new Date(seconds * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const date = `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  if (seconds % DAY === 0) return `${date} UTC`;
  return `${date} ${hmLabel(seconds)} UTC`;
}

/**
 * Build a tick-mark formatter for a bar size.
 *
 * Returns a function lightweight-charts calls per axis tick. The label chosen
 * depends on how much time a bar covers and whether the tick starts a new
 * day, so an intraday chart shows the time but still marks the day boundary.
 */
export function chartTickFormatter(
  barSeconds: number,
): (time: UTCTimestamp, tickMarkType: number) => string {
  return (time: UTCTimestamp, tickMarkType: number) => {
    const seconds = Number(time);
    if (!Number.isFinite(seconds) || seconds <= 0) return "";

    // tickMarkType: 0=Year, 1=Month, 2=DayOfMonth, 3=Time, 4=TimeWithSeconds
    // Daily and longer bars label by date; anything shorter labels by clock
    // time, falling back to the date at the start of a day so the user can
    // still see where one session ends and the next begins.
    if (barSeconds >= DAY || tickMarkType <= 2) return dayLabel(seconds);

    if (seconds % DAY === 0) return dayLabel(seconds);
    if (tickMarkType >= 3) return hmLabel(seconds);
    return hmLabel(seconds);
  };
}

/**
 * The bucket start for a timestamp, in seconds.
 *
 * Mirrors the backend's epoch alignment (ts // period * period) so a client
 * that needs to group anything locally lands on the same boundary the server
 * would. Not currently used for aggregation -- the server aggregates -- but
 * keeping the two definitions identical prevents a silent off-by-one-bar if
 * that ever changes.
 */
export function bucketStart(seconds: number, barSeconds: number): number {
  if (barSeconds <= 0) return seconds;
  return Math.floor(seconds / barSeconds) * barSeconds;
}

/** Human label for the toolbar: the API value "1day" reads better as "1D". */
export function timeframeLabel(value: string): string {
  const labels: Record<string, string> = {
    "1m": "1m", "2m": "2m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "4h": "4h", "1day": "1D",
  };
  return labels[value] ?? value;
}

/** Rough description of the bar size, for the axis and status text. */
export function timeframeUnit(barSeconds: number): "minute" | "hour" | "day" {
  if (barSeconds >= DAY) return "day";
  if (barSeconds >= HOUR) return "hour";
  return "minute";
}

export const TIME_UNITS = { MINUTE, HOUR, DAY };
