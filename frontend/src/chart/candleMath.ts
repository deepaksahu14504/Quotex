/**
 * Pure candle math for the market chart.
 *
 * Everything here is a pure function with no React, no chart library and no
 * network, so it can be unit tested directly. The chart component consumes
 * these; it does not re-implement them.
 */

export interface ChartCandle {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  /** null when the provider's volume is a tick-count proxy rather than real. */
  volume: number | null;
}

/** Raw shape as it arrives from /api/chart/candles or a candle_update frame. */
export type RawCandle = Partial<ChartCandle> & Record<string, unknown>;

/** Why a candle was rejected. Kept as a string so it can be logged directly. */
export type RejectReason =
  | "missing-timestamp"
  | "non-finite"
  | "non-positive"
  | "high-below-low"
  | "open-out-of-range"
  | "close-out-of-range";

export interface ValidationResult {
  candle: ChartCandle | null;
  reason: RejectReason | null;
}

const FINITE_FIELDS = ["timestamp", "open", "high", "low", "close"] as const;

/**
 * Validate one raw candle and coerce it to the chart shape.
 *
 * Returns a reason rather than throwing, because a corrupt frame from the wire
 * must be skipped and reported, never allowed to take the chart down.
 */
export function validateCandle(raw: RawCandle | null | undefined): ValidationResult {
  if (!raw || typeof raw !== "object") {
    return { candle: null, reason: "missing-timestamp" };
  }
  const ts = Number(raw.timestamp);
  if (!Number.isFinite(ts) || ts <= 0) {
    return { candle: null, reason: "missing-timestamp" };
  }

  const values: Record<string, number> = {};
  for (const field of FINITE_FIELDS) {
    const value = Number(raw[field]);
    if (!Number.isFinite(value)) {
      return { candle: null, reason: "non-finite" };
    }
    values[field] = value;
  }

  for (const field of ["open", "high", "low", "close"] as const) {
    if (values[field] <= 0) {
      return { candle: null, reason: "non-positive" };
    }
  }

  if (values.high < values.low) {
    return { candle: null, reason: "high-below-low" };
  }
  if (values.open < values.low || values.open > values.high) {
    return { candle: null, reason: "open-out-of-range" };
  }
  if (values.close < values.low || values.close > values.high) {
    return { candle: null, reason: "close-out-of-range" };
  }

  // Volume is genuinely optional; a null means "not real volume", which is a
  // valid state rather than an error.
  const rawVolume = raw.volume;
  const volume =
    rawVolume === null || rawVolume === undefined ? null : Number(rawVolume);

  return {
    candle: {
      timestamp: Math.floor(ts),
      open: values.open,
      high: values.high,
      low: values.low,
      close: values.close,
      volume: volume !== null && Number.isFinite(volume) ? volume : null,
    },
    reason: null,
  };
}

/**
 * Validate and order a batch, dropping anything unusable.
 *
 * Duplicate timestamps collapse to the last occurrence: a live update re-sends
 * the forming candle with a revised close, so the newest version is the one to
 * keep.
 */
export function normalizeCandles(raw: RawCandle[]): {
  candles: ChartCandle[];
  rejected: number;
} {
  const byTs = new Map<number, ChartCandle>();
  let rejected = 0;

  for (const item of raw) {
    const { candle, reason } = validateCandle(item);
    if (!candle || reason) {
      rejected++;
      continue;
    }
    byTs.set(candle.timestamp, candle);
  }

  const candles = [...byTs.values()].sort((a, b) => a.timestamp - b.timestamp);
  return { candles, rejected };
}

/**
 * Merge one live candle into an ordered series.
 *
 * Returns a NEW array only when something actually changed, so the caller can
 * skip work when the frame was a no-op. Appends when the candle is newer than
 * everything present, replaces in place when the timestamp already exists.
 */
export function mergeCandle(
  series: ChartCandle[],
  incoming: ChartCandle,
): ChartCandle[] | null {
  if (series.length === 0) return [incoming];

  const last = series[series.length - 1];

  if (incoming.timestamp > last.timestamp) {
    return [...series, incoming];
  }

  if (incoming.timestamp < series[0].timestamp) {
    // Older than the whole window: this is history arriving late, and the
    // chart is not showing that range. Ignore rather than prepend, which
    // would break the ordering the chart library expects.
    return null;
  }

  // Find the slot. Binary search, because the series is ordered and this runs
  // on every live frame.
  let lo = 0;
  let hi = series.length - 1;
  let index = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const ts = series[mid].timestamp;
    if (ts === incoming.timestamp) {
      index = mid;
      break;
    }
    if (ts < incoming.timestamp) lo = mid + 1;
    else hi = mid - 1;
  }
  if (index === -1) return null;

  const existing = series[index];
  if (
    existing.open === incoming.open &&
    existing.high === incoming.high &&
    existing.low === incoming.low &&
    existing.close === incoming.close &&
    existing.volume === incoming.volume
  ) {
    return null; // unchanged: no work, no re-render
  }

  const next = series.slice();
  next[index] = incoming;
  return next;
}

/** Drop candles older than the newest `limit`, keeping the most recent. */
export function trimSeries(series: ChartCandle[], limit: number): ChartCandle[] {
  if (limit <= 0 || series.length <= limit) return series;
  return series.slice(series.length - limit);
}

/** True when two candles are identical in every field the chart draws. */
export function sameCandle(a: ChartCandle, b: ChartCandle): boolean {
  return (
    a.timestamp === b.timestamp &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.volume === b.volume
  );
}
