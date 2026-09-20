# QuotexAutoTrader — FULL READ-ONLY CORE PATH INSPECTION + RCA

**Date:** 2026-09-20
**Mode:** READ-ONLY. Koi code modify nahi kiya gaya.
**Working tree status:** `git status --porcelain` → **empty** (clean). Sirf `backend/.venv/` banaya (gitignored) aur scratch verification scripts `/tmp/` mein.

---

## 0. Konsa branch inspect kiya — proof

```
$ git for-each-ref --sort=-committerdate refs/remotes refs/heads
2026-09-20 09:10:53 +0000  main                    58bcb4c  UI: fix indicator display... (#4)
2026-09-20 09:10:53 +0000  origin/HEAD             58bcb4c  ...
2026-09-20 09:10:53 +0000  origin/main             58bcb4c  ...
2026-09-20 09:10:40 +0000  arena/01a0bee8-quotex   4253d79  ...

$ git rev-parse origin/main^{tree}   → 9120508db6472513ef203c6aa471cbd1709610aa
$ git rev-parse HEAD^{tree}          → 9120508db6472513ef203c6aa471cbd1709610aa
$ git diff --stat origin/main HEAD   → (empty)
```

**Nateeja:** `origin/main` (58bcb4c) date ke hisaab se newest hai (13 second aage), lekin uska **tree hash bilkul identical** hai working branch ke saath. Matlab content 100% same hai — inspect karne ke liye koi farq nahi padta. Full history bhi fetch kar liya (7 commits: `22c3899` → `58bcb4c`).

## 0.1 Kya actually run kiya (verification, guess nahi)

| Check | Command | Result |
|---|---|---|
| Full test suite | `python -m pytest -q --continue-on-collection-errors` | **248 passed, 2 failed, 6 errors** |
| App import | `python -c "import app.main"` | **OK**, 69 routes, `/ws` present |
| Broker connect | `PyQuotexProvider(...).connect()` | **`ModuleNotFoundError: No module named 'pyquotex'`** at `market.py:856` |
| Indicator parity | custom parity script vs repo's own `_enrich` / `IndicatorCache` | 4 columns mismatch (details §E) |
| Leaked secrets | `git ls-files backend/data` | **28 files committed**, 3 of them `pyquotex_sessions/session.json` |

---

## 1. CORE PATH DIAGRAM

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ BROKER  (Quotex WS via vendor/pyquotex)                                 │
 │   ❌ BROKEN  — vendor/pyquotex/ directory EMPTY in the repo             │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ MARKET DATA   PyQuotexProvider.get_candles / _tick_watcher              │
 │   ⚠️ PARTIAL — logic sound & heavily hardened, but UNREACHABLE (F1)     │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ CANDLE   cache["asset:period"] → Candle → candles_to_df                 │
 │   ✅ WORKING — OHLC/dedup/sort/alignment all verified correct           │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ INDICATORS   IndicatorCache.get_enriched → enriched DataFrame           │
 │   ⚠️ PARTIAL — EMA/RSI/MACD/ATR/ADX/BB/Stoch/StochRSI/SuperTrend/PSAR   │
 │      match the vectorized path bit-for-bit; vol_* + price_strength      │
 │      + vol_ratio DO NOT (F5, F6)                                        │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ STRATEGIES   strategies.evaluate() → votes → confluence → confidence    │
 │   ✅ WORKING — no look-ahead, no forming-candle contamination           │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ DECISION   regime → calibrate → threshold → precision → MTF → payout    │
 │   ✅ WORKING — no gate blocks everything at default settings            │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ RISK   RiskManager.can_trade + stake_for                                │
 │   ✅ WORKING — limits correct; ⚠️ fed by wrong win/loss data (F7)       │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ EXECUTION   TradeEngine (single consumer) → place_order                 │
 │   ⚠️ PARTIAL — queue/dedup/unknown-outcome guard are excellent;         │
 │      ❌ account routing lost on restart (F4)                            │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ ORDER RESULT   _result_loop → check_result → TradeRecord                │
 │   ❌ BROKEN — win/loss/draw misclassified, timeout = fake LOSS,         │
 │      open trades deleted on restart (F7, F8)                            │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ LEARNING / ANALYTICS                                                    │
 │   ⚠️ PARTIAL — all wired & running, but trained on a corrupted ledger   │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## A. CORE TRADING PATH STATUS (stage-by-stage)

| # | Stage | File / function | Status |
|---|---|---|---|
| 1 | FastAPI startup + lifespan | `app/main.py:76-116 lifespan()` | ✅ Working |
| 2 | DB init (Alembic, off-loop, 360s bound) | `app/main.py:82`, `app/db.py init_db()` | ✅ Working |
| 3 | Session restore w/ 3× backoff | `app/main.py:44-73 _restore_session_safely()` | ✅ Working |
| 4 | Orchestrator per-user isolation | `app/orchestrator_registry.py`, `orchestrator.py:194` | ✅ Working |
| 5 | 5 supervised background loops | `orchestrator.py:544-556` + `engine/task_supervisor.py` | ✅ Working |
| 6 | WS hub (per-user, bounded broadcast) | `app/ws_hub.py`, `main.py:1570-1636` | ✅ Working |
| 7 | **Broker connect (pyquotex)** | `services/market.py:851 connect()` | ❌ **Broken (F1)** |
| 8 | Session restore / OTP / curl_cffi fallback | `market.py:697-968` | ❓ Cannot verify (blocked by F1) |
| 9 | Reconnect w/ jittered backoff | `orchestrator.py:883 _reconnect_with_backoff()` | ✅ Working |
| 10 | Balance retrieval | `market.py:1186 get_balance()` | ⚠️ currency hardcoded `"USD"` |
| 11 | Demo/Live/Tournament routing | `market.py:679`, `orchestrator.py:2502` | ❌ **Broken on restart (F4)** |
| 12 | Tick stream + cursor + stall detect | `market.py:987-1111` | ✅ Working (logic verified) |
| 13 | History seed + plateau + resync | `market.py:1471-1755` | ⚠️ gate sits exactly on broker ceiling (F11) |
| 14 | Candle model + cache + dedup | `market.py:1717-1753`, `indicators.py:205-270` | ✅ Working |
| 15 | Timeframe aggregation (tick→bucket) | `market.py:1699 bucket = int(ts // period * period)` | ✅ Working |
| 16 | Asset/TF isolation | `_candle_cache["asset:period"]`, `_stream_tick_ts[(asset,period)]` | ✅ Working |
| 17 | Indicator cache (incremental) | `engine/incremental.py:325 get_enriched()` | ⚠️ Partial (F5, F6) |
| 18 | Strategy votes + confluence | `engine/strategies.py:581 evaluate()` | ✅ Working |
| 19 | Confidence + calibration + threshold | `orchestrator.py:2933-2951` | ✅ Working |
| 20 | Precision gate / MTF / payout recheck | `engine/precision_gate.py`, `strategies.py:903-960`, `orchestrator.py:3480-3505` | ✅ Working |
| 21 | Risk gate + stake sizing | `engine/risk.py:74 can_trade`, `:107 stake_for` | ✅ Working |
| 22 | Execution queue (dedup + unknown-outcome) | `engine/trade_engine.py:245-383` | ✅ Working |
| 23 | Order placement (no auto-retry) | `orchestrator.py:3545-3576` | ✅ Working |
| 24 | **Settlement (win/loss)** | `market.py:1825 check_result`, `orchestrator.py:3750 _result_loop` | ❌ **Broken (F7)** |
| 25 | **Restart recovery of open trades** | `orchestrator.py:518 _reconcile_open_trades` | ❌ **Broken (F8)** |
| 26 | Learning/analytics ingestion | `orchestrator.py:3763-3870` | ⚠️ Wired, fed corrupted data |

---

## B. BLOCKERS THAT PREVENT REAL TRADING

### 🔴 F1 — `vendor/pyquotex/` is EMPTY in the repository → no broker connectivity at all

* **Exact file:** `backend/app/services/market.py`
* **Exact function:** `PyQuotexProvider.connect()`, lines **851-856**
* **Exact reason:**
  ```python
  vendor = Path(__file__).resolve().parents[3] / "vendor" / "pyquotex"
  if str(vendor) not in sys.path:
      sys.path.insert(0, str(vendor))
  from pyquotex.stable_api import Quotex        # ← market.py:856
  ```
  `/home/user/Quotex/vendor/pyquotex/` **exists but contains 0 files**. Git never tracked it (git cannot track empty directories). The only copy of the library in the repo is `vendor/old-pyquotex/pyquotex/` — and **nothing anywhere adds `vendor/old-pyquotex` to `sys.path`** (verified: `grep -rn "old-pyquotex"` returns only one hit, in `RCA_BINARY_CORRECTNESS.md`).
* **Evidence (executed):**
  ```
  vendor path resolves to: /home/user/Quotex/vendor/pyquotex
  exists: True | is dir: True | contents: []
  connect() RAISED: ModuleNotFoundError -> No module named 'pyquotex'
    failing frame: backend/app/services/market.py:856 in connect
    source line  : from pyquotex.stable_api import Quotex
  ```
  Same cause for **6 collection errors + 2 test failures** in the suite (`test_pipeline_progress.py`, `test_raw_candles.py`, `test_transport_bounds.py`, `test_histrical.py`, `test_one.py`, `test_market.py`, and `test_instrument_feed.py::test_vendor_*`).
* **Impact:** **Blocks 100% of real trading.** `build_provider("pyquotex", creds)` does return a `PyQuotexProvider` (verified), but the very first line of `connect()` raises. `_connect_provider` (`orchestrator.py:757`) catches it, sets `ok=False`, broadcasts `"Connect failed: No module named 'pyquotex'"`, and the watchdog then loops `_reconnect_with_backoff` forever producing the same error. Zero candles, zero signals, zero orders — while the process itself looks perfectly healthy.
* **Blocks actual trading:** **YES — absolutely.**

### 🔴 F2 — Docker deployment structurally cannot reach `vendor/` even if F1 is fixed

* **Exact files:** `docker-compose.yml` (`backend.build.context: ./backend`), `backend/Dockerfile` (`COPY . .`)
* **Exact reason:** the build context is `backend/`, so `vendor/` (repo root, one level up) is never copied into the image. Separately, inside the image `__file__ = /app/app/services/market.py`, so `parents[3]` = `/` → the computed path becomes **`/vendor/pyquotex`**, which will never exist.
* **Evidence:** `backend/Dockerfile` copies only the `backend/` context; `docker-compose.yml` builds with `context: ./backend`. The host path expression only resolves correctly when CWD layout is `<repo>/backend/app/services/market.py` (systemd deploy: `WorkingDirectory=/home/qat/app/backend` → `/home/qat/app/vendor/pyquotex` ✅; container → `/vendor/pyquotex` ❌).
* **Impact:** the containerized path can never run the pyquotex provider.
* **Blocks actual trading:** **YES** (for the Docker deployment specifically). The systemd/VPS deployment in `DEPLOYMENT.md §7` is *not* affected by F2 — only by F1.

---

## C. NON-BLOCKING BUGS

### 🟠 F4 — LIVE/DEMO account routing is lost on restart (money-critical, not a hard blocker only because default is demo)

* **Exact file/function:** `backend/app/orchestrator.py:2502-2582 switch_account_mode()`
* **Exact reason:** `switch_account_mode()` updates `self.runtime.trading.is_demo`, calls `self.runtime.save()`, and calls `provider.switch_account()` (which sets the *in-memory* provider's `_is_demo`) — but it **never calls `session_manager.save_credentials(..., is_demo=...)`**. Verified: the only caller of `save_credentials` in the whole app is `app/main.py:574` (`POST /api/broker/credentials`).
* **Why it bites:** on a *new* Orchestrator (process restart, or `registry.get_or_create` after `stop_all`), `__init__` → `_broker_settings()` → `session_manager.get_credentials()` → `quotex_is_demo=(row["account_type"] != "REAL")` (`session_manager.py:138`) → `market.py:679 client.set_account_mode("PRACTICE" if self._is_demo else "REAL")`. And `_connect_provider` (`orchestrator.py:776-782`) re-applies only **tournament** mode after a reconnect — never `live`.
* **Impact:** user switches Demo → Live in the UI; provider is on REAL; backend restarts; provider reconnects on **PRACTICE** while `state.is_demo=False`, the UI shows **LIVE**, and every `TradeRecord.is_demo=False`. The mirror case (DB says REAL, runtime says demo) would put real money behind a "demo" label.
* **Blocks actual trading:** No — but it can route real trades to the **wrong account**, which is worse than not trading.

### 🟠 F9 — the `stale_data_pause_seconds` watchdog is inert for the pyquotex provider

* **Exact file/function:** `backend/app/orchestrator.py:2621` (`_evaluate_asset_signal`), consumed at `orchestrator.py:862-864` (`_watchdog_loop`)
* **Exact reason:** `self._last_candle_at = now_ts()` is written on **any** successful `get_candles()` return — it records *"when I last fetched"*, not *"how old the newest candle is"*. `PyQuotexProvider.get_candles()` always returns from its in-memory `_candle_cache` and swallows its own I/O errors (`market.py:1674 except Exception: pass`, `market.py:1701 except Exception: ticks = []`). So `age = now_ts() - self._last_candle_at` stays ≈0 forever.
* **Verified:** the only two writers of `_last_candle_at` are `orchestrator.py:1495` (`_probe_data_resumed`, runs only *while* stale) and `orchestrator.py:2621`.
* **Mitigation that does exist:** `_tick_watcher` `STALL_THRESHOLD = 45.0` (`market.py:1085-1101`) flips `self._connected = False` on a frozen stream, which triggers reconnect. So a frozen feed **is** still caught — by a different mechanism than the documented one.
* **Impact:** the user-facing "Market data appears frozen → signals paused" control never fires. Non-blocking, but a documented safety control is dead code in practice.

### 🟡 F10 — "closed candle" is decided by position, not by timestamp

* **Exact file/function:** `orchestrator.py:2658` (`edf_eval = edf.iloc[:-1] if len(edf) > 1 else edf`), repeated at `1955`, `3359`, `3373`
* **Exact reason:** nothing verifies that `iloc[-1]`'s timestamp is the *current* period bucket. `get_candles` returns `sorted(cache)[-count:]`, and the forming bucket only exists once a tick has landed in it. Right after a candle boundary (or on a thin asset, or when only the 45 s resync has run — the vendor's `calculate_candles` drops the partial bar at `vendor/old-pyquotex/pyquotex/utils/processor.py:209`), `iloc[-1]` is the just-closed candle, so `iloc[:-1]` evaluates on a bar **one period older** than intended.
* **The good news (verified, not assumed):** there is **no look-ahead in the other direction**. `find_support_resistance` uses `high[start:i]` / `low[start:i]` (excludes bar *i*); `confluence_points` uses `close[max(0,i-20):i]`; `price_action_strength` uses `.expanding().max()`. So this is *staleness*, never *leakage*.
* **Impact:** occasional entries graded one candle late. Degrades quality, cannot corrupt a trade.

### 🟡 F13 — frontend doesn't consume several events the backend produces

* **Produced (`grep hub.broadcast`):** `notice, error, state, signal, trade_opened, trade_closed, otp_required, otp_cleared, latency, pipeline_snapshot, pipeline_trace_update, analytics_update, shadow_trade, shadow_result, validation_started, validation_progress, validation_completed, validation_error`
* **Handled in `frontend/src/store.ts:205-310`:** `ping, pong, state, signal, trade_opened, trade_closed, otp_required, notice, error, pipeline_snapshot, pipeline_trace_update, analytics_update`
* **Unhandled:** `latency`, `otp_cleared`, `shadow_trade`, `shadow_result`, `validation_*`
* **Actual severity — low:**
  * `otp_cleared` is harmless: `OtpModal.tsx:15` opens/closes off `state.otp_required` carried inside the `state` payload, which *is* handled.
  * `latency` is read over REST (`/api/latency`) instead.
  * `shadow_trade` / `shadow_result` / `validation_*` have **0 references anywhere in `frontend/src`** — Shadow Mode and live Validation progress have no push channel; they work only via polling (`/api/shadow/stats`, `/api/validation/status`).
* **Blocks trading:** No.

### 🟡 F14 — small correctness / hygiene items

| Item | Location | Note |
|---|---|---|
| `get_payout()` maps every TF except `"5m"` to the **1M** payout | `market.py:1755-1758` (`tf = "5" if timeframe in ("5m",) else "1"`) | Breakeven threshold for 15m/30m/1h is computed from the wrong payout |
| Currency hardcoded | `market.py:1186-1188` → `return float(bal or 0.0), "USD"` | BRL/EUR accounts display USD |
| Tie resolves to CALL | `strategies.py:844` (`if call_score >= put_score`) | Structural long bias on exact ties |
| Trade ledger rewritten whole + fsync on every event | `store.py:17,46-79` (`MAX_TRADES=1000`) | O(n) disk write per trade; fine at 1k, wasteful |
| Rate-limit "retry" never actually retries | `orchestrator.py:3604-3608` sets `sig.status="new"`, then the de-dupe at `orchestrator.py:1723-1730` blocks a new signal for that asset and nothing re-submits the old one | Signal just expires at TTL |
| Stale root/backend test files | `backend/test_market.py` imports `app.core.market` (does not exist); `test_histrical.py`, `test_one.py`, `test_raw_candles.py` need `pyquotex` | CI noise |
| Stale comments | `engine/trade_engine.py` docstring says ORDER_TIMEOUT "75s", constant is `90.0` | Ordering `65 < 90 < 105` is still correct |

---

## D. DISCONNECTED FEATURES (built, but not in the live decision path)

| Feature | Module | In live runtime path? | Evidence |
|---|---|---|---|
| Strategy Performance Manager | `engine/strategy_manager.py` | ✅ **LIVE, always on** | `active_strategies()` filters votes at `orchestrator.py:1567` & `2680`; `record_trade()` at `orchestrator.py:3780` |
| Confidence Calibrator | `engine/calibration.py` | ✅ **LIVE** (`calibration_enabled` default **True**, `config.py:623`) | `orchestrator.py:2937` |
| Regime Performance Tracker | `engine/regime_tracker.py` | ✅ **LIVE, always on** | `blended_adjustment()` at `orchestrator.py:2934` |
| Health Score Manager | `engine/health_score.py` | ✅ **LIVE, always on** | `filter_strategies()` at `orchestrator.py:2629` |
| Latency Tracker (threshold widening) | `engine/latency.py` | ✅ **LIVE** (`latency_aware_threshold` default True) | `_effective_confidence_threshold()` `orchestrator.py:2052-2057` |
| Production Metrics / Pipeline Tracer / Queue Persistence / Error Recovery | respective modules | ✅ **LIVE, always on** | `orchestrator.py:3028`, `3012`, `3533`, `2604` |
| Validation Service | `services/validation_service.py` | ✅ **LIVE** | `start()` at `orchestrator.py:557` |
| **Precision Gate** | `engine/precision_gate.py` | ❌ **OFF by default** | `precision_mode_enabled=False` (`config.py:322`) |
| **Decision Intelligence** | `engine/decision_engine.py` | ❌ **OFF by default** | `decision_engine_enabled=False` (`config.py:415`) — instrumentation only |
| **Rejection Reconciliation** | `engine/rejection_reconciliation.py` | ❌ **DEAD** (loop runs but `continue`s) | Needs `decision_engine_enabled`; `orchestrator.py:3160` |
| **Shadow Mode** | `engine/shadow_mode.py` | ❌ **OFF by default** | `shadow_mode_enabled=False` (`config.py:330`) |
| **Drift Detection** | `engine/drift_detection.py` | ❌ **OFF by default** | `drift_detection_enabled=False` (`config.py:437`) |
| **Confidence Model** | `engine/confidence_model.py` | ❌ **OFF by default** | `confidence_model_enabled=False` (`config.py:458`) |
| **Adaptive / Market Context** | `engine/market_context.py` | ❌ **OFF by default** | `adaptive_mode_enabled=False` (`config.py:393`) |
| **Regime Transition Detector** | `engine/regime_transition_detector.py` | ❌ **OFF by default** | `regime_transition_detector_enabled=False` (`config.py:497`) |
| **Dynamic Weighted Ensemble** | `strategies.py` `strategy_weights` | ❌ **OFF by default** | `dynamic_ensemble_enabled=False` (`config.py:326`) |
| **Pattern Risk Engine** | `engine/pattern_risk_engine.py` | ⚠️ **Scored & stored AFTER execution, never gates** | `orchestrator.py:3671-3731`; enforcement `pattern_risk_enforcement_enabled=False` (`config.py:649`). Its own audit note at `orchestrator.py:3658-3670` states this explicitly. |
| **Recovery Engine** | `engine/recovery_engine.py` | ⚠️ **Scored + broadcast only** | `assess()` at `orchestrator.py:3855`; auto-resume off (`config.py:653`) |
| **Analytics Engine** | `engine/analytics_engine.py` | ⚠️ **REST only** | Only call site outside construction is `main.py:1556` (`/api/insights`) |
| **Backtest** | `engine/backtest.py` | ⚠️ **REST only** | `main.py:964` |
| **Settings Advisor** | `engine/settings_advisor.py` | ⚠️ **REST only** | `main.py:1255` |

**Bottom line:** everything the *default* pipeline actually needs is wired. The advanced layers are opt-in and inert — that's a deliberate design choice, not a bug. But Pattern Risk and Recovery are the two that are *computed on every trade and then thrown away*, which is wasted work with zero decision value until they're wired in.

---

## E. DATA / MATHEMATICAL RISKS

### Verified-correct (I diffed live-incremental against the vectorized path, bar for bar)

Ran the repo's own `_enrich()` vs `IndicatorCache.get_enriched()` over the same 240 synthetic candles. **Bit-identical** (abs diff 0.0) for:
`ema5, ema9, ema21, ema50, rsi, macd, macd_sig, macd_hist, bb_mid, bb_up, bb_low, bb_width, stoch_k, stoch_d, srsi_k, srsi_d, atr, st_dir, psar_dir, support, resistance, support_strength, resistance_strength, confluence, time_vol_mult`.
`adx` differed by **2.19e-06** (floating-point accumulation over 240 bars — not a bug).

**No look-ahead bias.** **No future-candle leakage.** **No forming-candle contamination** — `edf.iloc[:-1]` is applied at every evaluation site, and `SymbolState.step(..., commit=False)` never writes smoothed state for the live bar (`incremental.py:115-121`).

### 🟠 F5 — the `_volume_source` tag is DROPPED at the incremental boundary

* **Exact file/function:** `backend/app/engine/incremental.py:259-295 _add_derived_columns()` (called from `get_enriched()` at `:379` and `:405`)
* **Exact reason:** the enriched frame is assembled from `SymbolState.step()` output + OHLC/volume — it never carries the `_volume_source` column that `candles_to_df()` preserves. `indicators._has_real_volume()` (`indicators.py:384-425`) honours the tag first and otherwise **falls back to the variance+sum heuristic**, which tick-count volume passes.
* **Evidence (executed):**
  ```
  columns present in vectorized frame:   _volume_source = True
  columns present in incremental frame:  _volume_source = False
                    vectorized     incremental       diff
  vol_ma               1.000000       31.550000   30.55000000   ** MISMATCH **
  vol_strength         1.000000        0.950872    0.04912837   ** MISMATCH **
  price_strength       0.421137        0.398749    0.02238842   ** MISMATCH **
  ```
* **Impact:** on the **live** path, pyquotex's synthetic tick-count proxy is treated as **real exchange volume** — exactly what PR #3 (`12de380 Volume correctness: tag pyquotex tick-count volume as synthetic…`) set out to prevent. That fix works on the backtest/vectorized path and is silently undone on the path that actually trades.
  * **Default 7 strategies** (`trend_pullback, ema_ribbon, supertrend, adx_di, mean_reversion, stoch_rsi, macd_cross`) do **not** read `vol_strength`/`price_strength`, so **no default-config impact today**.
  * Affected the moment they're enabled: `strat_confluence_breakout` (`strategies.py:367,371`), `strat_session_volatility_swing` (`:456,475`), `strat_multi_timeframe_confluence` (`:342`), `strat_volatility_normalized_mean_reversion`, plus `market_context.snapshot()` → `volume_quality` (`market_context.py:565,629`).
* **Blocks trading:** No.

### 🟡 F6 — `vol_ratio` is identically `1.0` in the live path

* **Exact file/function:** `backend/app/engine/incremental.py:38` (`TAIL_ROWS = 60`) + `backend/app/engine/indicators.py:277-292 volatility_ratio()`
* **Exact reason:** the warm incremental frame is only **61 rows** (`tail(60) + 1 live`). `volatility_ratio()` does `atr_pct.rolling(50).mean()`; after the 19-row SMA warm-up only ~42 valid values remain, so the rolling-50 mean is **all NaN** → `.fillna(1.0)`.
* **Evidence (executed):**
  ```
  cold frame rows: 240  |  warm frame rows: 61  |  TAIL_ROWS: 60
  warm-frame:  vol_ratio  non-nan=61/61  iloc[-1]=1.0        ← dead
  full frame:  vol_ratio  non-nan=240/240 last=1.021957158284751
  ```
* **Impact:** `vol_ratio` is a constant in live trading. Consumers: `strategies.py:331`, `regime_transition_detector.py:308-309`, and `orchestrator.py:2849` (decision engine). All opt-in. Live/backtest divergence.
* **Blocks trading:** No.

### 🟠 F7 — settlement (WIN / LOSS / DRAW) is misclassified

* **Exact file/function:** `backend/app/services/market.py:1825-1835 check_result()` + `backend/app/orchestrator.py:3750-3755 _result_loop()`; vendor `pyquotex/_api/trading.py:194-228 check_win()`, `pyquotex/api.py:643,718,762`
* **Three distinct defects:**

  **(a) A timeout is booked as a LOSS with zero profit.**
  Vendor `check_win()` on a 300 s wait: `except asyncio.TimeoutError: return "loss", 0.0`. The app then does:
  ```python
  w = str(win).lower()                     # "loss"
  status = "win" if ... else "loss" if "loss" in w else "draw"
  return {"status": status, "profit": float(profit or 0), ...}   # loss, 0.0
  ```
  → a trade that may well have **won** is recorded as a loss with **$0** P&L.

  **(b) A tie/refund is booked as a LOSS — the `"draw"` branch is dead code.**
  The vendor decides `win = "win" if profit > 0 else "loss"` (`api.py:643,718,762`). A refunded binary option has `profit == 0` → the vendor returns the **string `"loss"`**. So the app's `else "draw"` is unreachable, and `RiskManager.record_result()` (`risk.py:269-273`) increments `losses` **and `consecutive_losses`** and stamps `last_loss_ts`. With `max_consecutive_losses = 3` (`config.py:144`), three breakeven refunds in a row **auto-pause trading**.

  **(c) `_result_loop` has no timeout and no concurrency.**
  `result = await self.provider.check_result(oid)` is a **bare await inside a sequential `for` loop** over `active_trades`. One unresolved trade stalls the loop for up to 300 s; with `max_concurrent_trades = 3` (`config.py:146`) a full pass can take ~900 s before any trade behind it is even checked.
* **Impact:** corrupted win/loss ledger → poisons `StrategyPerformanceManager`, `ConfidenceCalibrator`, `RegimePerformanceTracker`, drift detection, Pattern Risk validation and Analytics. Balance itself self-heals (re-read from the broker in `_scan_loop`), so **money is not lost — the statistics are wrong**, and wrong statistics drive `consecutive_losses`, cooldowns, strategy muting and calibration.
* **Blocks trading:** Not directly, but (b) can pause a healthy bot.

### 🟠 F8 — open trades are DELETED on restart and never reconciled

* **Exact file/function:** `backend/app/orchestrator.py:518-524 _reconcile_open_trades()`, called from `start()` at `:528`
  ```python
  stale = [t.id for t in self.store.all() if t.status in (TradeStatus.OPEN, TradeStatus.PENDING)]
  if stale:
      self.store.remove_many(stale)
  ```
* **Exact reason:** `self.active_trades` is **in-memory only** — never persisted. So after a restart there is no order-id left to poll `check_result()` with, and the trade record is simply erased from the ledger.
* **Impact:** the broker's real outcome for any position open at restart is never recorded. Every learning module is trained on a ledger with the restart window surgically removed — a systematic **survivorship bias** (whichever trades happened to be open disappear, win or lose). Money is safe (balance is re-read from the broker).
* **Blocks trading:** No. Corrupts learning: yes.

### 🟡 F11 / F12 — the warm-up gate sits exactly on the broker's history ceiling

* **F11 — `min_candles_for_signal` == broker ceiling.**
  `config.py:282 min_candles_for_signal = 120`; the settings loader force-clamps anything higher back to 120 with the comment *"which no asset can reach with this broker's history depth"* (`config.py:701-712`). `market_init._init_one` requires `warm` at `max(55, 120) = 120` bars to reach `READY` (`market_init.py:530-546`), and `_candidate_assets` filters to `READY` only (`orchestrator.py:1247`).
  **If the broker delivers 119 bars, every asset stays non-READY forever** → `SCAN_CYCLE_NO_CANDIDATES` every cycle → **zero signals**, while the scanner reports healthy progress. The `SEED_PLATEAU` mechanism (`market.py:1543-1560`) stops the re-walk spin but does not lower the gate.
* **F12 — any history gap fails validation outright.**
  `market_init.py:125-141 _validate_candles()` rejects `delta <= 0` **or** `delta > interval * gap_multiplier (1.5)`. The vendor's `get_historical_candles` explicitly leaves failed chunks absent (*"never padded, never duplicated, never faked"* — `history.py` docstring). One dropped chunk in ~120 bars ⇒ 3 total attempts (`max_retries = 3`, `config.py:661`) ⇒ `FAILED`, retried by `_recovery_loop` every 60 s.
* **Impact:** both can produce a **permanent zero-signal state that looks completely healthy** in the dashboard. Neither corrupts or duplicates a trade.
* **Blocks trading:** Can block *all* signals (config-dependent), but not by crashing anything.

---

## F. EXECUTION / RISK RISKS

### ✅ What is genuinely well-built here (verified, not taken on trust)

| Concern | Verdict | Evidence |
|---|---|---|
| **Duplicate orders** | **Effectively prevented** | Three independent layers: (1) `TradeEngine` single-consumer queue + `_inflight` dedup (`trade_engine.py:216-243`); (2) asset-level de-dupe in `_scan_once` (`orchestrator.py:1723-1730`); (3) `max_trades_per_asset` in `_do_execute_signal_impl` (`orchestrator.py:3428-3459`). |
| **Auto-retry of a placed order** | **Correctly forbidden** | `orchestrator.py:3545-3576` — one attempt, `asyncio.wait_for(..., 65s)`, with an explicit *"Order placement is not idempotent and must never be retried"* comment. `OrderNotSent` (`market.py:104-113`) distinguishes "provably not transmitted" from "unknown". |
| **Unknown outcome** | **Handled correctly** | `_execute_one` never cancels the in-flight task (`trade_engine.py:329-364`), marks it `unreconciled`, **blocks new executions**, and `_late_order_finished` books it if it returns late. `block_trading_on_unknown_order` default **True** (`config.py:172`). |
| **Timeout ladder** | **Correct ordering** | `ORDER_PLACE_TIMEOUT_SECONDS = 65` < `ORDER_TIMEOUT_SECONDS = 90` < `SUBMIT_WAIT_SECONDS = 105`. (Docstring still says 75 — stale comment only.) |
| **Stake sizing** | **Hard-capped** | `risk.py:150-186` — unconditional `max_stake_pct_of_balance = 25%` cap; returns `0.0` (refuse) rather than forcing $1 when even $1 breaches the cap. |
| **Daily loss / consecutive losses / cooldown / max concurrent / max per day** | **All enforced** | `risk.py:74-105 can_trade()`. |
| **Drawdown breaker** | **Enforced, default ON** | `risk.py:63-72`, `drawdown_breaker_enabled=True` (`config.py:198`). |
| **Kill switch / resume** | **Present** | `orchestrator.py:3958-3968`, `main.py:1098/1105`. `resume()` correctly resets peak equity to current (`risk.py:250-259`). |
| **Correlation guard** | **On by default** | `orchestrator.py:3465-3474`, `correlation_guard_enabled=True` (`config.py:202`). |
| **Loop supervision** | **Solid** | `TaskSupervisor` on all 5 loops + a real-progress watchdog (`orchestrator.py:851-856` → `_recover_stalled_scan`). |

### ⚠️ Remaining execution/risk exposures

1. **F4 (wrong account on restart)** — the single most dangerous execution issue after F1.
2. **F7 (settlement)** — see §E. Feeds wrong data into every risk counter.
3. **F8 (open trades erased)** — orphaned broker positions are never tracked after a restart.
4. **Memory/CPU:** `TradeStore._persist()` rewrites the whole JSON ledger with `fsync` on **every** `add`/`update` (`store.py:46-79`); `_candle_cache` is LRU-bounded at 120 keys × 800 candles (`market.py:963-985`); `IndicatorCache` tail is bounded at 60 rows. No unbounded growth found. `_cleanup_stale_signals` (`orchestrator.py:1375-1389`) only purges **terminal** statuses, so a signal stuck in `"new"` is never reclaimed — very slow leak, low risk.
5. **Single-worker requirement:** documented at `DEPLOYMENT.md:5` and enforced by design (in-memory `Orchestrator` + `WSHub` registries). `docker-compose.yml` runs one `backend` service. ✅
6. **Persistent session files:** `ReadWritePaths` in the systemd unit covers `backend/data` and `backend/sessions`. ✅ — but see F3.

### 🔴 F3 — SECURITY: real broker session tokens are committed to the public repo

* **Exact files (committed — `git ls-files backend/data` returns 28 files):**
  * `backend/data/users/1bc8ff81e17a4671a82d4feb64ce6749/pyquotex_sessions/session.json`
  * `backend/data/users/b28b7360c3ff4bc2911fb03d5ef5b563/pyquotex_sessions/session.json`
  * `backend/data/users/c072101029fb46bf9af00102993c0329/pyquotex_sessions/session.json`
* **Contents (verified by parsing the JSON):** 40-character `token` (SSID) **plus** full Laravel session cookies (`laravel_session` and `remember_web_*`, values redacted here) for four real addresses: `deepaksahu14504@gm…`, `shimpipravin1999@gm…`, `deepaksahju14504@gm…`, `deepaksah.strad@gm…`
* **Why it happened:** `.gitignore` contains `sessions/` but **not** `pyquotex_sessions/`, and `backend/data/` is not ignored at all.
* **Impact:** **anyone with read access to this repository can authenticate to those Quotex accounts without a password.** This is independent of every trading bug above and is the most urgent item in this report.
* **Blocks trading:** No — but it can empty the account.

---

## G. EXACT PRIORITY ORDER FOR FIXING

Ranked strictly by *"can it prevent, corrupt, duplicate, or incorrectly settle a real trade"*.

| P | Issue | Category | Why this rank |
|---|---|---|---|
| **P0** | **F3 — leaked SSID + cookies in git** | Account safety | Not a code bug, but it can drain the account faster than any trading defect. Revoke the sessions at Quotex, purge git history, add `pyquotex_sessions/` and `backend/data/` to `.gitignore`. |
| **P1** | **F1 — `vendor/pyquotex/` empty** | **Prevents all trading** | Nothing else matters until the broker library is actually present and importable. Also fix the 8 tests that fail for this reason. |
| **P2** | **F2 — Docker build context / `parents[3]` path** | **Prevents all trading (container)** | Fix together with P1, or the container path stays broken after P1 is fixed. |
| **P3** | **F4 — LIVE/DEMO lost on restart** | **Can route real money to the wrong account** | Silent, money-relevant, and invisible in the UI. Persist `account_type` in `switch_account_mode` and re-apply `live` in `_connect_provider`. |
| **P4** | **F7 — settlement misclassification** | **Incorrectly settles a real trade** | A win can be booked as a loss (timeout), a refund as a loss (dead `draw` branch → false auto-pause), and `_result_loop` can stall ~300 s per open trade. |
| **P5** | **F8 — open trades erased on restart** | **Corrupts the ledger** | Persist `active_trades` (order ids) and reconcile on boot instead of deleting. |
| **P6** | **F11 + F12 — warm-up gate on the broker ceiling** | **Can produce zero signals** | Decouple `min_candles_for_signal` from the broker's actual delivery and tolerate small history gaps. |
| **P7** | **F5 — `_volume_source` dropped in the incremental path** | **Mathematical corruption** | Silently re-enables synthetic-volume-as-real on the live path and undoes PR #3. Cheap fix, latent today, live the moment any of the 4 affected strategies is enabled. |
| **P8** | **F6 — `vol_ratio` constant 1.0 live** | **Mathematical corruption** | Same root cause family (TAIL_ROWS truncation). |
| **P9** | **F10 — positional "closed candle"** | **Degrades entry timing** | Verify `iloc[-1]` against the current period bucket. |
| **P10** | **F9 — inert stale-data watchdog** | **Dead safety control** | Derive staleness from the newest candle's timestamp, not from `now_ts()`. |
| **P11** | **F13 — unhandled WS events** | **UX / contract** | Shadow + Validation progress are invisible in the UI. |
| **P12** | **F14 — misc** | **Hygiene** | Payout-TF mapping, hardcoded USD, CALL-on-tie, dead tests, stale comments. |

---

## Summary — one line

**The architecture is sound and unusually well-hardened** (single-consumer execution queue, no order retries, unknown-outcome blocking, hard stake cap, per-user isolation, supervised loops). **But it cannot trade at all right now**, because the vendored broker library is not in the repository (`vendor/pyquotex/` is an empty directory), and the one thing that *would* misdirect real money once that's fixed is the demo/live account mode not surviving a restart. Behind those two sit a genuinely incorrect settlement path (win/loss/draw + timeout) and an indicator layer whose volume tagging is silently lost between the vectorized and incremental implementations.
