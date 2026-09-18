# Multi-Source Merge Report

Base: `QuotexAutoTrader-prod-last-final.zip`. Four other zips were compared
against it file-by-file; two contributed real work, two contributed nothing
(see §1). This report covers the merge; `MIGRATION_REPORT.md` (already in
this project) covers the PostgreSQL migration in full detail and isn't
repeated here.

---

## 1. What each source actually contained (evidence, not assumption)

Before merging anything, every zip was diffed against the base and checked
for genuine new content vs. staleness — file timestamps, byte-identical
files, and function-level presence checks, not just "this file is
different so take it."

| Source | Verdict | Evidence |
|---|---|---|
| `postgres-migration.zip` | **Merged (Step 1)** | This was my own earlier output — base + a full SQLite→PostgreSQL migration. Covered in `MIGRATION_REPORT.md`. |
| `fixed-v3.zip` | **Merged (Step 2)** | Newer than base (mtimes), zero missing files, monotonically larger. Real content: a race-condition fix in pyquotex's `history/load` correlation, a new Tournament account mode, a websocket dead-connection fix. |
| `decision-intelligence-engine.zip` | **Nothing to merge** | Its entire unique contribution — `decision_engine.py`, `confidence_model.py`, `drift_detection.py`, `rejection_reconciliation.py` — is **byte-identical** to what's already in the base. Every function in its `main.py`/`orchestrator.py` already exists in the base (checked by name, zero unique). It's chronologically *older* than the base by about a day. |
| `signal-accuracy-fixes.zip` | **Nothing to merge** | Chronologically the *oldest* of everything uploaded (~2 days before base). Missing 6 whole engine files the base has wired into the orchestrator, plus the pipeline dashboard UI. Checked 7 of its flagged files in detail (`precision_gate.py`, `calibration.py`, `strategy_manager.py`, `config.py`, `strategies.py`, `signal_validation.py`, `pipeline_tracer.py`) — every one is missing functionality the base already has; none contains anything unique. Despite the name, merging it would have been a regression. |
| `regime-transition-detector.zip` | **Merged (Step 3, substantial)** | Newest backend files of anything uploaded (~5 days ahead of base) but frontend files at the *original* pre-fork timestamp (this branch never touched the frontend). Real content: a from-scratch rewrite of the regime transition detector plus integration into 7 other engine files. Full detail in §3. |

The practical effect: **only two of the five sources needed real merge
work.** Presenting the other two as "merged" would have been dishonest —
so instead of forcing a "Step 3 / Step 4" that had nothing to do, this
report says plainly that they were fully superseded already.

---

## 2. Step 2 — fixed-v3 (recap)

Already delivered and confirmed compiling in the previous turn:
- **pyquotex race-condition fix** (`vendor/pyquotex/pyquotex/api.py` +
  3 related files): a single overwritable `_temp_status` string used to
  correlate a placeholder candle frame with its data, replaced with a FIFO
  queue — fixes silent candle drops when an unrelated websocket event
  (e.g. a periodic `instruments/list` refresh) interleaved with an
  in-flight `history/load` request. A reproduction script (`repro_race.py`)
  proving the bug was real is included.
- **Tournament account mode**: live/demo/tournament switching with fully
  isolated per-scope state (trade history, decision store, drift detector,
  confidence model, pattern-risk validation, queue persistence, shadow
  store) — `config.py`, `schemas.py`, `store.py`, `services/market.py`,
  `orchestrator.py`, new `/api/control/switch-account` endpoint, frontend
  UI in `Settings.tsx`.
- **Websocket session-leak fix**: a dead/half-open connection used to sit
  registered in the hub forever after a failed heartbeat ping; now it's
  actively closed so the client reconnects.

---

## 3. Step 3 — regime-transition-detector (the substantial one)

### 3.1 What it is

A ground-up rewrite of `engine/regime_transition_detector.py`, smaller
than the version it replaces (484 vs 695 lines) despite being newer —
verified by reading both in full, not inferred from size:

- **Welford's algorithm** for running mean/variance instead of a
  sum/sum-of-squares accumulator, which is numerically unstable
  (catastrophic cancellation) over a long-running process.
- **Real multi-timeframe confirmation.** The old version's `ConfluenceWeights`
  had an `mtf_confirmation` weight hardcoded to `0.0` with a comment saying
  it was "applied as a separate multiplier" — it wasn't actually wired to
  anything. The new version's weights sum to 1.0 *including* a live `mtf`
  term fed by real `TimeframeSignal` objects.
- **Reads indicators already computed elsewhere** (`row.get("macd_hist")`,
  `row.get("st_dir")`, etc. from the enriched DataFrame row) instead of
  recomputing them — directly addresses "indicator instability / duplicate
  computation," one of the audit categories requested.
- Cleaner separation: `RegimeTransitionDetector` (one instance per
  asset+timeframe) vs. `RegimeTransitionEngine` (the multi-key manager),
  replacing one class that did both.

Every downstream file it touches was reviewed in full diff, not skimmed:

| File | Change | Assessment |
|---|---|---|
| `decision_engine.py` | New `regime_transition_confidence` field feeds an informational `composite_confidence` blend | Additive, explicitly never a gate |
| `confidence_model.py` | Feature-vector length safety check before `predict_proba()` | Real bug-prevention — previously a misaligned feature vector would silently apply weights to the wrong inputs |
| `drift_detection.py` | (1) `magnitude=peak` instead of `magnitude=self.threshold` — the old code logged the *static threshold* as every alert's magnitude, useless for severity comparison; (2) cooldown to stop alert-spam / repeated `force_trial()` calls | Both genuine fixes |
| `analytics_engine.py` | Dependency-free inverse-normal-CDF (Acklam's approximation), making the Wilson-interval confidence level configurable instead of a hardcoded z=1.96 everywhere | Standard, correct math |
| `signal_validation.py` | Optional regime-transition-stability check in revalidation, defaults to `None`/pass-through | Backward compatible |
| `strategy_manager.py` | Hardcoded module constants → instance-configurable via `TradingSettings`, defaults unchanged | Clean refactor, verified same defaults |
| `calibration.py` | Same pattern, plus exposes the Phase 6 holdout-validation/rollback toggle | Same |

### 3.2 A real safety gap I found and corrected — disclosed, not hidden

**This branch wires the regime detector into the live pipeline
unconditionally.** There is no enable flag anywhere in its `config.py` or
`orchestrator.py` — `RegimeTransitionEngine()` is constructed and called on
every signal, for every user, with no way to turn it off short of a code
change. It can also outright drop a signal (`return ..., None, ...`) during
a detected "paused" state.

Every *other* advanced feature in this codebase —
`decision_engine_enabled`, `drift_detection_enabled`,
`confidence_model_enabled`, and this exact module's own previous
implementation (whose docstring stated "OFF BY DEFAULT... with it off,
nothing in this file is ever called from the live pipeline" as a named
design commitment) — ships opt-in. Shipping a brand-new, unvalidated,
signal-suppressing module always-on, with no kill switch, on a production
multi-user trading platform is not something I was willing to do silently.

**What I changed:** added `regime_transition_detector_enabled: bool =
False` back to `config.py`, and gated the entire orchestrator block behind
it — `regime_transition_result` stays `None` and the module is never
constructed-and-called unless explicitly turned on, restoring the same
guarantee every other feature already has. This is documented inline in
`config.py` at the flag itself, not just in this report.

**What I did not do:** re-add the old version's specific tuning knobs
(`regime_transition_probability_threshold`,
`min_confidence_multiplier`, etc.) — the new module's classes take no
external configuration at all (`RegimeTransitionDetector()`,
`TransitionAccuracyTracker()` are both called with zero args), so there's
nothing for those settings to actually control. Re-adding decorative
settings that don't wire to anything would be worse than not having them —
Settings.tsx now says exactly that in the "Enable detector" hint instead of
presenting four sliders that silently do nothing.

### 3.3 Conflicts found and resolved (this only exists because I merged two independent branches into one)

Two things below are not present in *either* source zip — they only
became bugs once fixed-v3's tournament mode and this branch's regime work
were combined, since neither branch was ever tested against the other.

1. **Tournament-mode account-switch rebuild silently dropped every new
   tunable.** `_rebuild_account_scoped_stores()` (added by fixed-v3, predates
   the new tunables) was still constructing `StrategyPerformanceManager`/
   `DriftDetector` with bare positional args. Switching account mode would
   have silently reset a user's configured `strategy_min_sample`,
   `drift_cooldown_seconds`, etc. back to hardcoded defaults. **Fixed:**
   both branches of that method now pass every tunable from
   `TradingSettings`, exactly matching `__init__`'s construction.
2. **A duplicate method.** Applying this branch's 30-hunk diff on top of my
   already-tournament-mode-modified `orchestrator.py` via fuzzy patch
   matching left two identical copies of `_persist_decision` (one from each
   branch's context matching to a slightly different location). Caught by
   an AST-based duplicate-definition scan across the whole merged tree
   (zero duplicates found anywhere else, 53 files checked) — **fixed** by
   removing the redundant copy.
3. **A dead method referencing a deleted class.** The old
   `_build_regime_inputs()` helper (built a `RegimeSignalInputs` struct for
   the old detector's interface) had no replacement patch hunk that
   actually removed it cleanly against my modified file — it was left
   behind referencing `RegimeSignalInputs`, a class that no longer exists
   in the rewritten `regime_transition_detector.py`. **Fixed:** removed
   the dead method entirely (confirmed zero call sites first).
4. **Renamed config fields broke the Settings UI.** The new branch's
   `confidence_model.py`/`config.py` use different field names than the
   base (`confidence_model_min_training_samples` vs. the base's
   `confidence_model_min_samples_to_train`, etc.), and dropped the old
   regime-tuning fields entirely. `Settings.tsx` and `types.ts` were still
   bound to the old names. **Fixed:** renamed every binding to match,
   removed the four now-nonfunctional regime-tuning inputs, added new
   panels for the fields that do exist (Strategy Performance, Confidence
   Calibration). Verified with a repo-wide grep that zero references to any
   old field name remain, frontend or backend.

### 3.4 What I deliberately did not do

Add Settings UI for every single new config knob (`decision_*` weights,
`drift_cooldown_seconds`, `drift_auto_trial_mode`/`drift_auto_mute`,
`adaptive_wilson_confidence_level`, `strategy_weight_overrides`). These are
all live in `config.py`/settable via `.env` or direct API calls to
`/api/settings`, just without a dedicated slider yet — adding a form
control for each is feature work, not a merge-correctness requirement, and
I prioritized fixing actual breakage over expanding UI surface given the
scope already covered.

---

## 4. Signal-quality audit

Scope, stated honestly: a full line-by-line audit of the ~170 files
untouched by any of the three merged branches (most of `strategies.py`,
`indicators.py`, `incremental.py`, etc.) was not performed — that would be
claiming work I didn't do. What follows is (a) full verification of every
file this merge actually changed, already covered in §2/§3, and (b)
targeted pattern checks across the engine for the highest-value structural
risks named in the request.

- **Look-ahead bias / repainting:** zero `.shift(-N)` (negative shift,
  the standard footgun for pulling a future value into a past row) anywhere
  in `engine/`. `backtest.py`'s own docstring states its design principle
  explicitly and it's architecturally enforced, not just claimed: it
  reuses the *exact same* `IndicatorCache`/`strategies.evaluate()` code
  path the live scanner uses, bar-by-bar, so a backtest result reflects
  what the live engine would actually have done — not a separate
  reimplementation that could quietly drift or peek ahead.
- **Duplicate signals / duplicate logic:** AST-scanned every file in
  `backend/app/` for duplicate top-level or method definitions — 0 found
  after fixing the one the merge itself introduced (§3.3.2).
- **Broken imports / dead code:** full `py_compile` sweep of all 53+
  backend Python files, clean. Fixed the one dead reference the merge
  introduced (§3.3.3). Confirmed (via grep) zero remaining references to
  every renamed/removed field or class, both frontend and backend.
- **Indicator instability / duplicate computation:** the new regime
  detector's explicit design principle (reads already-computed indicator
  columns instead of recomputing) is a direct improvement here, not a
  regression risk.
- **Confidence miscalibration / rollback safety:** Phase 6 calibration's
  holdout-validation-with-rollback (already in the base, now
  instance-tunable) protects against a calibration refresh that's actually
  worse than the current one being silently adopted.
- **Session/queue/data leakage across users:** Tournament mode's whole
  purpose is preventing exactly this for account-scope switching (§2); the
  Postgres migration's per-call connection pooling prevents it at the DB
  layer (see `MIGRATION_REPORT.md` §6).

---

## 5. Full modified-files list

**Postgres migration (Step 1):** see `MIGRATION_REPORT.md` §2-3 — 11
modified, 7 created, not repeated here.

**fixed-v3 + regime-transition-detector (Steps 2-3), this session:**

| File | Source(s) | Reason |
|---|---|---|
| `backend/app/config.py` | fixed-v3 + regime branch + my fixes | Tournament fields; replaced Phase 3-6 config block with richer validated version; added back `regime_transition_detector_enabled` safety gate; added `strategy_weight_overrides` |
| `backend/app/schemas.py` | fixed-v3 + regime branch | `EngineState` tournament fields; `SignalDecision` regime/composite-confidence fields |
| `backend/app/main.py` | fixed-v3 + regime branch | `/api/control/switch-account` endpoint; websocket heartbeat fix; `rejection_min_sample_size` wiring |
| `backend/app/orchestrator.py` | fixed-v3 + regime branch + my fixes | Tournament mode; regime detector integration (gated); fixed duplicate method, dead method, tournament-rebuild tunable gap |
| `backend/app/store.py`, `services/market.py` | fixed-v3 | Tournament mode support |
| `backend/app/engine/pattern_risk_validation.py` | fixed-v3 | `scope_dir` param for tournament isolation |
| `backend/app/engine/regime_transition_detector.py` | regime branch | Full rewrite (§3.1) |
| `backend/app/engine/decision_engine.py`, `confidence_model.py`, `drift_detection.py`, `analytics_engine.py`, `signal_validation.py`, `strategy_manager.py`, `calibration.py` | regime branch | Integration + fixes (§3.1 table) |
| `frontend/src/api.ts`, `types.ts`, `components/Settings.tsx` | fixed-v3 + regime branch + my fixes | Tournament UI; renamed/removed/added config field bindings (§3.3.4) |
| `vendor/pyquotex/pyquotex/api.py` + 3 related files | fixed-v3 | Race-condition fix (§2) |

---

## 6. Performance

- Race-condition fix removes a source of silently-dropped candle data
  during concurrent websocket events — directly improves data integrity
  feeding every strategy.
- Numerically stable running statistics (Welford's) in the regime detector
  avoid precision loss over long-running processes.
- Dead-connection websocket fix frees hub resources instead of
  accumulating zombie registrations across a long-running multi-user
  session.

## 7. Multi-user impact

- Tournament mode gives complete state isolation per account scope — no
  live/demo/tournament data crossover, fixed to also respect per-user
  tunable settings (§3.3.1).
- Websocket session-leak fix prevents one user's dead connection from
  silently occupying hub state indefinitely.
- Everything from the Postgres migration (per-call connection pooling, no
  shared/global session) still applies unchanged — see
  `MIGRATION_REPORT.md` §6.

## 8. PostgreSQL migration verification

Unchanged from `MIGRATION_REPORT.md` — this session added no new database
tables or SQL. New settings (`regime_transition_detector_enabled`, etc.)
are in-memory `TradingSettings`, not persisted to Postgres.

## 9. Production readiness checklist

| Item | Status |
|---|---|
| No SQLite remaining | ✅ (Migration report) |
| Full backend compiles | ✅ verified this session, 53+ files |
| No duplicate class/function definitions | ✅ AST-scanned, whole repo |
| No dead/broken references | ✅ checked, one found and fixed |
| No stale frontend↔backend field bindings | ✅ checked, several found and fixed |
| New unvalidated feature ships with a kill switch | ✅ added (was missing) |
| Tournament mode respects user-configured tunables | ✅ fixed (was missing) |
| Live smoke test against running Postgres + frontend build | ❌ **not done** — no network access in this environment |
| tsc/eslint run on frontend changes | ❌ **not done** — no Node toolchain available; verified via brace/paren balance and grep-based reference checks only |
| Full line-by-line audit of untouched strategy/indicator files | ❌ **not done**, scoped honestly in §4 |

## 10. Production readiness score: **75/100**

Up slightly from the Postgres migration's own 72, for the same reason and
the same caveat: real, verified engineering work (evidence-based source
comparison, full compile sweeps, an AST duplicate-scan, several actually-
caught bugs) but zero live execution — no running Postgres, no `npm run
build`, no actual boot of the merged app. The regime-detector safety fix
and the tournament-rebuild fix are the kind of thing that's easy to miss
without deep review; catching them is real signal, but "reviewed carefully"
is still not the same as "ran it." Run the validation checklist in
`MIGRATION_REPORT.md` §11 plus a frontend build before deploying.
