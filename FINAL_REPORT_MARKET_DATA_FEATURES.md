# Final Report — PyQuotex market-data feature architecture

Repo `/home/user/Quotex` · branch `arena/01a0bee8-quotex`
Task commits: `117af6a`, `dbadfd4`, `20b9f93`, `907c12c`, `9faefd4`
Inspection evidence: `RCA_MARKET_DATA_FEATURES.md`

**Verification:** full backend suite **458 passed / 1 skipped**. `import app.main`
succeeds with 69 routes. Every claim below is backed by a command run in this
workspace; nothing is inferred from upstream pyquotex documentation.

---

## 1. Files changed

| File | Change |
|---|---|
| `backend/app/engine/market_features.py` | **NEW** (701 L) — price-action, tick-activity, sentiment normalisation, data quality, `MarketFeatures` |
| `backend/app/services/realtime_state.py` | **NEW** (332 L) — `RealtimeStateManager`, keyed `(asset, timeframe)` |
| `backend/alembic/versions/0002_candle_volume_source.py` | **NEW** — nullable `candles.volume_source` |
| `backend/test_market_features.py` | **NEW** (872 L) — 41 tests |
| `backend/app/config.py` | `MarketDataSettings` added to `RuntimeSettings` |
| `backend/app/engine/incremental.py` | features wired into `_add_derived_columns`; windowed copy-back list extended |
| `backend/app/engine/strategies.py` | same features in `_enrich` (backtest parity) |
| `backend/app/orchestrator.py` | `_collect_market_features`, `_market_features_log_fields`, bounded adjustment, structured log |
| `backend/app/services/market.py` | provider `get_realtime_sentiment`/`get_realtime_ticks`; **5 sites** stopped writing tick counts into `volume` |
| `backend/app/services/candle_store.py` | persists + reads back `volume_source` |

`2507 insertions(+), 59 deletions(-)` across 11 files.

---

## 2. Data flow **before**

```
pyquotex ws frame [asset, ts, price, direction]
   │ api.py:783-796   (direction DROPPED; no volume field exists)
   ▼
api.realtime_price[asset]   1000-entry left-evicting list
   │ market.py get_realtime_price()
   ▼
get_candles() tick-fold:  row["volume"] += 1.0   ◄── TICK COUNT AS VOLUME
                          row["ticks"]  += 1
                          _volume_source = "synthetic"
   ▼
Candle(volume=tick_count, volume_source="synthetic")
   ├─► CandleStore ── volume_source DROPPED, read back hardcoded "real"  ◄── TAG LAUNDERED
   └─► IndicatorCache ─► _has_real_volume() ─► volume_ma / volume_strength
```
Sentiment: **zero references in `app/`** — `grep -rniE "sentiment|traders_?mood" --include=*.py app/` returned nothing.

## 3. Data flow **after**

```
OHLC history ─┐
              ├─► IndicatorCache.get_enriched()
ticks ────────┘        │  existing indicators UNCHANGED
                       │  + 20 price-action columns (OHLC only)
                       ▼
                  edf_eval = edf.iloc[:-1]      (closed candles, unchanged)
                       │
                       ▼
              strategies.evaluate()  ─► res.direction, res.confidence
                       │
                       ▼
   adjusted = res.confidence + regime_adj                 (existing)
                       │
   ┌───────────────────┴──────────────────────────────────────────┐
   │ _collect_market_features(asset, tf, edf_eval, direction)     │
   │   RealtimeStateManager keyed (asset, timeframe):             │
   │     tick activity  ◄─ provider.get_realtime_ticks()          │
   │     sentiment      ◄─ provider.get_realtime_sentiment()      │
   │   sentiment_confidence_adjustment() → bounded, ≤ cap         │
   └───────────────────┬──────────────────────────────────────────┘
                       ▼
   adjusted += sentiment_adj  (0.0 when disabled/stale/weak/missing)
                       ▼
   calibrate() → threshold gate → Signal       (both UNCHANGED)
```

## 4. Where synthetic volume was removed / isolated

**Removed (C.2)** — five sites in `market.py` no longer write tick counts into `volume`:
history path, resync path, tick-fold seed, tick-fold increment, `fetch_history`
fallback, plus the `get_candles` Candle-construction fallback.

Verified by driving the **real** `PyQuotexProvider.get_candles()`: after folding
7 ticks the live bar is `volume=0.0`, `volume_source="synthetic"`, `ticks=7`,
OHLC still correct (`open 1.10`, `close 1.1006`); all 10 candles `volume=0`.
Pre-fix the same call returns `volume=6.0`.

**Isolated (C.1)** — `volume_source` is now persisted (migration `0002`) and read
back. Measured before: `_has_real_volume` `False→True` and `volume_strength`
`1.0 → 0.5` through the PG round-trip. After: stays `False` / `1.0000`. Real
volume still reads back `"real"` and still computes (`0.5000`).

**Preserved, not deleted:** the `volume_source` field, its `"real"` default, the
`_volume_source` column in `candles_to_df`, `_carry_volume_source()`, and
`_has_real_volume()`. Neutral behaviour confirmed: `volume_ma`/`volume_strength`
return `1.0` on synthetic.

## 5. How OHLC is used

Primary and unchanged. Closed candles remain the strategy input
(`edf_eval = edf.iloc[:-1]`); the forming candle is context only. Twenty new
columns are derived from OHLC **alone**, so they are meaningful with `volume=0`:

`candle_body_ratio`, `upper_wick_ratio`, `lower_wick_ratio`, `candle_direction`,
`close_location_value`, `candle_range`, `range_vs_atr`, `atr_normalized_move`,
`short_term_return`, `momentum`, `atr_normalized_momentum`,
`consecutive_bullish`, `consecutive_bearish`, `bullish_rejection`,
`bearish_rejection`, `breakout_distance`, `breakdown_distance`,
`resistance_distance`, `support_distance`, `price_position_in_range`.

Structure levels use `shift(1)` so a bar never measures distance to a level it
is itself creating. No existing column is overwritten (`atr` is preserved —
tested with a sentinel). Live-vs-backtest parity on the last closed bar:
**max diff 1.66e-06** (Wilder ATR recursion residual).

## 6. How realtime ticks are used

Activity/microstructure only. Nine fields, none named volume:
`tick_count`, `price_update_count`, `price_update_frequency`, `tick_activity`,
`activity_intensity`, `tick_direction_balance`, `intrabar_momentum`,
`intrabar_price_change`, `intrabar_range`.

`tick_direction_balance` is derived from **signed price changes**, because the
vendor drops the wire `direction` element when storing ticks (`api.py:785`
unpacks 3 of 4). `activity_intensity` is the bar's count against a baseline of
*closed* bars. Duplicates are de-duplicated on `(ts, price)`; out-of-order ticks
are flagged and routed to their own bucket.

## 7. How sentiment is used

Read from the vendor's own shared state — `_api/realtime.py:494`
`get_realtime_sentiment`, populated by the `"sentiment"` control frame
(`api.py:363-369`, registered `api.py:171`). Payload keys taken from the
vendor's own consumer `cli/commands/realtime.py:59-60`: `call`/`put`, with
`bulls`/`bears` aliases.

`sentiment_bias = (buy_pct − sell_pct) / 100`; also stored: `sentiment_buy`,
`sentiment_sell`, `sentiment_strength = |bias|`, `sentiment_timestamp`,
`sentiment_stale`. One-sided payloads infer the complement.

It is **bounded secondary confirmation only**. `res.direction` is already fixed
by the OHLC engine before the adjustment runs; the function returns a number,
never a direction.

## 8. Is sentiment optional?

**Yes, and off by default** (`sentiment_enabled: bool = False`).

- Disabled → **zero** provider calls (asserted: `sentiment_calls == 0`) and
  adjustment exactly `0.0`.
- Missing / unparseable → `available=False`, adjustment `0.0`, reason `missing`.
- Stale (`> sentiment_max_age_seconds`) → `0.0`, reason `stale`.
- Weak (`|bias| < sentiment_min_strength`) → `0.0`, reason `weak`.
- Provider raising → caught; bundle still returned, ticks still collected.
- Legacy `runtime_settings.json` with no `market_data` key loads unchanged.

Absence never reduces confidence. Configurable: `sentiment_enabled`,
`sentiment_max_age_seconds` (120), `sentiment_min_strength` (0.10),
`sentiment_max_confidence_adjustment` (4.0), `sentiment_disagreement_penalty`
(default half the cap).

## 9. Confidence impact and configured cap

Applied after `regime_adj`, before calibration, so the calibrator still sees one
number and the threshold gate is untouched. Measured sweep:

| case | adjustment |
|---|---|
| fresh strong CALL-agreeing | **+1.778** |
| fresh mild (56/44) | +0.089 |
| fresh strong CALL-disagreeing | **−1.111** |
| weak / stale / missing / disabled | **0.000** |
| cap=3, strong agree | +2.667 |
| cap=5, strong disagree | −2.222 |

A parametrised test sweeps every bias 0–100 in steps of 5 for both directions
at caps 0/1/3/4/5 and asserts `|adjustment| ≤ cap`, and that the cap is
actually reached (so the assertion is not vacuous). Disagreement never reverses
a signal: `78.0 − 1.111 = 76.9` still clears a 70 threshold.

## 10. Tests before / after

| | before | after |
|---|---|---|
| backend tests | 417 passed / 1 skipped | **458 passed / 1 skipped** |
| new file | — | `test_market_features.py`, 41 tests |

All 16 required behaviours covered, plus 5 extras (real volume still honoured,
columns don't clobber, no invented neutral, sign symmetry, memory bound,
no-secret log assertion, integration wiring).

## 11. Full test result

```
458 passed, 1 skipped in 46.27s
```
`import app.main` → 69 routes. Secrets guard: 9 passed. `git ls-files` matches
for `session.json` / `backend/data/`: **0 tracked**.

**Pre-fix proofs** (each fix reverted, test run, fix restored):
- `candle_store` read-back → hardcoded `"real"`: `test_candle_store_persists_volume_source` fails (`'real' != 'synthetic'`)
- windowed copy-back → old 10-column list: `test_incremental_engine_preserves_new_feature_fields` fails (row 60 all-NaN across all 20 columns)
- tick-fold → `volume += 1.0`: `test_pyquotex_tick_fold_never_puts_tick_count_into_volume` fails (`volume=6.0`)

## 12. Unresolved issues

1. **Live broker sentiment was never observed.** No Quotex credentials here, so
   the payload shape is taken from the vendor's own CLI consumer
   (`call`/`put`, `bulls`/`bears`), not from a captured frame. Both spellings
   plus `"62%"` strings and 0–1 fractions are handled, but a genuinely
   different key set would surface as `available=False` (safe, silent).
   Recommend one live probe via `scripts/manual_quotex_probe.py` before enabling.
2. **`sentiment_enabled` defaults to False** deliberately. Enabling it is a
   one-line settings change, not a code change.
3. **Streak features are window-bounded.** `consecutive_bullish` caps at the
   derived window length (200); a longer real streak reads as 200. Inherent to
   windowed recompute, not a defect, but worth knowing.
4. **RCA findings from Task 1 still open** (unchanged by this task): F9
   `stale_data_pause_seconds` never fires, F10 `edf.iloc[:-1]` is positional and
   never timestamp-verified, F13 unhandled frontend event types, F14 `get_payout`
   maps every timeframe except `"5m"` to the 1M payout.
5. **Cannot be done from this sandbox:** revoking/rotating the leaked Quotex
   sessions and Telegram token, purging secrets from git history, and
   `docker compose build`.

## 13. Broker / vendor API limitations

- **No traded volume exists on this path.** The tick frame is
  `[asset, ts, price, direction]` (`api.py:790`); history rows carry no volume
  key. Any Quotex "volume" is a tick count and is now kept out of the field.
- **`direction` is dropped by the vendor handler** (`api.py:785` unpacks 3 of 4),
  so `tick_direction_balance` must be derived from price changes.
- **`realtime_price` is a 1000-entry left-evicting list** (`api.py:789-793`).
  Position-based cursors break at saturation; the existing
  `(ts, index-within-ts)` cursor and the `(asset, period)` keying are reused,
  not reimplemented.
- **`start_mood_stream` is conditional** — it guards on
  `hasattr(api, "subscribe_Traders_mood")` / `hasattr(api, "traders_mood")`
  (`_api/realtime.py:599-602`), so the *mood* path may not exist. The
  **sentiment** path is unconditional and is what this implementation uses.
- **`_h_sentiment` stores the raw payload unchanged** (`api.py:363-369`), so key
  names are a broker-side contract this repo does not control. Normalisation is
  centralised in `normalize_sentiment()` for exactly that reason.
- **History ceiling.** The broker caps deep history (~120 bars); this is why the
  warm-up gate relaxation from Task 1 (`effective_min_bars`) still matters and
  why the derived window is 200 rather than larger.

---

### Accuracy claims

None are made. This work changes data correctness and feature availability, not
predicted win rate. No backtest was run that would support an accuracy figure,
and no confidence threshold was lowered to compensate for anything.
