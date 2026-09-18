"""Background Auto Backtesting & Strategy Validation Engine.

Runs in a separate asyncio worker per user — never blocks the live scan loop.

Bug fixes (2026-08):
  1. `score_to_status` was imported as `from .health_score import ...` from
     inside `app/services/` — but that module lives in `app/engine/`. The
     import sat inside the per-job `try:` block, so every job that earned a
     correlation penalty died with ModuleNotFoundError and its result was
     silently dropped. Now imported once, correctly, at module level.
  2. When `_run_validation()` raised, the `backtest_runs` row was left in
     status 'running' forever (nothing ever called `finish_run`), so the run
     never appeared in `list_runs()` as failed and `latest_run_results()`
     kept returning an older run. The run row is now closed out as 'failed'
     with the error text in `notes`.
  3. A run with zero assets or zero enabled strategies used to "complete"
     with 0 results, which is indistinguishable in the UI from a run that
     worked and found nothing. It now fails loudly with the reason.
  4. Per-job failures are counted and the first error is kept, so a run
     where every job failed reports as failed instead of completed-with-0.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from ..config import RuntimeSettings
from ..services.candle_store import CandleStore
from ..services.validation_store import ValidationStore
from ..ws_hub import hub
from ..engine.correlation import CorrelationGuard
from ..engine.health_score import HealthScoreManager, score_to_status
from ..engine.validation_scoring import evaluate_strategy_oos

logger = logging.getLogger(__name__)

HTF_MAP = {
    "1m": "5m", "2m": "5m", "3m": "15m",
    "5m": "15m", "15m": "30m", "30m": "1h", "1h": "4h",
    "4h": "1day", "1day": "1day",
}


class ValidationRunError(RuntimeError):
    """Raised for a run that cannot produce meaningful results (no assets,
    no strategies, every job failed). Carries a message the UI can show."""


@dataclass
class ValidationProgress:
    status: str = "idle"  # idle | queued | running | completed | failed
    run_id: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    total_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0
    current: Optional[str] = None
    error: Optional[str] = None
    last_run_at: Optional[float] = None


class ValidationService:
    """Per-user validation worker with bounded concurrency."""

    def __init__(
        self,
        user_id: str,
        *,
        get_runtime: Callable[[], RuntimeSettings],
        get_provider,
        get_calibrator,
        get_regime_tracker,
        get_strategy_manager,
        health_manager: HealthScoreManager,
    ) -> None:
        self.user_id = user_id
        self._get_runtime = get_runtime
        self._get_provider = get_provider
        self._get_calibrator = get_calibrator
        self._get_regime_tracker = get_regime_tracker
        self._get_strategy_manager = get_strategy_manager
        self.health_manager = health_manager
        self.candle_store = CandleStore(user_id)
        self.store = ValidationStore(user_id)
        self.correlation = CorrelationGuard(self.candle_store)
        self.progress = ValidationProgress()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._running = False
        self._sem = asyncio.Semaphore(2)
        self._current_run_id: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._running = False
        for t in (self._worker_task, self._scheduler_task):
            if t:
                t.cancel()
        self._worker_task = self._scheduler_task = None

    def enqueue_manual(self) -> bool:
        # Guard 'queued' too, not just 'running' -- double-clicking "Run
        # Validation Now" used to enqueue two identical runs back to back.
        if self.progress.status in ("running", "queued"):
            return False
        self._queue.put_nowait({"trigger": "manual"})
        self.progress.status = "queued"
        self.progress.error = None
        return True

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            try:
                await self._run_validation(job.get("trigger", "scheduled"))
            except asyncio.CancelledError:
                raise
            except ValidationRunError as exc:
                logger.warning("Validation run aborted for user %s: %s", self.user_id, exc)
                await self._fail_run(str(exc))
            except Exception as exc:
                logger.exception("Validation failed for user %s", self.user_id)
                await self._fail_run(f"{type(exc).__name__}: {exc}")

    async def _fail_run(self, message: str) -> None:
        """Single exit path for a failed run: close the DB row, set progress,
        tell the UI why."""
        if self._current_run_id:
            try:
                self.store.finish_run(self._current_run_id, "failed", notes=message[:500])
            except Exception:
                logger.exception("Could not mark validation run %s failed", self._current_run_id)
        self.progress.status = "failed"
        self.progress.error = message
        self.progress.finished_at = time.time()
        self.progress.current = None
        self._current_run_id = None
        await hub.broadcast(self.user_id, "validation_error", {"message": message})
        await hub.broadcast(self.user_id, "validation_progress", self.progress_dict())

    async def _scheduler_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(30)
                runtime = self._get_runtime()
                vs = runtime.validation
                if not vs.enabled or vs.daily_run_hour_utc < 0:
                    continue
                now = time.gmtime()
                if now.tm_hour != vs.daily_run_hour_utc or now.tm_min > 5:
                    continue
                # once per day near configured hour
                if self.progress.last_run_at and time.time() - self.progress.last_run_at < 20 * 3600:
                    continue
                if self.progress.status not in ("idle", "completed", "failed"):
                    continue
                self._queue.put_nowait({"trigger": "scheduled"})
                self.progress.status = "queued"
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("Validation scheduler error: %s", type(exc).__name__)

    async def _run_validation(self, trigger: str) -> None:
        runtime = self._get_runtime()
        vs = runtime.validation
        provider = self._get_provider()
        if not provider.connected:
            try:
                await provider.connect()
            except Exception as exc:
                logger.warning("Provider connection failed during validation: %s", type(exc).__name__)

        strategies = list(runtime.trading.enabled_strategies or [])
        tf = runtime.trading.timeframe
        duration = runtime.trading.duration_seconds
        confidence = runtime.risk.confidence_threshold
        use_htf = runtime.trading.multi_timeframe_confirmation
        htf_tf = HTF_MAP.get(tf, tf)

        if not strategies:
            raise ValidationRunError(
                "No strategies are enabled — enable at least one strategy in Settings, then run validation."
            )

        assets = await self._resolve_assets(provider, runtime)
        if not assets:
            raise ValidationRunError(
                "No tradeable assets resolved — the broker returned no open assets at or above your "
                f"min_payout ({runtime.risk.min_payout}%), or the connection is down. "
                "Check the broker connection and your asset whitelist."
            )

        jobs = [(a, s) for a in assets for s in strategies]
        run_id = self.store.create_run({
            "trigger": trigger, "assets": assets, "strategies": strategies,
            "timeframe": tf, "bars": vs.history_bars, "folds": vs.walk_forward_folds,
        })
        self._current_run_id = run_id
        self.progress = ValidationProgress(
            status="running", run_id=run_id, started_at=time.time(),
            total_jobs=len(jobs), completed_jobs=0, failed_jobs=0,
        )
        self._sem = asyncio.Semaphore(max(1, vs.max_concurrency))
        await hub.broadcast(self.user_id, "validation_started", self.progress_dict())

        results: List[dict] = []
        peer_assets = list(assets)
        first_job_error: Optional[str] = None

        async def _one(asset: str, strategy: str) -> None:
            nonlocal first_job_error
            async with self._sem:
                self.progress.current = f"{asset} · {strategy}"
                await hub.broadcast(self.user_id, "validation_progress", self.progress_dict())
                try:
                    candles = await self.candle_store.fetch_or_load(
                        asset, tf, vs.history_bars,
                        lambda a=asset: provider.fetch_history(a, tf, vs.history_bars),
                    )
                    htf_candles = None
                    if use_htf and htf_tf != tf:
                        htf_candles = await self.candle_store.fetch_or_load(
                            asset, htf_tf, max(vs.history_bars // 4, 200),
                            lambda a=asset, h=htf_tf: provider.fetch_history(a, h, max(vs.history_bars // 4, 200)),
                        )
                    payout = 80.0
                    try:
                        payout = await provider.get_payout(asset, tf) or 80.0
                    except Exception as exc:
                        logger.debug("Failed to get payout for %s/%s: %s", asset, tf, type(exc).__name__)

                    row = await evaluate_strategy_oos(
                        candles,
                        asset=asset, strategy=strategy, timeframe=tf,
                        duration_seconds=duration, confidence_threshold=confidence,
                        payout_pct=payout, starting_balance=vs.starting_balance,
                        risk_settings=runtime.risk, validation_settings=vs,
                        htf_candles=htf_candles, multi_timeframe_confirmation=use_htf,
                        # Audit fix (M-5): do NOT pass the live calibrator/
                        # regime_tracker here. Those are fit (via .refresh())
                        # from this account's entire realized trade history,
                        # and reusing that same fit uniformly across every
                        # walk-forward fold means an early fold's "would this
                        # signal have passed the confidence gate" decision is
                        # influenced by a calibration curve that, from that
                        # fold's point in time, wouldn't have existed yet --
                        # a second-order lookahead distinct from (and in
                        # addition to) the candle-level lookahead guard,
                        # which is correctly enforced elsewhere in
                        # backtest.py. Leaving these as the default None
                        # makes each OOS fold judge signals on raw,
                        # uncalibrated confluence score against
                        # confidence_threshold -- a period-correct estimate.
                    )
                    penalty = self.correlation.penalize_score(asset, peer_assets, tf)
                    if penalty:
                        row["health_score"] = max(0, row["health_score"] - int(penalty))
                        row["health_status"] = score_to_status(
                            row["health_score"],
                            active_min=vs.active_score_min, trial_min=vs.trial_score_min,
                        )
                        row.setdefault("details", {})["correlation_penalty"] = penalty

                    self.store.save_result(run_id, row)
                    results.append(row)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.progress.failed_jobs += 1
                    if first_job_error is None:
                        first_job_error = f"{asset}/{strategy}: {type(exc).__name__}: {exc}"
                    logger.warning("Validation job failed %s/%s: %s", asset, strategy, exc, exc_info=True)
                finally:
                    self.progress.completed_jobs += 1
                    await hub.broadcast(self.user_id, "validation_progress", self.progress_dict())

        await asyncio.gather(*[_one(a, s) for a, s in jobs])
        self.correlation.invalidate_cache()

        if not results:
            raise ValidationRunError(
                f"All {len(jobs)} validation jobs failed — no results stored. First error: "
                f"{first_job_error or 'unknown'}"
            )

        self.health_manager.apply_validation_results(run_id, results,
                                                     active_min=vs.active_score_min,
                                                     trial_min=vs.trial_score_min)
        if vs.sync_strategy_manager:
            changed = self.health_manager.sync_strategy_manager(
                self._get_strategy_manager(), strategies,
            )
            for name in changed:
                await hub.broadcast(self.user_id, "notice", {
                    "message": f"Validation updated '{name}' status via health scores",
                })

        self.store.finish_run(run_id, "completed")
        self._current_run_id = None
        self.progress.status = "completed"
        self.progress.finished_at = time.time()
        self.progress.last_run_at = self.progress.finished_at
        self.progress.current = None
        # Partial failure is still a completed run, but say so rather than
        # letting the UI imply every asset/strategy pair was evaluated.
        self.progress.error = (
            f"{self.progress.failed_jobs}/{len(jobs)} jobs failed (first: {first_job_error})"
            if self.progress.failed_jobs else None
        )
        await hub.broadcast(self.user_id, "validation_completed", {
            **self.progress_dict(), "results_count": len(results),
        })

    async def _resolve_assets(self, provider, runtime: RuntimeSettings) -> List[str]:
        wl = runtime.trading.asset_whitelist
        try:
            all_assets = await provider.get_assets()
            open_assets = [a.symbol for a in all_assets
                           if a.is_open and a.payout >= runtime.risk.min_payout]
        except Exception as exc:
            logger.warning("Failed to fetch assets during validation: %s", type(exc).__name__)
            open_assets = []
        if wl:
            return [a for a in wl if a in open_assets or not open_assets][:vs_limit(runtime)]
        return open_assets[:vs_limit(runtime)]

    def progress_dict(self) -> dict:
        p = self.progress
        pct = round(p.completed_jobs / p.total_jobs * 100, 1) if p.total_jobs else 0
        return {
            "status": p.status, "run_id": p.run_id,
            "started_at": p.started_at, "finished_at": p.finished_at,
            "total_jobs": p.total_jobs, "completed_jobs": p.completed_jobs,
            "failed_jobs": p.failed_jobs,
            "percent": pct, "current": p.current, "error": p.error,
            "last_run_at": p.last_run_at,
        }


def vs_limit(runtime: RuntimeSettings) -> int:
    return max(1, runtime.validation.max_assets_per_run)
