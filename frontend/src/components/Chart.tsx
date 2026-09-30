/**
 * Chart entry point.
 *
 * Keeps the same named export and prop signature the dashboard already used, so
 * App.tsx:159 needs no change. What changed underneath is the whole data path:
 * this used to fetch the full series every 2.5 seconds and rebuild the series
 * each time (and it swallowed every failure into an empty canvas). It now loads
 * history once per asset/timeframe change and receives live frames over the
 * websocket the dashboard already has open.
 *
 * This component owns selection state only -- which asset, which timeframe,
 * which indicators are visible. All drawing is in MarketChart and all data
 * handling is in useChartCandles.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { AssetInfo } from "../types";
import { useStore } from "../store";
import MarketChart, { type HoverInfo } from "../chart/MarketChart";
import ChartToolbar from "../chart/ChartToolbar";
import ChartStatus from "../chart/ChartStatus";
import { useChartCandles } from "../chart/useChartCandles";
import { DEFAULT_TOGGLES, type IndicatorToggles } from "../chart/indicators";

/**
 * Fallback timeframe list, used only until /api/chart/meta answers.
 *
 * It matches what the backend offers (chart_service.CHART_TIMEFRAMES); the
 * server response replaces it, so the UI cannot drift from what the engine
 * actually supports.
 */
const FALLBACK_TIMEFRAMES = [
  "1m", "2m", "3m", "5m", "15m", "30m", "1h", "4h", "1day",
];

export function Chart({ timeframe }: { timeframe: string }) {
  const wsConnected = useStore((s) => s.wsConnected);
  // Trades, not signals: a marker belongs on the bar the order actually
  // filled on, at the price it actually filled at.
  const trades = useStore((s) => s.trades);
  const settings = useStore((s) => s.settings);

  const [assets, setAssets] = useState<AssetInfo[]>([]);
  const [timeframes, setTimeframes] = useState<string[]>(FALLBACK_TIMEFRAMES);
  const [asset, setAsset] = useState<string>("");
  const [toggles, setToggles] = useState<IndicatorToggles>(DEFAULT_TOGGLES);
  const [hover, setHover] = useState<HoverInfo | null>(null);

  // The chart starts on the engine's own timeframe and only diverges when the
  // user picks something else. It never writes back -- changing the chart's
  // timeframe must not change what the engine trades.
  const [chosenTimeframe, setChosenTimeframe] = useState<string | null>(null);
  const activeTimeframe = chosenTimeframe ?? timeframe ?? "1m";

  // ── the asset and timeframe lists come from the server ───────────────────
  useEffect(() => {
    let cancelled = false;
    api
      .chartMeta()
      .then((meta) => {
        if (cancelled) return;
        setAssets(meta.assets);
        if (meta.timeframes?.length) setTimeframes(meta.timeframes);
        setAsset((current) => {
          if (current && meta.assets.some((a) => a.symbol === current)) return current;
          return meta.assets[0]?.symbol ?? "";
        });
      })
      .catch(() => {
        // Without the list there is nothing to plot. ChartStatus reports the
        // failure; the rest of the dashboard keeps working.
        if (!cancelled) setAssets([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // If the engine's timeframe is not one the chart offers, fall back to the
  // closest available rather than requesting something the server rejects.
  useEffect(() => {
    if (!timeframes.includes(activeTimeframe)) {
      setChosenTimeframe("1m");
    }
  }, [activeTimeframe, timeframes]);

  const { state, getCandles, subscribeLive, reload } = useChartCandles(
    asset,
    activeTimeframe,
    wsConnected,
  );

  const candles = useMemo(() => getCandles(), [getCandles, state.loadToken]);

  const onToggle = useCallback((key: keyof IndicatorToggles) => {
    setToggles((t) => ({ ...t, [key]: !t[key] }));
  }, []);

  // Auto-trade status is display only. Read from the engine's settings; this
  // component has no setter for it and cannot start or stop trading.
  const autoTradeOn = settings?.trading.mode === "auto";

  if (!asset) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <ChartStatus
          phase={assets.length === 0 ? "error" : "loading"}
          error={
            assets.length === 0
              ? "The asset list could not be loaded from the server."
              : null
          }
          connected={wsConnected}
          rejected={0}
          hover={null}
          lastUpdateAt={null}
          latest={null}
        />
      </div>
    );
  }

  return (
    <div className="relative flex h-full min-h-0 w-full flex-col">
      <ChartToolbar
        assets={assets}
        asset={asset}
        timeframes={timeframes}
        timeframe={activeTimeframe}
        toggles={toggles}
        onAsset={setAsset}
        onTimeframe={setChosenTimeframe}
        onToggle={onToggle}
        onReload={reload}
        autoTradeOn={autoTradeOn}
      />

      <div className="relative min-h-0 flex-1">
        {/* The canvas. Kept mounted across asset/timeframe changes so the
            chart is never re-created per tick. */}
        <MarketChart
          asset={asset}
          timeframe={activeTimeframe}
          candles={candles}
          loadToken={state.loadToken}
          toggles={toggles}
          trades={trades}
          subscribeLive={subscribeLive}
          onHover={setHover}
        />

        <div className="pointer-events-none absolute inset-x-0 top-0">
          <ChartStatus
            phase={state.phase}
            error={state.error}
            connected={state.connected}
            rejected={state.rejected}
            hover={hover}
            lastUpdateAt={state.lastUpdateAt}
            latest={state.latest}
          />
        </div>
      </div>
    </div>
  );
}

export default Chart;
