import { useEffect, useRef, useState } from "react";
import { createChart, ColorType, type IChartApi, type ISeriesApi, CandlestickSeries } from "lightweight-charts";
import { api } from "../api";
import type { AssetInfo } from "../types";
import { Pill } from "./ui";

export function Chart({ timeframe }: { timeframe: string }) {
  const wrap = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [assets, setAssets] = useState<AssetInfo[]>([]);
  const [asset, setAsset] = useState<string>("EURUSD_otc");
  const [last, setLast] = useState<number | null>(null);

  useEffect(() => {
    api.assets().then((a) => {
      setAssets(a);
      if (a.length && !a.find((x) => x.symbol === asset)) setAsset(a[0].symbol);
    });
  }, []);

  useEffect(() => {
    if (!wrap.current) return;
    const chart = createChart(wrap.current, {
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "rgba(255,255,255,0.55)", fontFamily: "Inter" },
      grid: { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.04)" } },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
      timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { labelBackgroundColor: "#7c5cff" }, horzLine: { labelBackgroundColor: "#7c5cff" } },
      autoSize: true,
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#22e0a1", downColor: "#ff5d73", borderVisible: false,
      wickUpColor: "#22e0a1", wickDownColor: "#ff5d73",
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => chart.remove();
  }, []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const candles = await api.candles(asset, timeframe, 150);
        if (!alive || !seriesRef.current) return;
        seriesRef.current.setData(
          candles.map((c) => ({ time: Math.floor(c.timestamp) as any, open: c.open, high: c.high, low: c.low, close: c.close }))
        );
        if (candles.length) setLast(candles[candles.length - 1].close);
      } catch {
        /* ignore */
      }
    };
    load();
    const id = setInterval(load, 2500);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [asset, timeframe]);

  return (
    <div className="flex h-full flex-col">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select
          value={asset}
          onChange={(e) => setAsset(e.target.value)}
          className="tile px-3 py-2 text-[13px] font-medium outline-none"
        >
          {assets.map((a) => (
            <option key={a.symbol} value={a.symbol}>
              {a.name} · {a.payout}%
            </option>
          ))}
        </select>
        <Pill tone="accent">{timeframe}</Pill>
        {last !== null && <Pill tone="neutral">{last.toFixed(5)}</Pill>}
      </div>
      <div ref={wrap} className="min-h-[260px] flex-1 rounded-2xl" />
    </div>
  );
}
