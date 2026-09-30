/**
 * Tests for the chart's pure modules: candle validation/merging, indicators,
 * and time formatting.
 *
 * Runs on Node's own test runner with type stripping -- `npm run test:chart`.
 * No test framework is added to the project for this, because these are pure
 * functions and the alternative would be a new devDependency to assert that
 * 1 + 1 === 2.
 *
 * They matter because these functions decide what the chart draws. A validator
 * that lets a NaN through throws inside lightweight-charts and unmounts the
 * component; an EMA that is not deterministic redraws a different line each
 * time it is computed from the same candles.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  validateCandle,
  normalizeCandles,
  mergeCandle,
  trimSeries,
  sameCandle,
} from "../src/chart/candleMath.ts";
import {
  ema,
  emaSeries,
  bollinger,
  closes,
  INDICATOR_PERIODS,
  BOLLINGER_PERIOD,
  BOLLINGER_MULT,
} from "../src/chart/indicators.ts";
import {
  bucketStart,
  chartTickFormatter,
  hmLabel,
  dayLabel,
  timeLabel,
  timeframeLabel,
} from "../src/chart/timeFormat.ts";
import { precisionFor, timeframeSeconds, fmtPrice } from "../src/chart/chartFormat.ts";
import { publishCandleUpdate, onCandleUpdate } from "../src/chart/candleBus.ts";

const BASE = 1759201200; // 15m-aligned epoch second

/** A candle whose high/low are derived from open/close, so the fixture is
 *  always valid and only the behaviour under test can fail. */
const C = (ts, cl = 1.144, o = 1.143, h = null, l = null) => ({
  timestamp: ts,
  open: o,
  high: h ?? Math.max(o, cl) + 0.0002,
  low: l ?? Math.min(o, cl) - 0.0002,
  close: cl,
  volume: null,
});

// ── validation ─────────────────────────────────────────────────────────────

describe("validateCandle", () => {
  it("accepts a well-formed candle", () => {
    const r = validateCandle(C(BASE));
    assert.equal(r.reason, null);
    assert.equal(r.candle.timestamp, BASE);
  });

  it("rejects every kind of bad data, with a reason", () => {
    const cases = [
      [{}, "missing-timestamp"],
      [null, "missing-timestamp"],
      [{ timestamp: 0, open: 1, high: 1, low: 1, close: 1 }, "missing-timestamp"],
      [{ timestamp: BASE, open: NaN, high: 1, low: 1, close: 1 }, "non-finite"],
      [{ timestamp: BASE, open: 1, high: Infinity, low: 1, close: 1 }, "non-finite"],
      [{ timestamp: BASE, open: 0, high: 1.1, low: 0.9, close: 1 }, "non-positive"],
      [{ timestamp: BASE, open: 1, high: 0.5, low: 0.9, close: 0.7 }, "high-below-low"],
      [{ timestamp: BASE, open: 2, high: 1.1, low: 0.9, close: 1 }, "open-out-of-range"],
      [{ timestamp: BASE, open: 1, high: 1.1, low: 0.9, close: 2 }, "close-out-of-range"],
    ];
    for (const [raw, expected] of cases) {
      const r = validateCandle(raw);
      assert.equal(r.reason, expected, `expected ${expected} for ${JSON.stringify(raw)}`);
      assert.equal(r.candle, null);
    }
  });

  it("keeps a null volume null, and a real number a number", () => {
    assert.equal(validateCandle({ ...C(BASE), volume: null }).candle.volume, null);
    assert.equal(validateCandle({ ...C(BASE), volume: 7 }).candle.volume, 7);
    assert.equal(validateCandle({ ...C(BASE), volume: "bad" }).candle.volume, null);
  });
});

describe("normalizeCandles", () => {
  it("sorts by timestamp and counts rejects", () => {
    const r = normalizeCandles([C(BASE + 60), C(BASE), { garbage: true }]);
    assert.deepEqual(r.candles.map((c) => c.timestamp), [BASE, BASE + 60]);
    assert.equal(r.rejected, 1);
  });

  it("collapses a duplicate timestamp, last one wins", () => {
    const r = normalizeCandles([C(BASE, 1.144), C(BASE, 1.146)]);
    assert.equal(r.candles.length, 1);
    assert.equal(r.candles[0].close, 1.146);
  });
});

// ── merging live frames ────────────────────────────────────────────────────

describe("mergeCandle", () => {
  it("appends a newer candle", () => {
    const next = mergeCandle([C(BASE)], C(BASE + 60));
    assert.equal(next.length, 2);
  });

  it("replaces an existing candle in place", () => {
    const series = [C(BASE), C(BASE + 60, 1.145)];
    const next = mergeCandle(series, C(BASE + 60, 1.147));
    assert.equal(next.length, 2);
    assert.equal(next[1].close, 1.147);
  });

  it("returns null when nothing changed, so the chart can skip work", () => {
    const series = [C(BASE), C(BASE + 60, 1.147)];
    assert.equal(mergeCandle(series, C(BASE + 60, 1.147)), null);
  });

  it("refuses late history older than the window", () => {
    assert.equal(mergeCandle([C(BASE)], C(BASE - 1000)), null);
  });

  it("refuses a candle that belongs in a gap", () => {
    const series = [C(BASE), C(BASE + 120)];
    assert.equal(mergeCandle(series, C(BASE + 60)), null);
  });
});

describe("trimSeries", () => {
  it("keeps the newest candles", () => {
    const many = Array.from({ length: 50 }, (_, i) => C(BASE + i * 60));
    const t = trimSeries(many, 5);
    assert.equal(t.length, 5);
    assert.equal(t[4].timestamp, BASE + 49 * 60);
  });

  it("returns the same reference when nothing needs trimming", () => {
    const many = Array.from({ length: 5 }, (_, i) => C(BASE + i * 60));
    assert.equal(trimSeries(many, 100), many);
  });
});

it("sameCandle compares every drawn field", () => {
  assert.equal(sameCandle(C(BASE), C(BASE)), true);
  assert.equal(sameCandle(C(BASE), C(BASE, 1.145)), false);
});

// ── indicators ─────────────────────────────────────────────────────────────

describe("ema", () => {
  it("is deterministic for the same input", () => {
    const v = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
    assert.deepEqual(ema(v, 3), ema(v, 3));
  });

  it("seeds with the simple average and leaves the warm-up as NaN", () => {
    const e = ema([1, 2, 3, 4, 5], 3);
    assert.ok(Number.isNaN(e[0]) && Number.isNaN(e[1]));
    assert.ok(Math.abs(e[2] - 2) < 1e-9, "seed is the SMA of the first 3");
  });

  it("returns nothing when there are fewer points than the period", () => {
    assert.deepEqual(ema([1, 2], 5), []);
  });

  it("is flat for a constant series", () => {
    for (const v of ema([5, 5, 5, 5, 5], 3)) {
      if (!Number.isNaN(v)) assert.ok(Math.abs(v - 5) < 1e-9);
    }
  });
});

describe("emaSeries", () => {
  it("drops the warm-up and keeps timestamps in seconds", () => {
    const candles = Array.from({ length: 12 }, (_, i) => C(BASE + i * 60, 1.1 + i * 0.001));
    const pts = emaSeries(candles, 9);
    assert.equal(pts.length, 4, "12 - 9 + 1");
    assert.equal(pts[0].time, BASE + 8 * 60);
    assert.ok(pts[0].time > 1e9 && pts[0].time < 2e9, "seconds, not milliseconds");
  });
});

describe("bollinger", () => {
  it("uses period 20 and multiplier 2 by default", () => {
    assert.equal(BOLLINGER_PERIOD, 20);
    assert.equal(BOLLINGER_MULT, 2);
  });

  it("has exactly zero width on a flat series", () => {
    // Regression: the one-pass variance form suffered catastrophic
    // cancellation here and returned a ~6e-8 width instead of 0.
    const flat = Array.from({ length: 25 }, (_, i) => C(BASE + i * 60, 1.1));
    const bands = bollinger(flat);
    assert.equal(bands.length, 6, "25 - 20 + 1");
    assert.equal(bands[0].upper - bands[0].lower, 0);
    assert.ok(Math.abs(bands[0].middle - 1.1) < 1e-9);
  });

  it("keeps upper above middle above lower, with the middle equal to the SMA", () => {
    const rising = Array.from({ length: 30 }, (_, i) => C(BASE + i * 60, 1.1 + i * 0.001));
    const bands = bollinger(rising);
    const last = bands[bands.length - 1];
    assert.ok(last.upper > last.middle && last.middle > last.lower);
    const sma = closes(rising).slice(-20).reduce((a, b) => a + b, 0) / 20;
    assert.ok(Math.abs(last.middle - sma) < 1e-9);
  });

  it("returns nothing below the period", () => {
    const short = Array.from({ length: 19 }, (_, i) => C(BASE + i * 60));
    assert.deepEqual(bollinger(short), []);
  });
});

it("exposes the three EMA periods the UI offers", () => {
  assert.deepEqual(INDICATOR_PERIODS, { ema9: 9, ema21: 21, ema50: 50 });
});

// ── time handling ──────────────────────────────────────────────────────────

describe("timestamps stay in seconds everywhere", () => {
  it("maps each timeframe to its period in seconds", () => {
    assert.equal(timeframeSeconds("1m"), 60);
    assert.equal(timeframeSeconds("15m"), 900);
    assert.equal(timeframeSeconds("1h"), 3600);
    assert.equal(timeframeSeconds("1day"), 86400);
    assert.equal(timeframeSeconds("nonsense"), 60, "falls back rather than NaN");
  });

  it("aligns buckets on epoch boundaries, matching the backend", () => {
    assert.equal(bucketStart(BASE + 7, 900), BASE);
    assert.equal(bucketStart(BASE + 899, 900), BASE);
    assert.equal(bucketStart(BASE + 900, 900), BASE + 900);
  });

  it("labels the daily timeframe as 1D for the toolbar", () => {
    assert.equal(timeframeLabel("1day"), "1D");
    assert.equal(timeframeLabel("15m"), "15m");
    assert.equal(timeframeLabel("odd"), "odd");
  });

  it("formats clock time and dates in UTC", () => {
    // BASE == 1759201200 == 2025-09-30 03:00:00 UTC
    assert.equal(hmLabel(BASE), "03:00");
    assert.equal(dayLabel(BASE), "30 Sep");
    assert.match(timeLabel(BASE), /30 Sep 2025 03:00 UTC/);
  });

  it("labels a day-aligned timestamp with the date only", () => {
    const midnight = BASE - (BASE % 86400); // 00:00 UTC
    assert.equal(hmLabel(midnight), "00:00");
    // timeLabel keeps the year and drops the clock; dayLabel is the short
    // axis form, so they are deliberately not the same string.
    assert.match(timeLabel(midnight), /^\d{1,2} Sep 2025 UTC$/);
    assert.ok(!/\d{2}:\d{2}/.test(timeLabel(midnight)), "no clock on a day-aligned bar");
  });

  it("formats axis ticks by timeframe", () => {
    const intraday = chartTickFormatter(900);
    assert.equal(intraday(BASE, 3), "03:00");
    assert.equal(intraday(BASE, 2), "30 Sep", "day ticks label the date");
    const daily = chartTickFormatter(86400);
    assert.equal(daily(BASE, 3), "30 Sep", "daily bars label the date, not the clock");
    assert.equal(intraday(0, 3), "", "an invalid time formats to nothing");
  });
});

// ── price formatting ───────────────────────────────────────────────────────

describe("precisionFor", () => {
  it("follows the instrument's price magnitude", () => {
    assert.equal(precisionFor(1.14), 5, "FX pair");
    assert.equal(precisionFor(64250), 1, "crypto pair");
    assert.equal(precisionFor(0.42), 6);
    assert.equal(precisionFor(0), 2, "no crash on zero");
    assert.equal(precisionFor(NaN), 2, "no crash on NaN");
  });

  it("renders a missing value as a dash rather than NaN", () => {
    assert.equal(fmtPrice(undefined, 5), "—");
    assert.equal(fmtPrice(NaN, 5), "—");
    assert.equal(fmtPrice(1.14567, 5), "1.14567");
  });
});

// ── the live frame bus ─────────────────────────────────────────────────────

describe("publishCandleUpdate", () => {
  it("forwards a valid frame to subscribers", () => {
    const seen = [];
    const off = onCandleUpdate((f) => seen.push(f));
    const ok = publishCandleUpdate({ asset: "EURUSD_otc", timeframe: "15m", candle: C(BASE) });
    off();
    assert.equal(ok, true);
    assert.equal(seen.length, 1);
    assert.equal(seen[0].asset, "EURUSD_otc");
  });

  it("refuses a frame that is missing its routing info", () => {
    const seen = [];
    const off = onCandleUpdate((f) => seen.push(f));
    assert.equal(publishCandleUpdate({ timeframe: "15m", candle: C(BASE) }), false);
    assert.equal(publishCandleUpdate({ asset: "X", candle: C(BASE) }), false);
    off();
    assert.equal(seen.length, 0);
  });

  it("never forwards a candle the chart library would throw on", () => {
    const seen = [];
    const off = onCandleUpdate((f) => seen.push(f));
    const bad = publishCandleUpdate({
      asset: "X",
      timeframe: "15m",
      candle: { timestamp: BASE, open: NaN, high: 1, low: 1, close: 1 },
    });
    off();
    assert.equal(bad, false);
    assert.equal(seen.length, 0);
  });

  it("stops delivering once unsubscribed", () => {
    const seen = [];
    const off = onCandleUpdate((f) => seen.push(f));
    publishCandleUpdate({ asset: "X", timeframe: "15m", candle: C(BASE) });
    off();
    publishCandleUpdate({ asset: "X", timeframe: "15m", candle: C(BASE + 60) });
    assert.equal(seen.length, 1);
  });
});
