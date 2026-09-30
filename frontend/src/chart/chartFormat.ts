/**
 * Small pure helpers shared by the chart components.
 *
 * Split out of MarketChart.tsx so that file exports only a component. Mixing
 * helper exports into a component file breaks React Fast Refresh in dev -- the
 * whole module has to be re-evaluated instead of hot-swapping the component --
 * which is what the lint warning was about.
 */

/**
 * Price decimals for an instrument, from its price magnitude.
 *
 * A fixed precision is wrong for most pairs: EUR/USD around 1.14 needs five
 * decimals to be readable, while a 60000-level crypto pair needs one. The chart
 * asks the series to format at this precision and steps by the matching minimum
 * move, so the right-hand scale matches how the asset is actually quoted.
 */
export function precisionFor(price: number): number {
  const abs = Math.abs(price);
  if (!Number.isFinite(abs) || abs <= 0) return 2;
  if (abs < 1) return 6;
  if (abs < 10) return 5;
  if (abs < 100) return 4;
  if (abs < 1000) return 3;
  if (abs < 10000) return 2;
  return abs < 100000 ? 1 : 0;
}

/** Seconds in one bar. Used for the time axis, markers and bucket alignment. */
export function timeframeSeconds(timeframe: string): number {
  const map: Record<string, number> = {
    "1m": 60, "2m": 120, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400, "1day": 86400,
  };
  return map[timeframe] ?? 60;
}

/** Format a price at a given precision, or an em dash when there is no value. */
export function fmtPrice(value: number | undefined | null, precision: number): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return "—";
  return value.toFixed(precision);
}
