/**
 * Indicator math for the market chart.
 *
 * Pure functions over the candles already displayed, so an indicator can never
 * disagree with the chart it is drawn on. Calculated from the SELECTED
 * timeframe's candles only -- a 15m EMA is computed from 15m candles, never
 * from 1m data resampled in a way that could drift from what is on screen.
 *
 * Nothing here generates signals, scores confidence or influences trading. It
 * draws lines.
 */

import type { ChartCandle } from "./candleMath";

/** A point on an indicator line, aligned to a candle timestamp. */
export interface IndicatorPoint {
  time: number;
  value: number;
}

export interface BollingerPoint {
  time: number;
  upper: number;
  middle: number;
  lower: number;
}

/**
 * Exponential moving average over closes.
 *
 * Seeded with the simple average of the first `period` values, which is the
 * standard construction and makes the result deterministic: the same candles
 * always produce the same line, regardless of how many times it is recomputed.
 *
 * Returns fewer points than inputs until the period is satisfied -- the chart
 * simply starts the line later rather than drawing a misleading early value.
 */
export function ema(values: number[], period: number): number[] {
  if (period <= 0 || values.length < period) return [];

  const k = 2 / (period + 1);
  const out: number[] = new Array(values.length).fill(NaN);

  let seed = 0;
  for (let i = 0; i < period; i++) seed += values[i];
  let prev = seed / period;
  out[period - 1] = prev;

  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

/** Close prices in series order. */
export function closes(candles: ChartCandle[]): number[] {
  return candles.map((c) => c.close);
}

/**
 * EMA as chart points, dropping the leading NaN warm-up region.
 *
 * `time` stays in seconds, matching the candle timestamps the chart uses.
 */
export function emaSeries(
  candles: ChartCandle[],
  period: number,
): IndicatorPoint[] {
  const values = ema(closes(candles), period);
  const out: IndicatorPoint[] = [];
  for (let i = 0; i < values.length; i++) {
    const value = values[i];
    if (Number.isFinite(value)) {
      out.push({ time: candles[i].timestamp, value });
    }
  }
  return out;
}

/**
 * Bollinger Bands: a simple moving average with bands at +/- `mult` standard
 * deviations. Default period 20, multiplier 2.
 *
 * Uses the population standard deviation, which is the convention for these
 * bands and keeps the result reproducible.
 */
export function bollinger(
  candles: ChartCandle[],
  period = 20,
  mult = 2,
): BollingerPoint[] {
  const values = closes(candles);
  if (period <= 0 || mult <= 0 || values.length < period) return [];

  const out: BollingerPoint[] = [];

  // Two passes over each window rather than running sums. The one-pass form
  // (sumSq/n - mean^2) is O(n) but suffers catastrophic cancellation: for a
  // flat series of 1.1 both terms are ~1.21, and subtracting them leaves the
  // rounding error instead of zero, which showed up as a ~6e-8 band width on a
  // series with no variance at all. Cost here is period * n -- about 10k
  // operations for 500 candles at period 20, which is nothing next to drawing
  // the chart, and it is exact for the cases that matter.
  for (let i = period - 1; i < values.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += values[j];
    const mean = sum / period;

    let sqDev = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const d = values[j] - mean;
      sqDev += d * d;
    }
    const sd = Math.sqrt(sqDev / period);

    out.push({
      time: candles[i].timestamp,
      upper: mean + mult * sd,
      middle: mean,
      lower: mean - mult * sd,
    });
  }
  return out;
}

/** The indicator keys the UI can toggle. */
export type IndicatorKey = "ema9" | "ema21" | "ema50" | "bollinger";

export const INDICATOR_PERIODS: Record<Exclude<IndicatorKey, "bollinger">, number> = {
  ema9: 9,
  ema21: 21,
  ema50: 50,
};

export const BOLLINGER_PERIOD = 20;
export const BOLLINGER_MULT = 2;

export interface IndicatorToggles {
  ema9: boolean;
  ema21: boolean;
  ema50: boolean;
  bollinger: boolean;
}

export const DEFAULT_TOGGLES: IndicatorToggles = {
  ema9: true,
  ema21: true,
  ema50: true,
  bollinger: true,
};
