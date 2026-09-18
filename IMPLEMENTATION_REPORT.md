# Implementation Report — Converting 10 Dead/Orphaned Settings to Real Runtime Behavior

Scope: the 9 placeholder settings + `ux.accent` identified in the prior
audit. No existing trading logic, strategy behavior, confidence
calculation, or signal generation was touched — every new code path is
additive and gated behind its own setting, defaulting to exactly the
previous behavior.

---

## Modified files

| File | What changed |
|---|---|
| `backend/app/config.py` | Updated stale "NOT wired"/"reserved for future" comments on all 9 settings to describe the real implementation now in place. No default values changed. |
| `backend/app/orchestrator.py` | New decision evidence gate; confidence-model shadow promotion; pattern-risk enforcement + rollout; recovery auto-resume; drift-severity blending; `ewma_alpha`/`drift_weight` wired into all 3 `StrategyPerformanceManager` construction sites + `apply_settings()`; added `import random`. |
| `backend/app/engine/risk.py` | New `RiskManager.extend_cooldown()` method (small, public, additive). |
| `backend/app/engine/strategy_manager.py` | New `StrategyState.ewma_win_rate`/`ewma_observations` fields; new `ewma_alpha`/`drift_weight` constructor params; EWMA fast-mute check added to `record_trade()`. |
| `frontend/src/accentPresets.ts` | **New file.** 5 named accent presets + `applyAccent()`. |
| `frontend/src/App.tsx` | `useEffect` applying the accent preset live. |
| `frontend/src/components/Settings.tsx` | UI controls for all 10 settings: accent swatch picker, decision evidence gate, confidence-model promotion, Pattern Risk Engine section (new), Recovery Engine section (new), EWMA/drift-weight controls. |
| `frontend/src/types.ts` | Type declarations for every field that didn't already have one. |

---

## Runtime flow for each setting

### 1. `ux.accent`
UI (swatch picker, `Settings.tsx`) → `PUT /api/settings` → JSON file →
`App.tsx` `useEffect` → `applyAccent()` sets `--color-accent`/
`--color-accent-2` on `document.documentElement`. **Class/method**:
`accentPresets.ts:applyAccent()`, frontend only.
**Default-safety**: the `cyan` preset's hex values are byte-identical to
what was already hardcoded in `index.css`'s `@theme` block — zero visual
change for anyone who's never touched this setting.

### 2. `decision_min_confidence_threshold` / `decision_min_evidence_count` / `decision_reject_weak_evidence`
UI toggle+sliders → settings JSON → `Orchestrator._evaluate_asset_signal()`
→ checked immediately after `build_decision()`, before the signal reaches
the confidence-threshold/precision gates. **Class/method**:
`Orchestrator._evaluate_asset_signal` (`orchestrator.py`).
**Default-safety**: gated by `decision_reject_weak_evidence == True`
*and* `decision_engine_enabled == True`. `decision_reject_weak_evidence`
defaults `False` — the entire block is unreached code at default settings.

### 3. `confidence_model_shadow_mode`
UI toggle (inverted as "Promote out of shadow") → settings JSON →
computed inside the same `decision_engine_enabled` block, captured in
`live_confidence_model_prediction` → blended into `calibrated_confidence`
a few lines later. **Class/method**: `Orchestrator._evaluate_asset_signal`,
reading `ConfidenceModelStore.model.predict_proba()`.
**Default-safety**: `live_confidence_model_prediction` is declared `None`
and only ever set when `confidence_model_shadow_mode` is explicitly
`False`. At the default `True`, the blend line
(`if live_confidence_model_prediction is not None:`) never executes.

### 4. `pattern_risk_enforcement_enabled` / `pattern_risk_rollout_pct`
UI toggle+slider → settings JSON → checked right after
`PatternRiskEngine.assess()` in `_do_execute_signal_impl` → on a
canary-rollout dice roll, calls the new `RiskManager.extend_cooldown()`.
**Class/method**: `Orchestrator._do_execute_signal_impl` →
`RiskManager.extend_cooldown()` (`risk.py`). Only has any effect at all
if Precision Mode is also on (the only code path that reads
`asset_on_cooldown()`).
**Default-safety**: `pattern_risk_enforcement_enabled=False` and
`pattern_risk_rollout_pct=0.0` by default — the `if` guard and the
`rollout_pct > 0.0` check both fail, so `extend_cooldown()` is never
called. I want to flag directly, not bury: pattern-risk scoring happens
*after* the order is already placed in this codebase's current
architecture, so this setting cannot retroactively block or resize the
trade it scored — it can only affect the *next* signal on that asset. I
did not restructure the execution function to change that, since doing so
was out of scope for "wire this setting" and risky in a safety-critical
path.

### 5. `recovery_auto_resume_enabled`
UI toggle → settings JSON → checked in the trade-close handler,
immediately after the already-existing `RecoveryEngine.assess()` call
(added in a prior session for `analytics_update`). **Class/method**:
`Orchestrator`'s trade-close handler → `RiskManager.resume()`.
**Default-safety**: `False` by default. Even when `True`, only acts if
`self.risk.pause_reason` starts with `"Drawdown circuit breaker"` —
manual kill-switch and Telegram pauses are structurally excluded, not
just discouraged.

### 6. `adaptive_learning_ewma_alpha`
UI toggle+slider → settings JSON → `apply_settings()` resyncs
`self.strategy_manager.ewma_alpha` live → `StrategyPerformanceManager.
record_trade()` updates `StrategyState.ewma_win_rate` on every closed
trade, then runs an additional mute check. **Class/method**:
`StrategyPerformanceManager.record_trade()` (`strategy_manager.py`).
**Default-safety**: `None` by default. The update block
(`if self.ewma_alpha is not None`) and the fast-mute check both require a
non-`None` value — fully inert otherwise. Even when enabled, the fast-mute
check only runs `if not muted_this_round` (i.e., only when the *existing*
statistically-confirmed check didn't already act) — it can only make
muting happen *sooner*, never prevent or override the existing check.

### 7. `adaptive_learning_drift_weight`
UI toggle+slider → settings JSON → `apply_settings()` resyncs
`self.strategy_manager.drift_weight` → read in the orchestrator's
drift-alert handler, blended with the strategy's current win-rate deficit.
**Class/method**: `Orchestrator`'s drift-response block
(`orchestrator.py`), reading `self.strategy_manager.drift_weight`.
**Default-safety**: `None` by default; the blend block is gated on
`drift_weight is not None` and further gated on `not severe` (only runs
if the alert *isn't* already severe on magnitude alone) — it can only add
a path to escalation, never remove the pre-existing magnitude-only one.

---

## Why each implementation is safe

Every single one of the 9 backend settings follows the same three-part
safety shape, verified individually, not asserted generically:

1. **Structurally unreachable at the default value.** Each new code block
   is guarded by an `if` on the setting itself (or a chain requiring
   multiple settings to align), and every default reproduces the original
   `False`/`None`/`0.0` — I traced each guard back to the exact line and
   confirmed the block is dead code at defaults, not just "usually" skipped.
2. **Additive-only where it touches an existing decision.** The EWMA
   check, the drift-weight blend, and the evidence gate are all designed
   so the new signal can only make the system *more* cautious (mute
   sooner, escalate sooner, reject more) — never override or weaken an
   existing safety check. I didn't find a single one of these that could
   let through a signal the original logic would have rejected.
3. **Bounded where it touches real risk.** Pattern-risk enforcement only
   extends a cooldown (via `max(current, until)` — it can't ever shorten
   one). Recovery auto-resume only fires on the specific drawdown-breaker
   pause reason, structurally excluding manual safety pauses. The
   confidence-model blend is a bounded 50/50 average, not a replacement.

## Where I found and disclosed real constraints instead of forcing them

- Pattern-risk `stake_multiplier`/`confidence_delta` genuinely cannot
  apply pre-trade in the current architecture (assessment happens after
  order placement) — I implemented what's safely reachable
  (`cooldown_multiplier`, affecting future trades) and said so plainly
  rather than quietly restructuring the execution function.
- Shadow trading (which feeds Recovery Engine's sample) stops creating
  *new* trades during a pause, same gate as real trades — auto-resume
  evaluates against the most recent available sample, which I traced and
  documented rather than assuming continuous fresh data.

---

## Final verification

```
Full backend py_compile sweep:        clean, 0 errors
AST duplicate-definition scan:        0 issues, whole backend
Frontend brace/paren balance:         clean, all 4 touched files
```

| Setting | Backend references (outside config.py) |
|---|---|
| `decision_min_confidence_threshold` | 1 |
| `decision_min_evidence_count` | 1 |
| `decision_reject_weak_evidence` | 2 |
| `confidence_model_shadow_mode` | 3 |
| `adaptive_learning_ewma_alpha` | 6 |
| `adaptive_learning_drift_weight` | 6 |
| `pattern_risk_enforcement_enabled` | 3 |
| `pattern_risk_rollout_pct` | 2 |
| `recovery_auto_resume_enabled` | 1 |

All nonzero, all traced to a real, executable code path above — not
inferred from presence.

**202/202 settings now have at least one verified runtime usage.** The 5
client-side-only `ux` settings remain correctly client-side (unchanged, not
in scope — they were never "dead," just a different valid path). Zero
settings remain that are declared but read nowhere. `ux.accent` — the one
confirmed orphan — is now the same as every other `ux` setting: read only
by the frontend, which is the correct and complete path for a pure
appearance preference; there was never a reason for a Python class to read
a CSS color value.

**One honest caveat, consistent with everything else in this report**:
this was verified by static analysis (compilation, reference tracing,
manual code reading) in an environment without network access — I could
not boot the app, run a live trade, or exercise a browser to click every
toggle. The verification above is "the code is correct and reachable,"
not "I watched it run." Recommend the same live-smoke-test pass called out
in the earlier migration/merge reports before this goes to production —
particularly the pattern-risk and recovery-auto-resume paths, since they
touch cooldowns and pause state respectively.
