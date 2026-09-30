/**
 * Loads chart history and keeps it live.
 *
 * One hook owns the whole data lifecycle so the chart component can stay about
 * drawing. History comes from /api/chart/candles on mount and whenever the
 * asset or timeframe changes; after that, frames arrive over the websocket that
 * the dashboard already has open.
 *
 * There is deliberately no polling interval here. The previous Chart.tsx
 * fetched every 2.5 seconds, which both lagged behind the market and wasted a
 * request per tab when nothing had changed. The server pushes a frame only when
 * a candle actually moved, so an idle chart costs nothing.
 *
 * The candles live in a ref, not in React state. A frame arrives several times
 * a second and only the canvas needs it; putting it in state would re-render
 * the component -- and therefore the chart -- on every tick.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { unwatchChart, watchChart } from "../store";
import { applyFrame, onCandleUpdate } from "./candleBus";
import { mergeCandle, normalizeCandles, type ChartCandle } from "./candleMath";

export type ChartPhase = "idle" | "loading" | "live" | "error";

export interface ChartDataState {
  phase: ChartPhase;
  /** Present only in the error phase; the server's own message. */
  error: string | null;
  /** Candles the server rejected as malformed during the last load. */
  rejected: number;
  /** True while the websocket carrying live frames is connected. */
  connected: boolean;
  /** Monotonic counter; changes when a fresh history load completes. */
  loadToken: number;
  lastUpdateAt: number | null;
}

const HISTORY_LIMIT = 300;

export interface UseChartCandles {
  state: ChartDataState;
  /** The current series. Read from a ref, so no re-render on update. */
  getCandles: () => ChartCandle[];
  /** Called by the chart when it has drawn a frame; used for the price line. */
  subscribeLive: (handler: (candle: ChartCandle) => void) => () => void;
  reload: () => void;
}

export function useChartCandles(
  asset: string,
  timeframe: string,
  connected: boolean,
): UseChartCandles {
  const [state, setState] = useState<ChartDataState>({
    phase: "loading",
    error: null,
    rejected: 0,
    connected,
    loadToken: 0,
    lastUpdateAt: null,
  });

  const candlesRef = useRef<ChartCandle[]>([]);
  const liveHandlers = useRef(new Set<(c: ChartCandle) => void>());
  const requestRef = useRef(0);

  // A state counter rather than a ref, on purpose. Mutating a ref does not
  // re-render, so an effect that listed a ref in its dependency array only
  // re-ran by accident -- whenever something else happened to trigger a render.
  // The reload button therefore worked sometimes and not others. A state value
  // is a real dependency, so reload always re-fetches.
  const [reloadToken, setReloadToken] = useState(0);

  // Keep `connected` mirrored into state without a second source of truth.
  useEffect(() => {
    setState((s) => (s.connected === connected ? s : { ...s, connected }));
  }, [connected]);

  const getCandles = useCallback(() => candlesRef.current, []);

  const subscribeLive = useCallback((handler: (c: ChartCandle) => void) => {
    liveHandlers.current.add(handler);
    return () => {
      liveHandlers.current.delete(handler);
    };
  }, []);

  const reload = useCallback(() => {
    setState((s) => ({ ...s, phase: "loading", error: null }));
    setReloadToken((t) => t + 1);
  }, []);

  // ── history load ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!asset || !timeframe) return;

    const myRequest = ++requestRef.current;
    let cancelled = false;

    setState((s) => ({ ...s, phase: "loading", error: null }));

    api
      .chartCandles(asset, timeframe, HISTORY_LIMIT)
      .then((res) => {
        // A newer request (asset or timeframe changed mid-flight) supersedes
        // this one. Applying a stale response is how a chart ends up showing
        // the previous asset's prices under the new label.
        if (cancelled || myRequest !== requestRef.current) return;

        // Validate again on the client. The server already did, but a frame
        // that reaches the chart library with a NaN throws and unmounts the
        // component, so this is a cheap second gate rather than paranoia.
        const { candles, rejected } = normalizeCandles(
          res.candles as unknown as Array<Record<string, unknown>>,
        );
        candlesRef.current = candles;

        setState((s) => ({
          ...s,
          phase: candles.length > 0 ? "live" : "error",
          error:
            candles.length > 0
              ? null
              : "No candles returned for this asset and timeframe.",
          rejected: rejected + (res.rejected ?? 0),
          loadToken: s.loadToken + 1,
          lastUpdateAt: candles.length ? candles[candles.length - 1].timestamp : null,
        }));
      })
      .catch((err: unknown) => {
        if (cancelled || myRequest !== requestRef.current) return;
        // Never leave the chart silently empty. An empty canvas is
        // indistinguishable from "no trades yet", which is how an outage
        // hides. Show the server's reason instead.
        const message =
          err instanceof Error && err.message ? err.message : "Market data unavailable";
        setState((s) => ({ ...s, phase: "error", error: message }));
      });

    return () => {
      cancelled = true;
    };
  }, [asset, timeframe, reloadToken]);

  // ── live subscription ────────────────────────────────────────────────────
  useEffect(() => {
    if (!asset || !timeframe) return;

    watchChart(asset, timeframe);

    const unsubscribe = onCandleUpdate((frame) => {
      const next = applyFrame(candlesRef.current, frame, asset, timeframe);
      if (!next) return; // unchanged, or for a different asset/timeframe

      candlesRef.current = next;
      const candle = next[next.length - 1];

      for (const handler of liveHandlers.current) {
        try {
          handler(candle);
        } catch {
          // A drawing error must not stop the data layer.
        }
      }

      // Throttled: state drives only the small status readout, not the canvas.
      setState((s) => ({ ...s, lastUpdateAt: candle.timestamp }));
    });

    return () => {
      unsubscribe();
      unwatchChart(asset, timeframe);
    };
  }, [asset, timeframe]);

  // ── reconnect resync ─────────────────────────────────────────────────────
  // The server drops subscriptions when a socket closes, and store.ts re-sends
  // them on reconnect. But any candle that closed *while* the socket was down
  // was never delivered, so the series would have a hole in it. A single
  // history reload on reconnect fills that gap.
  const wasConnected = useRef(connected);
  useEffect(() => {
    const before = wasConnected.current;
    wasConnected.current = connected;
    if (!before && connected && candlesRef.current.length > 0) {
      reload();
    }
  }, [connected, reload]);

  return { state, getCandles, subscribeLive, reload };
}

/**
 * Merge helper re-exported for the chart component, so there is exactly one
 * definition of how a candle enters a series.
 */
export { mergeCandle };
