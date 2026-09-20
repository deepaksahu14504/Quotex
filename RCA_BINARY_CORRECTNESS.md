# RCA — Binary correctness fixes

**PR:** Binary correctness: volume=0 + pure price-action + WebSocket spam fix
**Branches:** `arena/01a0bd7f-quotex` → `main`
**Tests:** 207 backend regression + unit tests pass; 179 upstream `pyquotex` vendor tests pass.

---

## 1. Volume = 0 on binary (Quotex/pyquotex)

**Symptom:** Live signals almost never fired on real Quotex connectivity. Backtests
on Binance (where `kline.v` is real) looked fine; on pyquotex the pipeline
produced zero entries for minutes at a time and the debug log showed
`vol_strength=0.0` on every candle.

**Root cause:**
* `backend/app/engine/strategies.py` gated entries on `vol_strength > 0.55`
  (and other volume-derived confluence factors).
* On Quotex/pyquotex the broker does not publish real exchange volume — every
  candle arrives with `volume == 0`. The old code treated that as "no activity"
  rather than "data not available", so the volume gate was permanently closed.
* `backend/app/engine/indicators.py` `volume_strength()` divided by a rolling
  mean of zero → NaN, which then poisoned downstream comparators.

**Fix:**
* Removed all hard volume gates from binary strategies in `strategies.py`.
  Decisions now come from OHLC-derived features only: RSI, Stoch, Supertrend,
  Bollinger Bands, MACD, ATR, body/wick structure, and a price_strength that
  uses body_ratio + wick_rejection.
* `volume_ma()` / `volume_strength()` are defensive: when volume is all-zero
  they return a neutral 1.0 instead of 0/NaN so indicators don't collapse.
* `PyQuotexProvider.get_candles()` seeds `volume = ticks` (tick-count proxy)
  when the broker payload has no `volume` key — preserves an activity measure
  without inventing exchange volume, and keeps the Binance path unchanged.

## 2. Pure price-action confirmation for binary

**Symptom:** Even with the volume gate disabled, entries were noisy on short
timeframes. 1-minute signals had low win-rate in shadow mode.

**Root cause:** The legacy strategies bundled volume confirmation with price
confirmation; when volume was stripped out there was nothing explicitly
replacing that conviction check — just indicator thresholds.

**Fix:**
* Added explicit body_ratio + wick_rejection price-action confirmation
  directly into each binary strategy. A candle whose body is a small fraction
  of its range with a long rejection wick now counts as confluence, the same
  way volume used to (and this is the correct thing to look at on a
  no-volume book).
* Breakout strategy now requires a measured ATR move away from structure
  rather than a volume spike.
* Time-aware swings strategy uses MACD + Supertrend + BB alignment (three
  independent OHLC indicators) instead of a volume-confirmation leg.

## 3. WebSocket subscribe spam (the "spam" the title calls out)

Three distinct subscription-path bugs, each of which caused the websocket to
be hammered with duplicate or wasted frames. The tests in
`backend/test_market_service.py` §2 fail against pre-fix code.

### 3a. Check-then-act race in `start_candles_stream`
`stream_key in self._streamed` → `await start_candles_stream(...)` →
`self._streamed.add(stream_key)` was a TOCTOU across an `await`. The scan loop
and a concurrent revalidation/execution for the same asset could both read
"not yet subscribed" and both send a SUBSCRIBE frame. Fixed with an
in-flight set `self._subscribing`; the second coroutine skips the send and
picks up the stream on its next pass.

### 3b. Tick-cursor bug → permanent re-subscribe storm
The cursor used to be `len(ticks)`, and `pyquotex` caps `realtime_price` at
1000 entries with `pop(0)` eviction (`api.py:684`). Once any asset saturated
the buffer, `len()` pinned at 1000 forever, so `ticks[cursor:]` was `[]` on
every subsequent call. Two downstream effects:
* Live tick folding silently stopped — candles only grew from the periodic
  resync pull, which the system then interpreted as "stream not actually
  streaming" and re-subscribed as a recovery action.
* The `_tick_watcher` stall-detector used the same `len()` comparison, saw
  zero growth for 45 s and forced a reconnect — which tore down every
  subscription and re-subscribed every asset in a burst.

Fixed by switching the cursor to the **last folded tick's timestamp**
(immune to left-eviction). Keyed by `(asset, period)` tuple, not just asset,
so the three timeframes per asset (1m / 5m / 15m) no longer clobber each
other's cursor — previously the first timeframe to read advanced the
cursor past every tick and the other two got nothing, which is why HTF
confluence was weak.

### 3c. Idempotency of the boundary tick
While fixing 3b, the initial `>=` comparison meant the very last tick was
refolded on every single `get_candles()` call. OHLC is idempotent under
re-folding (max / min / last-write-wins) but additive volume was not — every
call bumped `volume` by 1 on the live candle, producing an ever-growing
fake volume spike that the confidence model then misread as conviction.
Fixed by changing the cursor to a `(timestamp, index_in_buffer)` pair:
same-second ticks that genuinely arrive after the previous fold are still
folded in, but the exact boundary tick is not processed twice.
`test_refolding_the_boundary_tick_is_idempotent` verifies this.

### 3d. Deep-history seed loop on broker-capped assets
`get_candles()` re-ran a ~150 s chunked history walk on every scan cycle for
any asset whose cache never reached `trading.history_fetch_bars` candles.
Once the broker's real ceiling (~120 bars on 1m) was reached, `seed_needed`
stayed True forever because `len(cache) < count` never flipped. That's one
~150 s dead-end per capped asset per cycle, holding the gather() and
producing the "scans gradually slow down then stop" symptom. Fixed with the
`_seed_plateaued_at` cooldown: if a full deep-seed gained < 5 candles, accept
the plateau for 30 min; live ticks + light resync keep the cache growing in
the meantime.

## 4. Bonus correctness fixes caught while writing RCA
* `candidates_excluded_by_payout` in `scan_telemetry()` was hard-coded to 0,
  making the frontend funnel panel misreport where assets were being
  excluded. Now computed from the funnel the same way
  `instruments_excluded_by_payout` is.
* Test helper `make_provider` was missing `_seed_plateaued_at` after the
  plateau fix landed — 14 tests were failing with AttributeError on a
  provider constructed via `__new__`. Restored the full init state.

## Verification
```
backend ....................... 207 passed in 25.23s
vendor/old-pyquotex tests ..... 179 passed in 50.38s
```
All fixes are covered by regression tests in
`backend/test_market_service.py`, `backend/test_scan_health.py`,
`backend/test_seed_plateau.py`, `test_stake_cap.py`, and
`test_revalidation_fallback.py`.
