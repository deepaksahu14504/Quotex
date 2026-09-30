# Task 13 — Entry-candle OHLC + trade markers on the existing chart

Scope: visibility only. Nothing here changes strategy, signal scoring, risk
gates, stake sizing, order placement, broker login, or settlement. Every field
added is metadata attached *after* the order is already placed.

## Inspection (all line numbers verified this turn at `185b51d`)

| Need | Already exists | Where |
| --- | --- | --- |
| entry price | `open_price` | `schemas.py:309`; filled from `place_order` at `orchestrator.py:4635` |
| entry timestamp | `created_at` | not passed at `orchestrator.py:4705`, so `default_factory=now_ts` runs *after* `place_order` — it is the execution time, not the signal time |
| result | `status` | `orchestrator.py:4889`, from `_bounded_check_result` → the provider's `check_result`. The broker is already the source of truth |
| P/L | `profit` | `orchestrator.py:4890`, same source |
| exit time | `closed_at` | `orchestrator.py:4892` |
| entry candle OHLC | **missing** | 5 new Optional fields |

- `TradeRecord` is built at exactly one place, `orchestrator.py:4705`.
- `trade_opened` (`:4803`) and `trade_closed` (`:5008`) both broadcast
  `trade.model_dump()`, so new fields reach the frontend with no new endpoint.
- The store's `trade_closed` handler replaces the existing entry
  (`store.ts:284`), so a marker updates in place — no page refresh.
- `TradeStore` is JSON (`trades.json`) and loads with
  `TradeRecord.model_validate`, so Optional fields need **no migration**; older
  records simply deserialize with `None`.
- `_revalidation_candles` (`orchestrator.py:~4380`) is `provider.get_candles`
  with a `candle_store` fallback — the same source the chart reads. Reusing it
  keeps one candle source, as required.
- `current_df` (`:4425`) is **not** usable for the entry candle: it is
  pre-execution revalidation data and its last, still-forming row is trimmed.

## What gets built

**Backend**

1. `TradeRecord` gains `entry_candle_timestamp`, `entry_candle_open`,
   `entry_candle_high`, `entry_candle_low`, `entry_candle_close` — all
   Optional, so nothing existing breaks.
2. `app/services/entry_candle.py` — one pure function,
   `find_entry_candle(candles, execution_ts, period)`. It aligns the execution
   timestamp with `candle_aggregate.bucket_start` (already tested, epoch
   aligned), looks for a candle with exactly that timestamp, and refuses to
   return anything unless that candle actually contains the execution time.
   Missing data yields `None`, never a guess and never a neighbouring bar.
3. `orchestrator.py` populates the fields right after the order is placed, from
   `_revalidation_candles`, inside try/except. The order is already committed by
   then, so a failure here loses metadata and nothing else.

**Frontend**

4. `Trade` type gains the same five fields.
5. Chart header always shows the current candle's OHLC; the crosshair overrides
   it while hovering. Values come from the candle series, never hardcoded.
6. Markers come from **trades**, not signals. The previous `drawSignalMarkers`
   used signal-generation time and `sig.price`, which is exactly what the
   requirement rules out — it is replaced, not supplemented.
7. After settlement the same marker shows IN THE MONEY / OUT OF MONEY (mapped
   from the broker's `status`, never recomputed) and P/L.
8. Trade History shows the entry candle OHLC per trade.

**Tests**

9. Boundary tests for the bucket lookup, including the stated case: an
   execution at 15:32:18 on a 15m chart must resolve to the 15:30 candle, and
   must not resolve to 15:15 or 15:45. Plus the exact-boundary instants, a
   missing candle, and a timestamp that falls in no candle's range.

## What this does not do

It does not recompute a result the broker already reported. It does not
reconstruct an entry candle from approximate data when the real one is
available, and it does not invent one when it is not. It does not add a candle
source, a trade model, or an endpoint.
