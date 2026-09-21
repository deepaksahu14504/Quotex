# Report — Market Evidence Score layer

Repo `/home/user/Quotex` · branch `arena/01a0bee8-quotex`
Commits: `847a97a`, `2161f35`, `bbcdc64`
Inspection: `INSPECTION_MARKET_EVIDENCE.md` (written before any change)

**Verified:** full backend suite **501 passed / 1 skipped** (was 458/1).
`import app.main` → 69 routes. One real signal traced end to end through the
actual `_evaluate_asset_signal`.

---

## 1. Files changed

| File | Δ | Why |
|---|---|---|
| `backend/app/engine/market_evidence.py` | **+470 (new)** | The scoring layer. `EvidenceConfig`, `MarketEvidence`, `activity_score()`, `price_action_score()`, `headroom_dampener()`, `evaluate_market_evidence()`. Needed because nothing combined tick activity and price action into one bounded number. |
| `backend/app/config.py` | +21 | 12 `market_evidence_*` settings (rule 9). Added to the existing `MarketDataSettings` rather than a new class so one settings object still covers all market-data behaviour. |
| `backend/app/orchestrator.py` | +178/−15 | Wiring: `_evaluate_market_evidence()`, `EVIDENCE_ROW_FIELDS`, the adjustment at the existing post-regime/pre-calibration slot, and full attribution on all three outcomes (generated, confidence-rejected, precision-rejected). |
| `backend/test_market_evidence.py` | **+615 (new)** | 43 tests (rule 10). |
| `backend/test_market_features.py` | +19/−15 | 3 call sites updated for the now-3-tuple `_collect_market_features`; the no-secrets test updated for the intentional `asset`/`timeframe` removal (see §9). |
| `INSPECTION_MARKET_EVIDENCE.md` | **+137 (new)** | The inspect-only findings this design is based on, including the double-counting analysis. |

No strategy, indicator, calibration, risk, precision-gate or execution file was
modified.

## 2. Old confidence flow

```
strategies.evaluate()            → res.confidence   (44 + 15.5*net + bonus, ± edge/body/atr)
  → + regime_adj                 → adjusted_confidence
  → + sentiment_adj (0.0 default)
  → calibrate()                  → calibrated_confidence
  → (optional ML blend)
  → effective_threshold          → GATE
  → precision gate               → GATE
  → Signal
```

## 3. New confidence flow

```
strategies.evaluate()            → res.confidence                      (unchanged)
  → + regime_adj                 → adjusted_confidence                 (unchanged)
  → + sentiment_adj (0.0 default)                                      (unchanged)
  → + market_evidence_adjustment  ← NEW, bounded ±cap
  → calibrate()                  → calibrated_confidence               (unchanged)
  → (optional ML blend)                                                (unchanged)
  → effective_threshold          → GATE                                (unchanged)
  → precision gate               → GATE                                (unchanged)
  → Signal
```

This is exactly the order rule 7 asks for. The new term slots into a position
that already existed, so **no reordering happened** and calibration still sees
one combined number. Ordering is pinned by
`test_calibration_runs_after_the_evidence_adjustment`, which asserts the source
positions of `market_ev.adjustment` < `calibrator.calibrate(` <
`calibrated_confidence < effective_threshold` < `evaluate_precision(`.

**Scoring.** Two independent components, each in [−1, +1]:

- `activity_score` — 0.60·`tick_direction_balance` + 0.40·net displacement
  (`intrabar_price_change / intrabar_range`). `activity_intensity` is **not**
  directional; it only gates trust (below `min_activity_intensity` or above
  `max_activity_intensity` → neutral). Trust also ramps with tick count.
- `price_action_score` — 0.30·`close_location_value` + 0.20·range position +
  0.30·tanh(`atr_normalized_momentum`) + 0.10·rejection asymmetry +
  0.10·breakout/breakdown.

CALL confirms only when **both** components clear their own thresholds and point
CALL; same for PUT. The composite is then expressed in the already-chosen
direction (`alignment`), so the adjustment is positive when evidence agrees and
negative when it opposes.

**Double-counting avoided (rule 3).** `support_distance` / `resistance_distance`
cast **no** directional vote — `strat_confluence_breakout` and
`strat_volatility_normalized_mean_reversion` already test whether price is *at*
a level. They act only as `headroom_dampener`, answering a different question
("is there room to run?"), and can only reduce (range 0.5–1.0), never amplify.
`bullish_rejection` / `bearish_rejection` carry the smallest weight (0.10)
because `price_strength` already feeds wick structure into three votes.
`test_support_and_resistance_distance_do_not_cast_a_directional_vote` pins this.

**No vote counting (rule 6).** `test_evidence_layer_does_not_read_strategy_votes`
asserts the scoring function's source contains no reference to `votes`, `win_n`,
`agreeing` or `opposing`. The correlation guard at `strategies.py:802-805` is
untouched.

## 4. Maximum possible adjustment

Measured with saturated inputs at default settings:

| case | adjustment |
|---|---|
| strongest possible confirmation | **+5.0000** (`market_evidence_max_adjustment`) |
| strongest possible disagreement | **−3.0000** (`market_evidence_disagreement_penalty`) |
| confirmation pressed against the level (no headroom) | +2.5000 |
| observed on the traced real signal | **+1.8229** |
| weak / missing / disabled / no direction | **0.0000** |

Ordinary evidence lands **below** the cap by design — the payout scales linearly
from the weaker threshold up to full agreement, so it tracks measured evidence
instead of being a flat +5. `test_realistic_evidence_lands_below_the_cap` pins
that, and `test_the_cap_is_actually_reachable` proves the ceiling is live rather
than unreachable. Caps are re-clamped at the end of
`evaluate_market_evidence` regardless of weights or dampener.

## 5. Is PyQuotex volume used? **NO**

- `market_evidence.py` contains the word "volume" only in three comments stating
  it is *not* used (`grep -in volume` → lines 15, 18, 232, all prose).
- No field in `MarketEvidence` or `TickActivity` is named like a volume.
- `test_evidence_layer_never_reads_or_requires_volume` feeds the same row with
  `volume=0.0` and with `volume=37.0` (a tick count) and asserts the scores are
  **identical** — proving volume is not an input, not merely an unused one.
- `test_price_action_score_uses_only_the_whitelisted_columns` pollutes the row
  with `volume=999999`, `vol_strength=50`, `vol_ma=1e6` and asserts no change.
- Upstream is unchanged from the previous task: `volume` stays 0 on the pyquotex
  path, `volume_source="synthetic"`, and `_has_real_volume()` returns False so
  `volume_ma` / `volume_strength` stay neutral at 1.0.

## 6. Test results

```
501 passed, 1 skipped in 47.72s
```
was 458/1 → **+43 tests**, no regression.

All twelve behaviours rule 10 named, plus extras:

| required | test |
|---|---|
| strong CALL activity confirmation | `test_strong_call_activity_confirms_a_call` |
| strong PUT activity confirmation | `test_strong_put_activity_confirms_a_put` (+ bullish evidence must not confirm a PUT) |
| opposite tick direction | `test_opposite_tick_direction_conflicts_and_penalises` |
| weak/no tick data | 5 parametrised variants + `test_missing_row_is_neutral` |
| zero/synthetic volume | `test_evidence_layer_never_reads_or_requires_volume` |
| neutral price action | `test_neutral_price_action_yields_no_adjustment` |
| conflicting evidence | `test_conflicting_evidence_penalty_is_bounded_and_smaller_than_confirmation` |
| adjustment cap | 5 caps × all combinations + reachability + extreme inputs |
| no direction reversal | `test_evidence_layer_cannot_reverse_the_signal_direction` |
| existing threshold still works | traced live (§7) + `test_calibration_runs_after_the_evidence_adjustment` |
| calibration still runs after | same ordering test |
| precision gate still works | traced live (§7) — it still rejected |

Plus: config plumbing in both directions, `FEATURE_NAMES` unchanged (rule 11),
attribution completeness + no-secret assertion, and the splat-collision
regression.

**Pre-fix proof:** reverting the `asset`/`timeframe` fix makes
`test_attribution_is_safe_to_splat_alongside_explicit_call_site_fields` fail
with `'asset' not in {...}`.

## 7. End-to-end trace of one signal (rule 12)

Real `Orchestrator` on a pgserver DB, stub provider serving 300 synthetic-volume
candles and a 60-tick stream, `precision_mode_enabled=True`,
`confidence_threshold=55`, payout 85%:

```
candles/ticks  300 OHLC bars (volume=0, synthetic) + 60 ticks
indicators     warm=True, 300 rows, price-action columns present
strategies     call · confidence 97 · regime trend
votes          agree: ema_ribbon, supertrend, adx_di, momentum_breakout,
                      session_swing, multi_confluence
               oppose: mean_reversion, vol_norm_reversion
regime         regime_adjustment 0.0
market evid.   activity_score 0.6875 · price_action_score 0.8617
               verdict confirms · adjustment +1.8229
before calib   98.8229
calibration    passthrough (curve not built) → 98.8229
threshold      effective 62.05 → PASSED
precision gate REJECTED — "2 strategies voted opposite (counter-vote veto, max 0)"
```

The evidence layer confirmed and added 1.82 points; the precision gate still
rejected on vote structure exactly as it did before. That is the intended
division of labour — **evidence adjusts confidence, gates still decide.**

## 8. Configuration (rule 9)

All on `RuntimeSettings.market_data`, backward compatible (a legacy
`runtime_settings.json` with no `market_data` key loads with these defaults):

```
market_evidence_enabled                          = True
market_evidence_max_adjustment                   = 5.0
market_evidence_disagreement_penalty             = 3.0
market_evidence_activity_confirmation_threshold  = 0.25
market_evidence_min_ticks_for_activity           = 8     # min tick data quality
market_evidence_min_activity_intensity           = 0.40
market_evidence_max_activity_intensity           = 4.00
market_evidence_price_action_threshold           = 0.20
market_evidence_headroom_min_atr                 = 0.35
market_evidence_require_both_components          = True
market_evidence_activity_weight                  = 0.45
market_evidence_price_action_weight              = 0.55
```

`test_every_config_field_maps_to_a_real_setting_in_both_directions` asserts every
dataclass field has a mapping **and** that key exists on `MarketDataSettings`.
This test exists because of a real bug: the first version prefixed only
`enabled` and read the other eleven unprefixed, so they silently fell back to
dataclass defaults. Invisible in a smoke test, since the defaults match — only
setting a non-default value exposed it.

## 9. Two bugs found and fixed while building this

1. **`EvidenceConfig.from_settings` ignored 11 of 12 settings.** Silent, because
   defaults matched. Now an explicit mapping, asserted complete both ways.
2. **Attribution splat collided with call-site kwargs.** `MarketFeatures.flat()`
   emits `asset`/`timeframe`; every call site also passes them, so
   `log_event(**fields)` raised `TypeError: got multiple values for keyword
   argument 'asset'`. **This was on the main `signal_generated` path** and had
   shipped in the previous task — the helper-level test could not catch it, only
   splatting the way real call sites do. Fixed by dropping both keys in the
   helper; regression test proven to fail pre-fix.

## 10. Remaining reasons a signal can still fail the threshold

The evidence layer did not weaken any gate, so all pre-existing rejection paths
remain live. A signal can still fail because:

1. **Insufficient confluence** — `win_n < confluence_min_confirmations` while any
   strategy opposes (`strategies.py:865`).
2. **Weak directional edge** — `directional_edge < confluence_min_directional_edge`
   (`strategies.py:887`).
3. **Confidence below the effective threshold** — still payout-aware
   (`100/(1+payout) + margin`), still latency-widened, still shifted by regime
   transitions. The max +5 evidence cannot bridge a large gap: on the traced
   signal the gap was 98.8 vs 62.05, but a marginal 60-vs-65 signal stays
   rejected.
4. **Precision gate** (when enabled) — as the trace shows, this rejected a
   98.8-confidence signal outright: min agreeing, counter-vote veto, regime
   alignment.
5. **Decision-engine evidence gate** — `decision_min_evidence_count` /
   `decision_min_confidence_threshold` (`orchestrator.py:3378-3396`).
6. **MTF hard veto** — a strong counter-trend on the HHTF tier vetoes outright.
7. **Risk controls** — `can_trade` order is unchanged: paused → daily loss →
   consecutive losses → trades_today → max_concurrent → cooldown. Plus the
   drawdown breaker, correlation guard, and stake cap (which returns 0.0 when
   even $1 breaches `max_stake_pct_of_balance`).
8. **Evidence returning 0.0** — under 8 ticks, a too-quiet or anomalous bar, no
   intrabar range, or price action below threshold. This is neutral by design; it
   neither helps nor hurts.

## 11. Not done / caveats

- **No accuracy or win-rate claim is made.** No backtest here would support one,
  and no threshold was lowered to generate more signals. The default cap is 5
  points against thresholds in the 55–65 range, so this cannot manufacture
  signals on its own.
- **`FEATURE_NAMES` is unchanged** (rule 11) — all 13 entries asserted
  byte-identical. Extending it would silently disable every persisted model,
  because `confidence_model.py:145` refuses a model whose fitted length differs.
  The evidence is exposed separately via `MarketEvidence.as_dict()`, so ML
  integration later needs no change to the existing vector.
- **Live tick data was never observed.** No Quotex credentials in this sandbox,
  so activity scoring is exercised against synthetic tick streams only. The
  thresholds (8 ticks, intensity 0.40–4.00) are reasoned defaults, not
  empirically tuned — worth revisiting against real traffic.
- **Weights are hand-set, not fitted.** 0.60/0.40 within activity, 0.45/0.55
  between components, and the price-action weights are chosen for independence
  from existing votes, not optimised against outcomes. Fitting them would need a
  labelled outcome history this workspace does not have.
