# Inspection — signal pipeline before the Market Evidence layer

Repo `/home/user/Quotex` · branch `arena/01a0bee8-quotex` · HEAD `296459d`
**Inspect-only.** Nothing was modified to produce this file. Every line below
came from reading code in this workspace this session.

---

## 1. Current confidence flow (verified line by line)

`orchestrator._evaluate_asset_signal()`:

```
candles  ← provider.get_candles(asset, tf, history_fetch_bars)
   ▼
IndicatorCache.get_enriched(f"{asset}|{tf}", candles)  → edf, warm
   ▼  edf_eval = edf.iloc[:-1]            (closed candles only)
strategies.evaluate(edf_eval, per_asset_strats, pre_enriched=True, ...)  → res
   │     res.confidence = clamp(44 + 15.5*net + confluence_bonus)
   │     then ± for directional_edge, body_ratio, atr_pct   (strategies.py:889-908)
   ▼
contributing = [n for n,d in res.votes.items() if d == res.direction.value]
regime_adj            = regime_tracker.blended_adjustment(contributing, res.regime)
adjusted_confidence   = clamp(res.confidence + regime_adj)          # :3304-3305
   ▼
sentiment_adj         ← _collect_market_features(...)               # :3316-3325
adjusted_confidence  += sentiment_adj.adjustment   (0.0 when disabled)
   ▼
calibrated_confidence = calibrator.calibrate(adjusted_confidence, regime)  # :3327
   ▼
if confidence_model_enabled: blend with live_confidence_model_prediction   # :3328-3331
   ▼
effective_threshold   = _effective_confidence_threshold(payout)            # :3332
                      + regime_transition_result.recommended_threshold_adjustment
   ▼
GATE  calibrated_confidence < effective_threshold  → reject                # :3334
   ▼
GATE  precision gate (opt-in): evaluate_precision(...)                     # :3347-3363
   ▼
Signal(confidence=calibrated_confidence, raw_confidence=adjusted_confidence)
```

The requested order — *strategy consensus → regime/MTF → market evidence →
calibration → threshold → precision → execution* — is already the shape of this
chain. The evidence layer slots into the existing `sentiment_adj` position
(`:3316-3325`), which is **after** regime and **before** calibration. No
reordering is needed and none will be done.

## 2. What already exists and must not be duplicated

`market_features.py` (previous task) already produces:

- **Tick activity** (`TickActivity`, keyed `(asset, timeframe)` via
  `RealtimeStateManager`): `tick_count`, `price_update_count`,
  `price_update_frequency`, `tick_activity`, `activity_intensity`,
  `tick_direction_balance`, `intrabar_momentum`, `intrabar_price_change`,
  `intrabar_range`.
- **Price action** (20 OHLC-only columns) including all nine the request names.
- `MarketFeatures.flat()` and `_market_features_log_fields()` for attribution.

So this task is a **scoring + attribution** layer over data that already exists,
not a new data layer.

## 3. Double-counting analysis (rule 3) — which features are genuinely independent

Extracted with `ast` from `strategies.py` — every `row[...]` / `row.get(...)`
each strategy reads:

| strategy | row fields read |
|---|---|
| strat_trend_pullback | close, ema9, ema21, ema50, rsi |
| strat_ema_ribbon | close, ema5, ema8, ema13, ema21 |
| strat_supertrend / psar / bollinger_bounce / macd_cross / stoch_rsi / adx_di | (indicator columns only) |
| strat_mean_reversion | bb_low, bb_up, close, rsi, stoch_k |
| strat_momentum_breakout | bb_mid, bb_width, close, macd_hist |
| strat_rsi_divergence | — |
| strat_volatility_normalized_mean_reversion | bb_low, bb_up, close, rsi, stoch_k, **price_strength**, **support**, **resistance**, vol_ratio |
| strat_confluence_breakout | bb_mid, close, **confluence**, ema9, ema21, macd_hist, **price_strength**, **support**, **resistance** |
| strat_session_volatility_swing | close, ema9, ema13, ema21, macd, macd_sig, macd_hist, rsi, st_dir, time_vol_mult |
| strat_multi_timeframe_confluence | adx, bb_mid, close, ema9, ema21, ema50, macd, macd_sig, **price_strength**, rsi, st_dir |

Plus the confidence formula itself reads `body_ratio` (`strategies.py:898`).

**Verdict:**

| requested feature | independent? | note |
|---|---|---|
| `close_location_value` | **yes** | no strategy reads it or an equivalent |
| `atr_normalized_momentum` | **yes** | strategies use macd_hist/rsi, not ATR-normalised displacement |
| `breakout_distance` / `breakdown_distance` | **yes** | rolling-50 prior extreme, ATR-normalised; `strat_confluence_breakout` tests the *pivot* `resistance`/`support` level instead |
| `bullish_rejection` / `bearish_rejection` | **partial** | `price_strength` blends wick ratio and is read by 3 strategies → down-weight |
| `price_position_in_range` | **yes** | rolling high/low range; strategies use Bollinger bands, a different construct |
| `support_distance` / `resistance_distance` | **partial** | `support`/`resistance` are read by 2 strategies |

**Design consequence:** `support_distance`/`resistance_distance` will **not** cast
a directional vote. The strategies already ask "is price *at* the level?"; these
will instead answer a different question — "does the direction have *room to
run*?" — and act only as a headroom dampener. `bullish_rejection`/
`bearish_rejection` are down-weighted because `price_strength` already carries
wick information into three votes.

## 4. Constraint checks

- **Correlation guard (rule 6):** `strategies.py:802-805`
  `_correlation_guard.apply_correlation_penalty(votes, adjusted_weights, side)`
  runs **before** the confluence score, so correlated strategy groups are already
  capped. The evidence layer reads only `res.votes` for attribution and never
  adds confidence for vote count.
- **FEATURE_NAMES (rule 11):** `confidence_model.py:51-64`, **12** entries
  (`net, win_n, directional_edge, body_ratio, atr_pct, htf_agree, hhtf_agree,
  is_trend, is_range, regime_transition_probability,
  regime_transition_confidence, regime_stability_score`). Will **not** be
  extended. `confidence_model.py:145` explicitly refuses to use a model whose
  fitted length differs from `len(FEATURE_NAMES)`, so extending it would
  silently disable every persisted model.
- **Calibration (rule 7):** `ConfidenceCalibrator.calibrate()`
  (`calibration.py:270`) is a passthrough until built. Evidence adjustment goes
  in **before** it, so the calibrator still sees one combined number.
- **Precision gate (rule 7):** `precision_gate.evaluate_precision()` runs after
  the threshold gate and is untouched.
- **Volume (rule 1):** confirmed already fixed. `grep` for
  `float(c["ticks"])` / `volume += 1.0` in `market.py` → **none**. `volume` stays
  0 on the pyquotex path; `_has_real_volume()` returns False; `volume_ma` /
  `volume_strength` return neutral 1.0.

## 5. Attribution surface

`StrategyResult` (`strategies.py:79-95`) already carries `direction`,
`confidence`, `reasons`, `votes`, `regime`, `raw_votes` (per-strategy
`{direction, raw_weight, adjusted_weight, regime_mult, reason}`) and
`raw_features` (the 13 FEATURE_NAMES values). `PrecisionResult` carries
`agreeing` / `opposing`. `_market_features_log_fields()` already emits
`base_confidence`, `sentiment_adjustment`, `final_confidence`,
`effective_threshold`.

So `agreeing_strategies` / `opposing_strategies` are derivable from `res.votes`
without new plumbing, and the rest extend the existing field set.
