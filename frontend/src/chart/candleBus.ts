/**
 * A tiny pub/sub for live candle frames.
 *
 * Why this exists instead of putting candles in the zustand store
 * ---------------------------------------------------------------
 * The store drives the whole dashboard. A candle frame arrives several times a
 * second, and if it lived in the store every consumer of `useStore` would
 * re-evaluate on each one -- the signal table, the trade log, the analytics
 * cards -- to redraw a chart that is the only thing that changed. This bus
 * keeps candle traffic out of React state entirely: the chart subscribes to it
 * directly and pushes into the chart series imperatively.
 *
 * The websocket handler in store.ts forwards `candle_update` here. It does not
 * touch the store, so a tick costs one function call and no render.
 */

import { mergeCandle, type ChartCandle, type RawCandle, validateCandle } from "./candleMath.ts";

export interface CandleFrame {
  asset: string;
  timeframe: string;
  candle: ChartCandle;
}

type Listener = (frame: CandleFrame) => void;

const listeners = new Set<Listener>();

/** Count of frames dropped as invalid, for the debug overlay. */
let rejectedCount = 0;

/**
 * Forward one live frame to the chart.
 *
 * Validates before publishing, mirroring the server: a malformed frame from
 * the wire must never reach the chart library, which would throw on NaN and
 * take the whole component down.
 */
export function publishCandleUpdate(raw: {
  asset?: unknown;
  timeframe?: unknown;
  candle?: unknown;
}): boolean {
  if (!raw || typeof raw !== "object") return false;
  const asset = typeof raw.asset === "string" ? raw.asset : null;
  const timeframe = typeof raw.timeframe === "string" ? raw.timeframe : null;
  if (!asset || !timeframe) return false;

  const { candle, reason } = validateCandle(raw.candle as RawCandle);
  if (!candle || reason) {
    rejectedCount++;
    return false;
  }

  for (const listener of listeners) {
    try {
      listener({ asset, timeframe, candle });
    } catch {
      // One failing listener must not stop the others, and must not throw back
      // into the websocket's onmessage handler.
    }
  }
  return true;
}

/** Subscribe to live frames. Returns an unsubscribe function. */
export function onCandleUpdate(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function rejectedFrames(): number {
  return rejectedCount;
}

export function resetRejectedFrames(): void {
  rejectedCount = 0;
}

/**
 * Apply a live frame to a series, returning null when nothing changed.
 *
 * Exported so the chart can apply a frame with the same merge rules the
 * history load used, keeping one definition of "how a candle enters a series".
 */
export function applyFrame(
  series: ChartCandle[],
  frame: CandleFrame,
  asset: string,
  timeframe: string,
): ChartCandle[] | null {
  if (frame.asset !== asset || frame.timeframe !== timeframe) return null;
  return mergeCandle(series, frame.candle);
}
