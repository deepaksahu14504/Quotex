// Shared number formatting for market-data display.
//
// The backend sends raw floats (see orchestrator._indicator_snapshot);
// how many decimals are meaningful depends on the indicator's scale.
// A flat toFixed(2) renders small-magnitude values as "0.00" -- e.g.
// BRLUSD ATR ~0.0002 -- hiding the real pipeline state. These helpers
// keep at least 3 significant digits visible at every magnitude.

const OSCILLATORS = new Set(["rsi", "stoch_rsi", "adx", "trend_strength"]);

/** Strip trailing zeros after the decimal point without ever producing exponent notation. */
function trimZeros(s: string): string {
  if (s.includes(".")) s = s.replace(/0+$/, "").replace(/\.$/, "");
  return s;
}

/**
 * Format one indicator value for the Live Pipeline card.
 * - Oscillators (rsi/stoch_rsi/adx/trend_strength, 0-100 scale): 1 decimal.
 * - volume (tick-count proxy or exchange volume): integer with thousands separators.
 * - Everything price-derived (ema/macd/atr/bb/psar/supertrend/vwap/support/resistance):
 *   decimals adapt to magnitude so small numbers (BRLUSD ATR) never collapse to "0.00".
 */
export function formatIndicatorValue(key: string, v: number): string {
  if (!Number.isFinite(v)) return "—";
  if (OSCILLATORS.has(key)) return v.toFixed(1);
  if (key === "volume") {
    if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString("en-US");
    return Number.isInteger(v) ? String(v) : trimZeros(v.toFixed(2));
  }
  const abs = Math.abs(v);
  if (abs === 0) return "0";
  if (abs >= 1000) return v.toFixed(2);
  if (abs >= 100) return v.toFixed(3);
  if (abs >= 1) return v.toFixed(4);
  if (abs >= 0.01) return v.toFixed(5);
  // Below 0.01: keep 3 significant digits, capped at 8 decimals.
  const decimals = Math.min(8, Math.max(5, 3 - Math.floor(Math.log10(abs)) - 1));
  return trimZeros(v.toFixed(decimals));
}
