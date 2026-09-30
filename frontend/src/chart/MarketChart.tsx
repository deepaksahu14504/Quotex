/**
 * The market chart.
 *
 * Draws candles from the existing market pipeline onto lightweight-charts. It
 * is visualisation only: it never generates a signal, scores confidence, reads
 * risk settings, sizes a stake or places an order. Nothing in here can change
 * what the trading engine does.
 *
 * Two rules shape the implementation:
 *
 * 1. The chart instance lives in a ref, never in React state. Creating a chart
 *    is expensive and destroys the user's zoom; re-creating it per tick would
 *    be both slow and unusable. History is applied with setData() once per
 *    asset/timeframe change, and live frames go through series.update(), which
 *    mutates the existing series in place.
 *
 * 2. Live frames never travel through React state. They arrive several times a
 *    second and are pushed straight into the series. Only the small status
 *    readout is stateful, and it is cheap to re-render.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  CandlestickSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineStyle,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Trade } from "../types";
import { indicatorColor, getChartTheme } from "./chartTheme.ts";
import {
  bollinger,
  emaSeries,
  INDICATOR_PERIODS,
  BOLLINGER_PERIOD,
  BOLLINGER_MULT,
  type IndicatorToggles,
} from "./indicators.ts";
import type { ChartCandle } from "./candleMath.ts";
import { precisionFor, timeframeSeconds } from "./chartFormat.ts";
import { bucketStart, chartTickFormatter } from "./timeFormat.ts";

interface Props {
  asset: string;
  timeframe: string;
  candles: ChartCandle[];
  /** Bumped by the data hook whenever a fresh history load completes. */
  loadToken: number;
  toggles: IndicatorToggles;
  /** Executed trades. Markers come from these, not from signals -- a marker
   *  belongs on the bar the order actually filled on, at the price it actually
   *  filled at, which a signal's own timestamp and price are not. */
  trades: Trade[];
  /** Subscribe to live frames arriving over the websocket. */
  subscribeLive: (handler: (candle: ChartCandle) => void) => () => void;
  onHover: (info: HoverInfo | null) => void;
}

export interface HoverInfo {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  change: number;
  ema9?: number;
  ema21?: number;
  ema50?: number;
  boll?: { upper: number; middle: number; lower: number };
  /** The trade that entered on this bar, when there is one. */
  trade?: Trade;
}

/** How much chart history to keep visible on first paint, in bars. */
const VISIBLE_BARS = 90;

export default function MarketChart({
  asset,
  timeframe,
  candles,
  loadToken,
  toggles,
  trades,
  subscribeLive,
  onHover,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const emaRefs = useRef<Record<"ema9" | "ema21" | "ema50", ISeriesApi<"Line"> | null>>({
    ema9: null, ema21: null, ema50: null,
  });
  const bollRefs = useRef<{
    upper: ISeriesApi<"Line"> | null;
    middle: ISeriesApi<"Line"> | null;
    lower: ISeriesApi<"Line"> | null;
  }>({ upper: null, middle: null, lower: null });
  const priceLineRef = useRef<IPriceLine | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // Latest values for the crosshair readout, keyed by timestamp.
  const indicatorIndex = useRef(new Map<number, HoverInfo>());
  /** Trades by the bucket they entered on, so the crosshair can show the full
   *  entry/result detail for the bar under it. */
  const tradeIndex = useRef(new Map<number, Trade>());
  const candlesRef = useRef<ChartCandle[]>(candles);
  candlesRef.current = candles;

  const theme = useMemo(() => getChartTheme(), []);
  const barSeconds = timeframeSeconds(timeframe);

  // ── create the chart once ────────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: theme.inkDim,
        fontFamily:
          '"Inter", system-ui, -apple-system, "Segoe UI", sans-serif',
        fontSize: 10,
      },
      grid: {
        vertLines: { color: theme.hair },
        horzLines: { color: theme.hair },
      },
      crosshair: {
        vertLine: { color: theme.inkFaint, width: 1, style: 2, labelBackgroundColor: theme.accent2 },
        horzLine: { color: theme.inkFaint, width: 1, style: 2, labelBackgroundColor: theme.accent2 },
      },
      rightPriceScale: {
        borderColor: theme.hair,
        scaleMargins: { top: 0.08, bottom: 0.06 },
      },
      timeScale: {
        borderColor: theme.hair,
        rightOffset: 4,
        barSpacing: 7,
        timeVisible: barSeconds < 86400,
        secondsVisible: false,
        tickMarkFormatter: chartTickFormatter(barSeconds),
      },
      localization: { locale: "en-US" },
      handleScale: { axisPressedMouseMove: { time: true, price: false } },
    });

    const price = chart.addSeries(CandlestickSeries, {
      upColor: theme.up,
      downColor: theme.down,
      borderUpColor: theme.up,
      borderDownColor: theme.down,
      wickUpColor: theme.up,
      wickDownColor: theme.down,
      priceLineColor: theme.accent,
      lastValueVisible: true,
      priceLineVisible: true,
    });

    chartRef.current = chart;
    priceRef.current = price;
    markersRef.current = createSeriesMarkers(price, []);

    // Indicators are line series created once and fed incrementally, so
    // toggling them on and off never rebuilds the chart.
    (Object.keys(INDICATOR_PERIODS) as Array<"ema9" | "ema21" | "ema50">).forEach((key) => {
      emaRefs.current[key] = chart.addSeries(LineSeries, {
        color: indicatorColor(key),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    });
    bollRefs.current.upper = chart.addSeries(LineSeries, {
      color: indicatorColor("boll"), lineWidth: 1, lineStyle: 2,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    bollRefs.current.middle = chart.addSeries(LineSeries, {
      color: indicatorColor("boll"), lineWidth: 1, lineStyle: 1,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    bollRefs.current.lower = chart.addSeries(LineSeries, {
      color: indicatorColor("boll"), lineWidth: 1, lineStyle: 2,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });

    // Crosshair readout. Writes to a callback, not to state, so moving the
    // mouse over the chart does not re-render the dashboard.
    chart.subscribeCrosshairMove((param) => {
      if (!param || !param.time || !param.point) {
        onHover(null);
        return;
      }
      const ts = Number(param.time);
      const row = candlesRef.current.find((c) => c.timestamp === ts);
      if (!row) {
        onHover(null);
        return;
      }
      const ind = indicatorIndex.current.get(ts);
      onHover({
        time: row.timestamp,
        open: row.open,
        high: row.high,
        low: row.low,
        close: row.close,
        change: row.close - row.open,
        ema9: ind?.ema9,
        ema21: ind?.ema21,
        ema50: ind?.ema50,
        boll: ind?.boll,
        trade: tradeIndex.current.get(ts),
      });
    });

    return () => {
      // Full teardown: the chart owns a canvas, a resize observer and its
      // event listeners, none of which React knows about.
      try {
        chart.remove();
      } catch {
        /* already removed */
      }
      chartRef.current = null;
      priceRef.current = null;
      indicatorIndex.current = new Map();
      tradeIndex.current = new Map();
      emaRefs.current = { ema9: null, ema21: null, ema50: null };
      bollRefs.current = { upper: null, middle: null, lower: null };
      priceLineRef.current = null;
      markersRef.current = null;
    };
    // Created once per mount. Asset/timeframe changes flow through the data
    // effect below instead of rebuilding the chart.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  /**
   * Compute and draw the visible indicators from the SELECTED timeframe's
   * candles. Recomputing from the same array the price series was fed is what
   * keeps a line aligned with the bars it is drawn over.
   */
  const drawIndicators = useCallback(
    (rows: ChartCandle[]) => {
      const index = new Map<number, HoverInfo>();

      for (const key of Object.keys(INDICATOR_PERIODS) as Array<"ema9" | "ema21" | "ema50">) {
        const series = emaRefs.current[key];
        if (!series) continue;
        if (!toggles[key]) {
          series.setData([]);
          continue;
        }
        const points = emaSeries(rows, INDICATOR_PERIODS[key]);
        series.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
        for (const p of points) {
          const existing = index.get(p.time);
          if (existing) existing[key] = p.value;
          else index.set(p.time, emptyHover(p.time, { [key]: p.value }));
        }
      }

      if (toggles.bollinger) {
        const bands = bollinger(rows, BOLLINGER_PERIOD, BOLLINGER_MULT);
        bollRefs.current.upper?.setData(
          bands.map((b) => ({ time: b.time as UTCTimestamp, value: b.upper })),
        );
        bollRefs.current.middle?.setData(
          bands.map((b) => ({ time: b.time as UTCTimestamp, value: b.middle })),
        );
        bollRefs.current.lower?.setData(
          bands.map((b) => ({ time: b.time as UTCTimestamp, value: b.lower })),
        );
        for (const b of bands) {
          const existing = index.get(b.time);
          const value = { upper: b.upper, middle: b.middle, lower: b.lower };
          if (existing) existing.boll = value;
          else index.set(b.time, emptyHover(b.time, { boll: value }));
        }
      } else {
        bollRefs.current.upper?.setData([]);
        bollRefs.current.middle?.setData([]);
        bollRefs.current.lower?.setData([]);
      }

      indicatorIndex.current = index;
    },
    [toggles],
  );

  /** Current-price line, so the last traded price is readable at a glance. */
  const updatePriceLine = useCallback(
    (value: number | null) => {
      const price = priceRef.current;
      if (!price) return;
      if (value === null || !Number.isFinite(value)) {
        if (priceLineRef.current) {
          try {
            price.removePriceLine(priceLineRef.current);
          } catch {
            /* already gone */
          }
          priceLineRef.current = null;
        }
        return;
      }
      if (priceLineRef.current) {
        priceLineRef.current.applyOptions({ price: value });
        return;
      }
      priceLineRef.current = price.createPriceLine({
        price: value,
        color: theme.accent,
        lineWidth: 1,
        lineStyle: 1 as LineStyle,
        axisLabelVisible: true,
        title: "",
      });
    },
    [theme],
  );

  /**
   * Mark the bars where trades actually executed.
   *
   * Sourced from trades, not signals. A signal's timestamp is when it was
   * generated and its price is the price at generation; an order can fill
   * seconds later at a different price, and the requirement is explicit that
   * the marker belongs on the execution. So this reads the trade record.
   *
   * The bar comes from `entry_candle_timestamp`, resolved on the server by the
   * same bucket function the chart uses. Only when that is absent -- a trade
   * recorded before the field existed, or one whose bar could not be resolved
   * -- does this fall back to bucketing `created_at`, which is still the
   * execution timestamp, just aligned locally.
   */
  const drawTradeMarkers = useCallback(
    (rows: Trade[], tf: string, barSec: number) => {
      const api = markersRef.current;
      if (!api) return;

      const windowStart = candlesRef.current[0]?.timestamp ?? 0;
      const windowEnd = candlesRef.current[candlesRef.current.length - 1]?.timestamp ?? 0;
      if (windowStart === 0) {
        tradeIndex.current = new Map();
        api.setMarkers([]);
        return;
      }

      const index = new Map<number, Trade>();
      const markers: SeriesMarker<Time>[] = [];

      for (const trade of rows) {
        if (trade.asset !== asset) continue;
        // A trade with no timeframe recorded is not filtered out: the asset
        // match is enough, and dropping it would silently hide a real trade.
        if (trade.timeframe && trade.timeframe !== tf) continue;

        const raw =
          typeof trade.entry_candle_timestamp === "number" &&
          Number.isFinite(trade.entry_candle_timestamp)
            ? trade.entry_candle_timestamp
            : Number.isFinite(trade.created_at)
              ? bucketStart(trade.created_at, barSec)
              : null;
        if (raw === null) continue;

        const bucket = Number(raw);
        if (bucket < windowStart || bucket > windowEnd) continue;

        // Direction is lowercase "call" | "put" (types.ts:1).
        const isCall = trade.direction === "call";
        // The broker's own result. "win" is IN THE MONEY and "loss" is OUT OF
        // IT; nothing here recomputes an outcome the broker already reported.
        const settled = trade.status === "win" || trade.status === "loss";
        const color =
          trade.status === "win" ? theme.up
          : trade.status === "loss" ? theme.down
          : trade.status === "draw" ? theme.gold
          : theme.accent;

        const label = trade.direction.toUpperCase();
        const pnl =
          settled && Number.isFinite(trade.profit)
            ? ` ${trade.profit >= 0 ? "+" : "\u2212"}$${Math.abs(trade.profit).toFixed(2)}`
            : trade.status === "draw" ? " DRAW"
            : "";

        index.set(bucket, trade);
        markers.push({
          time: bucket as UTCTimestamp,
          position: isCall ? "belowBar" : "aboveBar",
          color,
          shape: isCall ? "arrowUp" : "arrowDown",
          text: `${label}${pnl}`,
        });
      }

      tradeIndex.current = index;

      // Newest last: lightweight-charts requires ordered markers and silently
      // drops the rest if they are not.
      markers.sort((a, b) => Number(a.time) - Number(b.time));
      api.setMarkers(markers);
    },
    [asset, theme],
  );


  // ── apply history, and recompute indicators with it ──────────────────────
  useEffect(() => {
    const price = priceRef.current;
    const chart = chartRef.current;
    if (!price || !chart) return;

    const bars = candles.map((c) => ({
      time: c.timestamp as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    price.setData(bars);

    // Precision follows the instrument, so the scale and the crosshair label
    // match how the asset is actually quoted.
    const last = candles[candles.length - 1];
    if (last) {
      const precision = precisionFor(last.close);
      const minMove = 1 / Math.pow(10, precision);
      price.applyOptions({ priceFormat: { type: "price", precision, minMove } });
    }

    drawIndicators(candles);
    updatePriceLine(last?.close ?? null);

    // Keep the same window width across asset/timeframe changes rather than
    // snapping to a different zoom every time.
    const total = bars.length;
    if (total > 0) {
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(0, total - VISIBLE_BARS),
        to: total + 4,
      });
    }
    // loadToken forces this to re-run when a history reload completes, even
    // though the asset and timeframe did not change.
  }, [candles, loadToken, theme, drawIndicators, updatePriceLine]);

  // ── live frames: update in place, never rebuild ──────────────────────────
  useEffect(() => {
    return subscribeLive((candle) => {
      const price = priceRef.current;
      if (!price) return;

      try {
        price.update({
          time: candle.timestamp as UTCTimestamp,
          open: candle.open,
          high: candle.high,
          low: candle.low,
          close: candle.close,
        });
      } catch {
        // update() throws if the bar is older than the series' first bar or
        // out of order. Drop the frame rather than tearing down the chart.
        return;
      }

      updatePriceLine(candle.close);

      // Recompute only the indicators that are visible. A full recompute of
      // 300 candles is a few thousand operations, but doing it for four
      // hidden lines on every tick is waste for nothing.
      drawIndicators(candlesRef.current);
      drawTradeMarkers(trades, timeframe, barSeconds);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscribeLive]);

  // ── indicator visibility toggles ─────────────────────────────────────────
  useEffect(() => {
    (Object.keys(INDICATOR_PERIODS) as Array<"ema9" | "ema21" | "ema50">).forEach((key) => {
      emaRefs.current[key]?.applyOptions({ visible: toggles[key] });
    });
    const bollVisible = toggles.bollinger;
    bollRefs.current.upper?.applyOptions({ visible: bollVisible });
    bollRefs.current.middle?.applyOptions({ visible: bollVisible });
    bollRefs.current.lower?.applyOptions({ visible: bollVisible });
    drawIndicators(candles);
  }, [toggles, candles, drawIndicators]);

  // ── trade markers, from the trades the engine actually executed ─────────
  useEffect(() => {
    drawTradeMarkers(trades, timeframe, barSeconds);
  }, [trades, timeframe, candles, barSeconds, drawTradeMarkers]);

  // ── helpers ──────────────────────────────────────────────────────────────


  return <div ref={containerRef} className="h-full w-full" />;
}

function emptyHover(time: number, extra: Partial<HoverInfo>): HoverInfo {
  return { time, open: 0, high: 0, low: 0, close: 0, change: 0, ...extra };
}
