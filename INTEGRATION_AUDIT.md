# Production Integration Audit — fixed-v3 vs. Merged Output

Your diagnosis was correct, and worse than you described. This is a **runtime
trace audit**, not a code review: every finding below was verified by (1)
confirming the class/method exists, (2) confirming it's instantiated, (3)
grepping every call site in the actual execution path, (4) checking whether
a websocket broadcast actually fires, (5) checking whether the frontend
store has a handler for it, (6) checking whether a REST endpoint backing it
actually works. No finding here is "the class exists so it probably works."

No fixes were made. This is audit only, per your request.

---

## Headline finding

**The regime-transition-detector merge (Step 3 of the previous work)
silently gutted the pipeline observability system while integrating the
new regime detector.** Root cause: `__init__` had two independently-evolved
regions — one added `PipelineEventBus`/`on_stage` wiring (pipeline
dashboard lineage), the other added the new regime engine construction
(regime-detector lineage) — and merging them replaced the whole region with
the regime-detector lineage's version, which never had the pipeline
dashboard wiring to begin with. This is exactly what you suspected: **class
definitions were preserved, runtime wiring was not.**

There is also one bug this pattern introduced that's worse than "silent" —
a REST endpoint that now **hard-crashes (500)** on every call. Details in
§1.3.

---

## 1. Pipeline Dashboard / Pipeline Snapshot / PipelineTracer

### 1.1 `_pipeline_snapshot()` — Present but Never Used ❌

- **Class/method present:** ✅ `Orchestrator._pipeline_snapshot()`,
  `backend/app/orchestrator.py:1308`
- **Called from execution path:** ❌ **Zero call sites.** `fixed-v3` calls
  it **13 times** throughout `_evaluate_asset_signal` (cooldown rejection,
  "running" marker, candle-fetch failure, no-active-strategies rejection,
  indicators-not-warm rejection, the main candle/indicator snapshot, regime
  rejection, no-direction rejection, confidence-below-threshold rejection,
  precision-gate rejection, "signal generated" marker). The merged file has
  **zero.**
- **Missing runtime wiring — exact locations to restore**, all in
  `backend/app/orchestrator.py`, method `_evaluate_asset_signal` (starts
  line 1524):
  - After the `precision_mode_enabled` cooldown check (~line 1533):
    `stage="rejected", stage_reason=f"cooldown: {cooldown_reason}"`
  - Right after, before the candle fetch (~line 1535): `stage="running",
    ws_connected=self.state.connected`
  - In the `except` around `get_candles` (~line 1546): `stage="failed",
    stage_reason=f"candle_fetch: {exc}"`
  - After `active_strategies` returns empty (~line 1559): `stage="rejected",
    stage_reason="no active strategies (health/manual muted)"`
  - After the indicator-warm check fails (~line 1563): `stage="rejected",
    stage_reason="indicators not warm (insufficient history)"`
  - Right after `edf_eval` is computed (~line 1565): the main snapshot with
    OHLC + `indicators=self._indicator_snapshot(row_last)` — this is the
    one that feeds the dashboard's live candle/indicator view specifically
  - After the regime-based strategy exclusion empties the list (~line
    1581): `stage="rejected", stage_reason=f"no strategies active for
    regime={current_regime}"`
  - After `if not res.direction:` (~line 1710): `stage="rejected",
    stage_reason="no direction consensus"`
  - After the confidence-vs-threshold rejection (~line 1717): `stage=
    "rejected", stage_reason=f"confidence {calibrated_confidence} <
    threshold {effective_threshold}"`
  - After a precision-gate rejection (~line 1748): `stage="rejected",
    stage_reason=pg.reason`
  - Right after `start_trace()` at signal generation (~line 1816):
    `stage="passed", stage_reason="signal generated"`
- **Missing event broadcast:** ❌ Would broadcast `pipeline_snapshot` (a
  literal string that appears in the code — the mechanism is real, it's
  just never triggered).
- **Missing frontend update:** the frontend is correctly built and waiting
  — `frontend/src/store.ts:258`, `case "pipeline_snapshot":` sets
  `pipelineSnapshots` in the store. This code is fine; it just never
  receives anything.

### 1.2 `self._pipeline_events` — a latent crash bug, worse than "unused"

Even if something called `_pipeline_snapshot()` right now, it would
**crash**: line 1313 reads `self._pipeline_events.update_snapshot(...)`,
but `self._pipeline_events` is **never constructed anywhere.**
`fixed-v3`'s `__init__` has `self._pipeline_events = PipelineEventBus()`
(around line 165 there); the merged `__init__` (`backend/app/
orchestrator.py`, starts line 119) has no such line, and the import itself
— `from .engine.pipeline_events import PipelineEventBus` — is also gone
from the top of the file.
- **Missing initialization:** `self._pipeline_events = PipelineEventBus()`
  needs to go back into `Orchestrator.__init__`, alongside the other
  per-user store constructions (near where `self.pipeline_tracer` is set,
  currently line 197).
- **Missing import:** `from .engine.pipeline_events import
  PipelineEventBus` needs to go back near the other `.engine.*` imports at
  the top of `orchestrator.py`.

### 1.3 `/api/pipeline/snapshots` — hard crash, not just empty data ❌

`backend/app/main.py:847-853`:
```python
@app.get("/api/pipeline/snapshots")
async def pipeline_snapshots(o: Orchestrator = Depends(get_orch)):
    return o._pipeline_events.all_snapshots()
```
This calls `o._pipeline_events`, which (§1.2) does not exist. **Every call
to this endpoint raises `AttributeError` → 500.** The frontend calls this
once on mount (`store.ts:150`) wrapped in `.catch(() => {})`, so the error
is swallowed silently and the dashboard just looks empty — no error visible
to the user, no error in the browser console beyond a failed network
request. This is the most concrete, user-visible manifestation of "the
Pipeline Dashboard remains empty."

### 1.4 `PipelineTracer` — constructed without its broadcast callback ❌

- **Instantiated:** ✅ `backend/app/orchestrator.py:197`:
  `self.pipeline_tracer = PipelineTracer(max_traces=500)`
- **fixed-v3's version:** `PipelineTracer(max_traces=500,
  on_stage=self._on_trace_stage)` — the `on_stage=` callback is missing
  entirely.
- **Effect:** `_on_trace_stage()` (defined correctly and completely at
  `orchestrator.py:1296` — this method itself is fine) is **never called**,
  because `PipelineTracer` has no callback to invoke it with. Every
  `start_trace()`/`mark_stage()`/`end_trace()` call — and these ARE called,
  7 of 11 original call sites remain — updates `PipelineTracer`'s internal
  in-memory trace log, but nothing ever notifies the frontend in real time.
- **Missing event broadcast:** `pipeline_trace_update`, which
  `_on_trace_stage` would send via `hub.broadcast(self.user_id,
  "pipeline_trace_update", {...})` — code is correct, just unreachable.
  Frontend handler is correct and waiting: `store.ts:264`.
- **Fix location:** `backend/app/orchestrator.py:197`, restore
  `on_stage=self._on_trace_stage` as a constructor argument.

### 1.5 Four pipeline stages no longer marked at all — Missing After Merge ❌

| Stage | fixed-v3 call site | Merged status |
|---|---|---|
| `MARKET_CONTEXT` | after `STRATEGY_EVAL`, `data={"regime":..., "is_otc":...}` | **Gone** |
| `PRECISION_GATE` | after precision-gate passes, `data={"passed": True, "agreeing":..., "opposing":..., "reason":...}` | **Gone** |
| `QUEUE_ADD` | right after `queue_persistence.add(...)` in the execution path | **Gone** |
| `PATTERN_RISK` | right after the shadow pattern-risk assessment is recorded | **Gone** |

Only 7 of `fixed-v3`'s 11 `mark_stage()` calls remain (`STRATEGY_EVAL`,
`CONFIDENCE_SCORE`, `SIGNAL_REVALIDATE`, `ASSET_CHECK`, `RISK_CHECK`,
`EXECUTION` ×2). Even once §1.4 is fixed and traces broadcast again, any
trace a user inspects will be missing 4 of its stages.
- **Exact fix locations, `backend/app/orchestrator.py`, `_evaluate_asset_signal`**:
  restore the `MARKET_CONTEXT` and `PRECISION_GATE` `mark_stage()` calls
  around line 1810 (right after the existing `STRATEGY_EVAL`/
  `CONFIDENCE_SCORE` pair).
  **`_do_execute_signal_impl`** (line 1944): restore `QUEUE_ADD` right
  after `self.queue_persistence.add(...)` (line 2166).
  **the shadow pattern-risk block** (~line 2278, right before
  `self.store.add(trade)`): restore `PATTERN_RISK`.

### 1.6 `CONFIDENCE_SCORE` stage — data loss, not absence ⚠️

The stage still fires, but with less detail. `fixed-v3` builds a
`confidence_breakdown` dict (`base_confidence`, `regime_adjustment`,
`adjusted_confidence`, `calibrated_confidence`, `effective_threshold`, plus
a nested `regime_transition` sub-object) and passes the whole thing as
`data=confidence_breakdown`. The merged version passes only `{"raw":
adjusted_confidence, "calibrated": calibrated_confidence, "threshold":
effective_threshold}` — three of six original fields, and the regime
sub-object is gone (arguably should be rebuilt using the new
`regime_transition_result` object instead of the old
`regime_transition_assessment` one, since the underlying detector changed).
Similarly `SIGNAL_REVALIDATE`'s `data=` dropped `checks_passed` and
`warnings[:5]`, keeping only `confidence_adjustment`
(`orchestrator.py:2057`).

### Verdict: Pipeline Dashboard / Snapshot / Tracer = **Missing After Merge ❌**

---

## 2. Analytics / Production Metrics / Recovery Engine

These three are wired together in `fixed-v3` and were **severed together**
by the same dropped block.

- **`analytics_update` broadcast:** ❌ Zero occurrences anywhere in
  `backend/app/`. `fixed-v3` broadcasts it from inside the trade-close
  handler, right after `hub.broadcast(self.user_id, "trade_closed", ...)`
  — i.e., every time a trade resolves, the frontend should get a fresh
  `{overall, health, recovery}` bundle. That whole `try:` block (calling
  `self.recovery_engine.assess(...)` and broadcasting the result) is gone
  from the merged file's trade-close handler.
  - **Exact fix location:** `backend/app/orchestrator.py`, the trade
    settlement loop, immediately after `await hub.broadcast(self.user_id,
    "trade_closed", trade.model_dump())` (currently ~line 2409).
- **`recovery_engine.assess()`:** ❌ Never called anywhere in
  `orchestrator.py` — the only place it's invoked in the whole merged
  codebase is `main.py:931`, inside the `/api/recovery/assess` REST
  handler. So Recovery Engine only computes when a user manually hits that
  endpoint; it never runs proactively or pushes an update.
- **REST endpoints:** ✅ both exist and work — `/api/production/report`
  (`main.py:780`) and `/api/recovery/assess` (`main.py:923`).
- **Frontend:** ❌ **No client function calls either endpoint.** Grepped
  `frontend/src/api.ts` for `production` or `recovery` — zero matches.
  `store.ts` has a fully-correct handler for `analytics_update`
  (`store.ts:306`, sets `analytics` state), and `PipelineDashboard.tsx`
  reads `analytics.recovery.*` to render its Recovery mini-stats panel
  (`PipelineDashboard.tsx:432-449`) — but since nothing ever populates
  `analytics`, that panel is permanently blank, and there's no REST polling
  fallback the way `Strategy.tsx` or `Validation.tsx` have for their own
  data.

### Verdict: Analytics = **Missing After Merge ❌** (broadcast). Production
Metrics = **Partially Integrated ⚠️** (computed correctly, REST works,
zero frontend reachability). Recovery Engine = **Partially Integrated ⚠️**
(by original design it's shadow-only/advisory — see `orchestrator.py`'s
own "Audit note (H-2)" comment near line 2251 confirming this is
intentional — but its REST-reachability path is also currently
frontend-orphaned, and its proactive assess-and-broadcast path is gone).

---

## 3. Regime Transition Detector

- **Class present:** ✅ rewritten `RegimeTransitionDetector`/
  `RegimeTransitionEngine`, `backend/app/engine/regime_transition_detector.py`
- **Instantiated:** ✅ `self.regime_transition_engine = RegimeTransitionEngine()`,
  `orchestrator.py:172`
- **Connected to runtime:** ✅ called from `_evaluate_asset_signal`
  (~line 1621), gated behind `regime_transition_detector_enabled`
  (off by default — this gate was added deliberately during the previous
  merge specifically because this module ships with no gate of its own;
  see `MERGE_REPORT.md` §3.2 for why)
- **Called from execution path:** ✅ when enabled — confirmed via direct
  code read, not inferred
- **Broadcasting events:** ⚠️ indirectly only — it doesn't broadcast
  itself, but feeds `decision.regime_transition_*` fields and
  `composite_confidence`, which only reach the frontend if
  `decision_engine_enabled` is also on and something reads `SignalDecision`
  records (§4 — nothing currently does, on the frontend side)
- **Frontend:** ❌ no dedicated panel; would need Decision Intelligence's
  frontend gap (§4) closed first
- **One design note, not a bug:** `fixed-v3` re-checks the (old) detector a
  **second time**, right before execution, using fresh data
  (`orchestrator.py` ~line 2060 in fixed-v3, inside the comment "every
  signal must pass through the detector before execution per the approved
  spec"). The new architecture replaces this with a *cached* lookup
  (`self.regime_transition_engine.get(rt_key)._last_result`) fed into
  `signal_validator.revalidate_signal(regime_transition_result=...)`
  instead of a second live `.observe()` call. This is a legitimate design
  change (confirmed present and wired — `orchestrator.py` ~line 2028), not
  an oversight, but flagging it since the original spec comment explicitly
  said "every signal must pass through the detector" and the new approach
  reuses a potentially-stale cached result instead. Worth confirming this
  is the intended tradeoff.

### Verdict: **Partially Integrated ⚠️** — computation and gating are
correct and safe; downstream visibility (frontend) is absent because
Decision Intelligence's frontend layer (§4) doesn't exist.

---

## 4. Decision Intelligence

- **Class present:** ✅ `DecisionStore`, `build_decision`,
  `compute_composite_confidence` — `backend/app/engine/decision_engine.py`
- **Instantiated:** ✅ `orchestrator.py:159` (and correctly in both
  tournament-mode rebuild branches, `~1397`/`~1426`)
- **Called from execution path:** ✅ `build_decision(...)` at ~line 1679,
  `_persist_decision()` called at every rejection/acceptance point
- **Broadcasting events:** ❌ never — decisions are persisted to disk
  only, never pushed live. **This matches `fixed-v3` exactly — not a
  regression.** Confirmed via direct grep of both codebases.
- **REST endpoint for raw decisions:** ❌ none in either codebase. Only
  the aggregate `/api/rejections/report` exists, which summarizes
  *rejected* decisions statistically — there's no "list/inspect individual
  decisions" endpoint or UI in either version.
- **Frontend:** ❌ no consumption anywhere, in either codebase.

### Verdict: **Partially Integrated ⚠️ — by design**, not a merge
regression. It's genuinely shadow/instrumentation-only in both the base and
`fixed-v3`: decisions are computed, scored, and persisted for later
statistical analysis (via the rejection report), but there is no live feed
and never was one. If you want individual-decision visibility, that's a
net-new feature, not something to "restore."

---

## 5. Confidence Model

- **Class present:** ✅ `ConfidenceModelStore`, `backend/app/engine/confidence_model.py`
- **Instantiated:** ✅ `orchestrator.py:161`, with tunables correctly
  wired from `TradingSettings` (min_training_samples, prediction_threshold)
- **Called from execution path:** ✅ retrain loop present (~line 760,
  gated by `confidence_model_enabled` + `confidence_model_retrain_interval`);
  `predict_proba()` called at decision-build time (~line 1696) to populate
  `shadow_confidence_model_prediction`
- **Broadcasting events:** ❌ none, in either codebase
- **Frontend:** ❌ none, in either codebase — its one output field
  (`shadow_confidence_model_prediction`) lives inside `SignalDecision`,
  which (§4) has no frontend path at all

### Verdict: **Partially Integrated ⚠️ — by design**, same shadow-only
posture as Decision Intelligence, not a regression.

---

## 6. Strategy Performance Manager (Adaptive Learning's mute/trial engine)

- **Class present:** ✅ `StrategyPerformanceManager`, `backend/app/engine/strategy_manager.py`
- **Instantiated:** ✅ correctly, with all 7 tunables, in `__init__` and
  both tournament-rebuild branches
- **Called from execution path:** ✅ `record_trade()` on every trade close
  (~line 2337), `force_trial()` from the drift-detection integration
  (~line 2371), `active_strategies()`/`performance_multiplier()` feeding
  every scan cycle
- **Broadcasting events:** ❌ no push, but —
- **REST + frontend:** ✅ **fully working.** `/api/strategies/performance`
  (GET) + `/api/strategies/{name}/override` (POST), both called from
  `frontend/src/api.ts`, consumed by `Strategy.tsx`, which **polls every
  30 seconds** (`Strategy.tsx:36`).

### Verdict: **Fully Integrated ✅**

---

## 7. Market Context Engine

- **Class present:** ✅ `MarketContextEngine`, `backend/app/engine/market_context.py`
- **Instantiated:** ✅ `orchestrator.py:202`, config hot-reloaded on
  settings change (~line 552)
- **Fed live data:** ✅ `market_context.observe(...)` called 6 places
  inside `strategies.py`'s `evaluate()` (confirmed — this is the one
  system where the *feeding* mechanism lives inside the strategy
  evaluation code itself, not the orchestrator)
- **Read from:** ✅ `adaptive_threshold()` used in
  `signal_validation.py`'s revalidation path, and as an input to
  `revalidate_signal(market_context=...)`
- **Broadcasting events:** ❌ none, in either codebase — by design, it's
  an internal signal-quality input, not user-facing data on its own
- **Pipeline trace visibility:** ❌ **lost** — this is the one place
  Market Context *was* meant to be user-visible: the dropped
  `MARKET_CONTEXT` pipeline stage (§1.5) was specifically
  `data={"regime": res.regime, "is_otc": sig.is_otc}`, i.e., a per-signal
  snapshot of the market context state at decision time.

### Verdict: **Fully Integrated ✅** for its actual signal-quality function;
its one piece of user-facing visibility died with the pipeline stage in §1.5.

---

## 8. Pattern Risk Engine

- **Class present:** ✅ `PatternRiskEngine`, `backend/app/engine/pattern_risk_engine.py`
- **Instantiated:** ✅ `orchestrator.py:217`, tunable
  (`pattern_risk_min_sample_size`) wired
- **Called from execution path:** ✅ shadow-assessment on every trade
  (~line 2245), explicitly documented in-code as intentionally shadow-only
  — "Audit note (H-2, confirmed intentional — not yet approved for live
  gating)" — this is a deliberate product decision already made and
  documented by whoever built this, not a gap
- **Broadcasting events:** ❌ none, either codebase
- **REST endpoint:** ✅ `/api/pattern-risk/validation-report` (`main.py:795`)
- **Frontend:** ❌ **zero references anywhere** — no `api.ts` function, no
  component. Confirmed identical (zero) in `fixed-v3` — **pre-existing,
  not a merge regression.**
- **Pipeline trace visibility:** ❌ **lost** — the dropped `PATTERN_RISK`
  stage (§1.5) was this system's only per-signal visibility.

### Verdict: **Partially Integrated ⚠️** — correctly shadow-scoring every
trade by design, but its only two visibility paths (REST+frontend, and
pipeline trace) are respectively pre-existing-orphaned and merge-broken.

---

## 9. Recovery Engine

Covered in §2 (bundled with Analytics/Production Metrics in the actual
code). Summary: **Partially Integrated ⚠️** — shadow-only by explicit
design (same H-2 audit note as Pattern Risk Engine), REST endpoint works,
proactive assess-and-broadcast path is gone (§2), frontend has no path to
either the REST endpoint or the (currently dead) broadcast, and its
pipeline-trace stage (`RECOVERY_CHECK` — defined in the `PipelineStage`
enum itself, `pipeline_tracer.py:28`, annotated "visualization addition")
was **never wired in either codebase** — confirmed zero occurrences in
`fixed-v3` too. That one's pre-existing incompleteness, not something the
merge broke.

---

## 10. Shadow Trading

- **Class present:** ✅ `ShadowStore`, instantiated correctly (incl.
  tournament-rebuild branches)
- **Called from execution path:** ✅ trades added to shadow store
  correctly
- **Resolution loop:** ❌ **broken — silent failure, not absence.**
  `_shadow_loop()` (`orchestrator.py:1851`) fetches candles via
  `self.provider.get_candles(...)`, which returns `List[Candle]` — Pydantic
  objects (confirmed: `services/market.py`, three separate
  `get_candles` signatures all declare `-> List[Candle]`). Line 1868 does
  `exit_price = float(candles[-1]["close"])` — **dict-style subscript
  access on a Pydantic object, which doesn't support `__getitem__`.**
  `fixed-v3` correctly uses `candles[-1].close` (attribute access). This
  will raise `TypeError: 'Candle' object is not subscriptable` on every
  single resolution attempt.
  - **Why this doesn't crash the app:** it's caught by `except Exception
    as exc: logger.debug(...); continue` — logged at DEBUG level (usually
    not visible in production logs), then silently skipped. **Every shadow
    trade that comes due for resolution will fail to resolve, forever,
    with no visible error anywhere**, and just sit in `pending_due()`
    getting retried (and failing) every 3-second loop tick indefinitely.
  - **Exact fix location:** `backend/app/orchestrator.py:1868`, change
    `candles[-1]["close"]` → `candles[-1].close`.
- **Broadcasting:** `shadow_result`/`shadow_trade` events exist as string
  literals in the broadcast calls, but per above, resolution never
  succeeds, so `shadow_result` in practice never fires. Also — confirmed
  neither event has a frontend handler in `store.ts` in *either* codebase
  (pre-existing gap, separate from the resolution bug).

### Verdict: **Present but Never Used ❌** — worse than dead code, it's
live code that fails every time silently.

---

## 11. Rejection Reconciliation

Same exact bug as Shadow Trading, same fix, same root cause:
`_rejection_reconciliation_loop()` (`orchestrator.py:1883`) has the
identical `candles[-1]["close"]` on line ~1905. **Same verdict: Present
but Never Used ❌**, silently failing forever. This directly undermines
Decision Intelligence's whole stated purpose (§4) — the point of
Rejection Reconciliation is measuring what a rejected signal *would have*
done, and that measurement currently never completes.

---

## 12. Queue Persistence

Checked thoroughly, including a self-correction: I initially flagged this
as broken based on an incomplete grep, then re-verified with a complete
search. **It is intact.**
- `get_pending()` + `clear_all()` at startup recovery (`orchestrator.py:359,368`)
- `cleanup_stale()` in the periodic maintenance loop (`orchestrator.py:677`)
- `add()` on execution (`orchestrator.py:2166`)
- `remove()` on both the execution-failure path and the execution-success
  path (`orchestrator.py:2207, 2216`)

All present, matches `fixed-v3` exactly.

### Verdict: **Fully Integrated ✅**

---

## 13. Telegram

- **Class present:** ✅ `TelegramService`
- **Instantiated + wired:** ✅ command handler registered (~line 377),
  start/stop lifecycle (~line 494), live-reconfigure on settings change
  (~line 501)
- **Called from execution path:** ✅ signal notification (~line 822),
  trade-result notification (~line 2411)
- **Frontend:** ✅ configuration fields present in `Settings.tsx` (5
  references — bot token, chat ID, send-signals toggle, send-results
  toggle, and the enable switch)

### Verdict: **Fully Integrated ✅**

---

## 14. Validation / Backtesting

- **Endpoints:** ✅ `/api/backtest/run`, `/api/validation/run`,
  `/status`, `/health`, `/leaderboard`, `/runs`, `/runs/{id}`, `/history` —
  all present
- **Frontend:** ✅ every one of the above has a matching `api.ts`
  function and is consumed by `Validation.tsx`, which polls every 3s
  (running) / 15s (idle)

### Verdict: **Fully Integrated ✅**

---

## 15. WebSocket Events — full enumeration

**Backend broadcasts:** `error`, `latency`, `notice`, `otp_cleared`,
`otp_required`, `pipeline_snapshot`, `pipeline_trace_update`,
`shadow_result`, `shadow_trade`, `signal`, `state`, `trade_closed`,
`trade_opened`

**Frontend handles:** `analytics_update`, `error`, `notice`,
`otp_required`, `ping`, `pipeline_snapshot`, `pipeline_trace_update`,
`pong`, `signal`, `state`, `trade_closed`, `trade_opened`

| Event | Backend sends? | Frontend handles? | Status |
|---|---|---|---|
| `state`, `signal`, `trade_opened`, `trade_closed`, `error`, `notice`, `otp_required` | ✅ | ✅ | Fully Integrated ✅ |
| `pipeline_snapshot`, `pipeline_trace_update` | ⚠️ code exists, never triggered (§1) | ✅ | Missing After Merge ❌ |
| `analytics_update` | ❌ (§2) | ✅ | Missing After Merge ❌ |
| `latency`, `otp_cleared`, `shadow_result`, `shadow_trade` | ✅ | ❌ | Present but Never Used ❌ (pre-existing in `fixed-v3` too, not merge-caused) |

---

## Summary table

| System | Status |
|---|---|
| Pipeline Dashboard | ❌ Missing After Merge |
| Pipeline Snapshot | ❌ Missing After Merge (+ latent crash, §1.2-1.3) |
| PipelineTracer | ❌ Missing After Merge (callback dropped, 4/11 stages dropped) |
| Decision Intelligence | ⚠️ Partially Integrated (by design, unchanged from base) |
| Adaptive Learning (Strategy Performance Manager) | ✅ Fully Integrated |
| Confidence Model | ⚠️ Partially Integrated (by design, unchanged from base) |
| Strategy Performance Manager | ✅ Fully Integrated |
| Market Context Engine | ✅ Fully Integrated (signal-quality function); ❌ its pipeline-trace visibility is gone |
| Regime Transition Detector | ⚠️ Partially Integrated (correct + safely gated; no frontend path) |
| Pattern Risk Engine | ⚠️ Partially Integrated (correct shadow-scoring; both visibility paths broken/orphaned) |
| Recovery Engine | ⚠️ Partially Integrated (shadow-only by design; broadcast path lost, §2) |
| Shadow Trading | ❌ Present but Never Used (silent resolution failure, §10) |
| Queue Persistence | ✅ Fully Integrated |
| Telegram | ✅ Fully Integrated |
| Validation | ✅ Fully Integrated |
| Analytics | ❌ Missing After Merge (§2) |
| Backtesting | ✅ Fully Integrated |
| Production Metrics | ⚠️ Partially Integrated (computed correctly; zero frontend reachability) |
| WebSocket Events | ⚠️ Partially Integrated (see full table, §15) |

---

## What actually needs to happen (not doing it now, per your instruction)

In rough priority order, if/when you want it fixed:

1. **`candles[-1]["close"]` → `candles[-1].close`** in both
   `_shadow_loop` and `_rejection_reconciliation_loop`. Two-line fix,
   highest impact — this silently breaks two entire systems.
2. **Restore `self._pipeline_events = PipelineEventBus()`** and its import
   — fixes the 500 on `/api/pipeline/snapshots` immediately.
3. **Restore `on_stage=self._on_trace_stage`** on the `PipelineTracer`
   constructor — this alone reconnects `pipeline_trace_update` for the 7
   stages that still fire.
4. **Restore the 13 `_pipeline_snapshot()` calls** and the 4 missing
   `mark_stage()` calls (§1.1, §1.5) — full trace/snapshot coverage back.
5. **Restore the `analytics_update` broadcast block** in the trade-close
   handler (§2).
6. Lower priority: add frontend `api.ts` functions + polling for
   `/api/production/report` and `/api/recovery/assess`, matching the
   pattern `Strategy.tsx`/`Validation.tsx` already use — these have never
   had frontend reachability, in any version.
