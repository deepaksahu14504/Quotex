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

---

## Outcome

Four commits: `cffc7a5` (lookup), `c90cc9a` (capture), `0dc0db6` (chart),
`411e154` (history).

### Checks run

| Check | Result |
| --- | --- |
| Backend suite | 739 passed, 1 skipped (was 704/1) |
| `test_entry_candle.py` | 29 passed |
| `test_trade_entry_ohlc_api.py` | 6 passed |
| Frontend `npm run test:chart` | 35 passed, 0 failed |
| `tsc -b && vite build` | clean, 332 modules |
| oxlint over `src` | 3 warnings, 1 error — identical to the pre-task baseline |

The one lint error is the pre-existing conditional `useState` at
`Settings.tsx:165`; nothing in this task touches that file.

### Verified live, over real HTTP

A trade was written into a real user's store and read back through
`GET /api/trades`:

```
created_at (execution)   = 1759246338   15:32:18
entry_candle_timestamp   = 1759246200   15:30:00
chart bucket             = 1759246200   MATCH
entry_candle_open/high/low/close = 1.1441 / 1.14435 / 1.144 / 1.14422
status = win   profit = 425.0   closed_at = 1759247100
```

This is requirement 10 checked end to end: the execution at 15:32:18 resolved
to the 15:30 candle, not to 15:15 (1759245300) and not to 15:45 (1759247100),
and the bucket the chart computes for the same instant is the same number.

### What could not be verified here

Two gaps, both environmental:

1. **No trade has been executed against a live broker in this session.** The
   sandbox has no outbound network, so `_attach_entry_candle` has not run
   against real provider candles on the real execution path. Its body is
   exercised by tests calling it unbound, with a stubbed candle fetch — the
   method is real, the broker is not.
2. **The UI has not been looked at.** There is no browser here. The chart and
   History were type-checked, built and linted, and the pure logic tested, but
   the rendered result is unconfirmed.

### One deliberate difference from the request

The mock shows a four-line label stacked on the chart
(`CALL` / `Entry 1.14422` / `IN THE MONEY` / `+$500`). `SeriesMarker.text` is a
plain string and lightweight-charts draws it as one line, so the on-chart label
is compact (`CALL`, or `CALL +$500.00` once settled) and the full stacked detail
— direction, entry price, entry candle OHLC, result, P/L — appears as a block
when the crosshair is on that bar, and in full in Trade History. No information
was dropped to fit the library; only its placement moved.
