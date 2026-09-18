# Fix Report — Pipeline Observability Regressions

Scope: **one file only**, `backend/app/orchestrator.py`. No trading logic,
signal quality algorithm, indicator, strategy, or architecture was touched.
No new features were added — every change restores something `fixed-v3`
already had, that the earlier regime-transition-detector merge silently
dropped. Net: 2496 → 2569 lines (+73), all restorations.

## Fixes applied, in order

| # | Fix | Location | Verified |
|---|---|---|---|
| 1a | `candles[-1]["close"]` → `.close` | `_shadow_loop` | Byte-for-byte matches `fixed-v3` |
| 1b | Same fix | `_rejection_reconciliation_loop` | Same |
| 2 | Restored `PipelineEventBus` import + `self._pipeline_events = PipelineEventBus()` | `Orchestrator.__init__` | `/api/pipeline/snapshots` no longer crashes |
| 3 | Restored `on_stage=self._on_trace_stage` | `PipelineTracer` constructor, `__init__` | Identical to `fixed-v3` |
| 4a | Restored 11 of 12 `_pipeline_snapshot()` calls | `_evaluate_asset_signal` | Full call-by-call context match against `fixed-v3` |
| 4b | Restored `MARKET_CONTEXT`, `PRECISION_GATE`, `QUEUE_ADD`, `PATTERN_RISK` `mark_stage()` calls | `_evaluate_asset_signal` + `_do_execute_signal_impl` | All 10 pipeline stages now present, matches `fixed-v3` exactly |
| 5 | Restored `analytics_update` broadcast | trade-close handler | All 7 `RecoveryAssessment` fields + 2 `ProductionMetrics` methods verified to exist |

**One call deliberately not restored:** a `_pipeline_snapshot()` call tied
to `regime_transition_assessment.cooldown_candles` — that variable belongs
to the old regime-detector architecture and doesn't exist after the
earlier (separately audited) rewrite, which folds the same effect into
`effective_threshold` directly. Restoring it verbatim would have meant
writing new logic, which is out of scope for a regression fix.

**One self-caught mistake:** the first Priority-1 edit briefly deleted the
`except` block and a `resolved =` assignment along with the buggy line.
Caught immediately on re-view, corrected, then re-verified the whole
function byte-for-byte against `fixed-v3` before moving on.

## Verification

- Full backend compile sweep (`py_compile`, every `.py` file): clean
- AST-based duplicate top-level/method definition scan, whole repo: 0 issues
- Zero remaining `candles[-1]["close"]` anywhere
- Every restored call's variables (`pg`, `stake_mult`, `amount`,
  `signal_id`, `recovery.*`, etc.) confirmed in scope / confirmed to exist
  on their source classes before insertion, not assumed

## Closing checklist

| Item | Evidence |
|---|---|
| ✅ Live Pipeline shows assets | `_pipeline_snapshot()` called 11x in the scan loop → `_pipeline_events.update_snapshot()` → broadcasts `pipeline_snapshot` |
| ✅ Pipeline Snapshot endpoint returns data | `o._pipeline_events` now a real, constructed attribute; `all_snapshots()` returns real data once a scan cycle has run |
| ✅ Pipeline Trace updates over WebSocket | `on_stage=self._on_trace_stage` wired → every `start_trace`/`mark_stage`/`end_trace` (13 call sites) now triggers `pipeline_trace_update` |
| ✅ Analytics updates reach frontend | Backend broadcasts `analytics_update` after trade close → `store.ts` case handler → `PipelineDashboard.tsx`'s recovery mini-stats panel |
| ✅ Shadow trades resolve | `exit_price = float(candles[-1].close)` — `shadow_store.resolve()` now reachable |
| ✅ Rejection reconciliation resolves | Same fix, same verification, `rejected_outcome_store.resolve()` now reachable |

All six confirmed by tracing the actual code path end-to-end, not by
presence-of-class inference.
