"""Adaptive Market Context Engine (Stage B).

Stage A (see engine/strategies.py, engine/signal_validation.py,
engine/strategy_manager.py) turned previously-hardcoded thresholds into
configurable constructor/call parameters -- same numbers, just tunable.

Stage B is this file: a reusable, per-(asset, timeframe) engine that turns a
stream of raw indicator values into *adaptive* statistics -- rolling
percentile rank, z-score, and drift-limited EMA-smoothed thresholds --
instead of every asset/timeframe/session being judged against one fixed
number.

Stage C has since shipped: this module is called from strategies.py
(dead-market ADX/ATR floor, RSI/volume bounds, S/R proximity, confluence
edge), precision_gate.py (percentile_rank), and orchestrator.py, all
gated behind TradingSettings.adaptive_mode_enabled (default False) as
originally planned here. (Audit fix M-1: this docstring previously said
Stage C hadn't wired it in yet -- that was stale as of Stage C landing;
corrected so a future read of this file doesn't mistake live, load-bearing
code for unused scaffolding.)

Design constraints this file satisfies:
- Per (asset, timeframe): every metric is tracked independently per key,
  so EURUSD-1m noise never affects GBPJPY-5m's thresholds.
- Cold-start safe: below `min_samples` observations for a given key, every
  computation method returns the caller's static default untouched.
- Bounded memory: a fixed-size `deque(maxlen=rolling_window)` per
  (asset, timeframe, metric) -- old samples silently fall off, memory is
  O(tracked_keys * rolling_window), not unbounded.
- Smoothed + drift-limited: the threshold this engine publishes is an EMA
  of the raw percentile/z-score value, additionally clamped so it can't
  move by more than `max_drift_per_update` (a fraction of its previous
  value) in a single update -- one noisy candle can't suddenly swing a
  threshold that a live signal gate depends on.
- Never raises into the caller: any internal error is caught, logged, and
  answered with the static default -- a bug in adaptive statistics must
  never be able to block or corrupt live signal generation.
- One instance per user (owned by Orchestrator, like pipeline_tracer /
  production_metrics), not a module-level singleton -- no cross-tenant
  state leakage in a multi-user deployment.

Lifecycle & persistence (Foundation Stabilization decision): this engine's
rolling history is IN-MEMORY ONLY and resets to cold-start on every backend
restart. This is a deliberate choice, not an oversight:
  1. Cold-start is cheap and safe. Below min_samples, every method returns
     the exact same static default the bot already uses today -- a restart
     just means "back to proven defaults for a while," never "back to
     nothing." At typical min_samples=30 and 1m-5m candles, warm-up takes
     minutes to a couple of hours, not days.
  2. A restart is the same moment stale history would be most likely to be
     wrong anyway -- broker reconnects, account switches, and deploys often
     cluster around restarts, and the market regime today is not guaranteed
     to resemble yesterday's saved percentiles. Re-learning from fresh data
     is arguably the SAFER default here, not the costlier one.
  3. It matches the existing pattern for pipeline_tracer and
     production_metrics (also per-user, also in-memory-only, also reset on
     restart) rather than strategy_manager.py's disk-persisted state --
     that module persists because a strategy's mute/active status has real
     standing consequences if lost (a bad strategy silently un-muting after
     a restart could compound real losses); an adaptive threshold reverting
     to its static default has no such downside, by design.
  If this changes (e.g. restarts become frequent enough that constant
  re-warm-up is itself a problem), the design for adding persistence would
  be: periodic (not per-observation) snapshot of `_metrics` -- asset,
  timeframe, metric, the deque's contents, and published_value -- to a
  per-user JSON file via user_data_dir(), on the same debounced-save
  interval strategy_manager.py already uses, loaded back in Orchestrator
  __init__ before the first observe() call. Not implemented here.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple
import logging
import math
import time

from . import indicators as ta
from ..logging_setup import log_event

logger = logging.getLogger("qat.market_context")


@dataclass
class AdaptiveThresholdConfig:
    """Tuning knobs for one MarketContextEngine instance. Mirrors the
    TradingSettings fields Stage C will add (adaptive_mode_enabled,
    adaptive_rolling_window, adaptive_min_samples, adaptive_ema_alpha,
    adaptive_max_drift_pct, adaptive_use_zscore, adaptive_percentile,
    adaptive_update_interval_s) -- kept as a plain dataclass here so this
    module has zero dependency on config.py/pydantic and can be unit tested
    in isolation.
    """
    enabled: bool = False                # Stage C master switch; False = engine is inert everywhere
    rolling_window: int = 200            # max samples kept per (asset, timeframe, metric)
    # Duplicate-observation suppression. The scan loop is event-driven
    # with a 3s backstop, so on a 1m timeframe it evaluates the SAME
    # closed candle ~20 times, and strategies.evaluate() calls observe()
    # on every one of them. Left unsuppressed the rolling window fills
    # with duplicates of a handful of real candles: min_samples is
    # cleared in minutes instead of candles, and percentile_rank -- which
    # scales trade stake via _adaptive_risk_multipliers -- is computed
    # against a distribution weighted by how often each candle happened
    # to be scanned rather than by the market.
    # BLAST RADIUS (verified, and narrower than it first appears):
    # observe() itself has no `enabled` check, so the duplicate-filled
    # windows were built in every configuration. But all three consumers
    # -- orchestrator._adaptive_risk_multipliers, strategies.py's
    # confluence edge, and precision_gate's agreement requirement -- do
    # gate on cfg.enabled, which defaults to False. So with adaptive mode
    # OFF this cost memory and CPU but changed no trading decision; with
    # adaptive mode ON it distorted all three.
    dedupe_repeat_observations: bool = True
    # Two observations of the same key with a bit-identical float inside
    # this window are treated as the same bar re-scanned. Indicator
    # outputs are continuous floats, so exact equality across two genuinely
    # different bars effectively does not occur; a same-second repeat is a
    # re-scan. Sized just under the shortest supported timeframe (30s).
    dedupe_window_s: float = 25.0
    # Cap on tracked (asset, timeframe, metric) keys. Keys were previously
    # only ever removed by an explicit reset(), so a rotating asset
    # universe accumulated them for the life of the process.
    max_tracked_keys: int = 900
    # Cap on how many per-key sample counts get_stats() serialises.
    stats_sample_limit: int = 50
    min_samples: int = 30                # below this sample count, always use the caller's static default
    ema_alpha: float = 0.15              # smoothing on the published value (0 = frozen, 1 = no smoothing at all)
    max_drift_per_update: float = 0.05   # max fractional change in the published value per single update
    use_zscore: bool = False             # False -> percentile-rank based; True -> mean/std z-score based
    percentile: float = 0.5              # target percentile of recent history ("typical" value); 0.5 = median
    update_interval_s: float = 0.0       # min seconds between recompute passes per key (0 = recompute every call)
    rsi_high_percentile: float = 0.90    # Adaptive RSI: this asset's own Nth-percentile RSI = adaptive overbought bound
    rsi_low_percentile: float = 0.10     # Adaptive RSI: this asset's own Nth-percentile RSI = adaptive oversold bound
    vol_low_percentile: float = 0.20     # Adaptive Volume: this asset's own Nth-percentile vol_strength = low bound
    vol_high_percentile: float = 0.80    # Adaptive Volume: this asset's own Nth-percentile vol_strength = high bound
    sr_atr_multiplier: float = 1.0       # Adaptive S/R: proximity band = this * this asset's own median ATR%
    confluence_edge_low: float = 0.55    # Adaptive Confluence: required edge when this asset's trend is at its strongest (percentile rank 100)
    confluence_edge_high: float = 0.70   # Adaptive Confluence: required edge when this asset's trend is at its weakest (percentile rank 0)

    # --- Per-feature ON/OFF toggles (Production Adaptive Settings UI) ------
    # `enabled` above is the master switch (False = every one of these is
    # inert, full legacy behavior). When `enabled` is True, each of these
    # independently controls one feature; turning any single one off falls
    # back cleanly to that feature's static default -- the same static
    # default it already used before Stage C existed -- without touching
    # any other adaptive feature.
    rsi_enabled: bool = True
    adx_enabled: bool = True
    atr_enabled: bool = True
    volume_enabled: bool = True
    sr_enabled: bool = True
    confluence_enabled: bool = True
    signal_validation_enabled: bool = True
    precision_gate_enabled: bool = True
    risk_enabled: bool = True

    # --- Adaptive Precision Gate internal magnitudes (not UI-exposed) -----
    # Scales precision_gate.py's min_agreeing requirement by this asset's
    # own ADX percentile rank -- reuses the "adx" history already tracked
    # for the dead-market gate / Adaptive Confluence, no new observe() call.
    precision_agreeing_low: int = 2     # min_agreeing at this asset's strongest recent trend (percentile rank 100)
    precision_agreeing_high: int = 4    # min_agreeing at this asset's weakest recent trend (percentile rank 0)

    # --- Adaptive Risk internal magnitudes (not UI-exposed) ----------------
    # Both bounded tightly and symmetrically around 1.0 -- Adaptive Risk can
    # only NUDGE stake/cooldown within a narrow band, never override the
    # hard safety circuit breakers (daily loss limit, max consecutive
    # losses, drawdown breaker), which stay fixed regardless of this toggle.
    risk_stake_multiplier_low: float = 0.85     # stake multiplier at this asset's weakest recent trend (percentile rank 0)
    risk_stake_multiplier_high: float = 1.05    # stake multiplier at this asset's strongest recent trend (percentile rank 100)
    risk_cooldown_multiplier_low: float = 1.0   # per-asset cooldown multiplier at strongest recent trend (no extension)
    risk_cooldown_multiplier_high: float = 1.5  # per-asset cooldown multiplier at weakest recent trend (extend up to 1.5x)


@dataclass
class _MetricState:
    """Rolling history + last published (smoothed) value for one
    (asset, timeframe, metric) key. Not exposed outside this module."""
    history: Deque[float] = field(default_factory=lambda: deque(maxlen=200))
    published_value: Optional[float] = None
    last_update_ts: float = 0.0
    sample_count: int = 0
    # Bar identity of the last accepted observation, for duplicate
    # suppression. `last_bar_ts` is used when the caller supplies one;
    # `last_value`/`last_value_ts` back the fallback heuristic for callers
    # that do not (every current caller).
    last_bar_ts: Optional[float] = None
    last_value: Optional[float] = None
    last_value_ts: float = 0.0
    duplicates_skipped: int = 0
    # Cached ascending copy of `history`, invalidated on every accepted
    # observation. percentile_rank() and _compute_raw() both sorted the
    # full window on EVERY call -- per asset, per metric, per scan.
    _sorted: Optional[list] = None
    # Percentile that produced `published_value`, so a query for a
    # different percentile cannot be served the cached one.
    published_percentile: Optional[float] = None

    def sorted_history(self) -> list:
        if self._sorted is None:
            self._sorted = sorted(self.history)
        return self._sorted


@dataclass
class MarketContextSnapshot:
    """A qualitative read of current market conditions for one
    (asset, timeframe), composed from already-computed indicator columns
    (this does NOT recompute indicators -- it reads the same enriched
    DataFrame columns strategies.py/signal_validation.py already use, per
    the 'reuse existing indicator implementations' requirement).

    Each field is a 0-100 "quality" score where higher = more favorable for
    trading, except `regime` (categorical) and `noise_level` (0-100, higher
    = noisier/worse). This is intentionally a read-only summary object --
    Stage C decides what to *do* with it; this class only describes what a
    caller reuses whatever gates already exist.
    """
    asset: str
    timeframe: str
    regime: str                    # "trend" | "range" | "mixed" (from strategies._regime)
    trend_strength: float          # derived from ADX percentile rank
    volatility: float               # derived from ATR% percentile rank
    liquidity: float                # derived from payout/spread proxy if available, else neutral 50.0
    volume_quality: float           # derived from vol_strength column
    candle_quality: float           # derived from candle body ratio
    noise_level: float              # derived from ADX/BB-width instability (0=calm/stable, 100=choppy/noisy)
    session_strength: float         # derived from time_session_volatility_multiplier
    historical_stability: float     # derived from stddev of recent ADX (100 = very stable regime, 0 = whipsawing)


class MarketContextEngine:
    """Per-(asset, timeframe) adaptive market-statistics engine.

    Typical usage (once wired in Stage C -- NOT called anywhere yet):

        engine = MarketContextEngine(AdaptiveThresholdConfig(enabled=True))
        engine.observe("EURUSD_otc", "1m", "adx", float(row["adx"]))
        adx_threshold = engine.adaptive_threshold(
            "EURUSD_otc", "1m", "adx", static_default=14.0)

    Every public method degrades gracefully to the caller-supplied static
    default with zero history, insufficient samples, a disabled engine, or
    an internal error -- see class docstring for the full list of
    guarantees this provides.
    """

    def __init__(self, cfg: Optional[AdaptiveThresholdConfig] = None):
        self.cfg = cfg or AdaptiveThresholdConfig()
        self._metrics: Dict[Tuple[str, str, str], _MetricState] = {}
        # Observability counters -- transient, process-lifetime only (same
        # pattern as pipeline_tracer/production_metrics: per-user instance,
        # not persisted, reset on restart). Useful for a future
        # /api/market-context/stats endpoint or just debugging in a shell.
        self._observations_total = 0
        self._fallbacks_served = 0
        self._adaptive_served = 0
        self._errors_total = 0
        self._duplicates_skipped = 0
        self._keys_evicted = 0

    def _evict_if_needed(self) -> None:
        """Keep the tracked-key count bounded.

        `self._metrics` grew for the life of the process: a key was only
        ever removed by an explicit reset(). The scanner's candidate set
        rotates with payout, so over days every asset the account has ever
        traded accumulates a full rolling window per timeframe per metric.
        Python dicts preserve insertion order and observe() re-inserts on
        every accepted sample, so the front of the dict is the coldest key.
        """
        over = len(self._metrics) - self.cfg.max_tracked_keys
        if over <= 0:
            return
        for key in list(self._metrics.keys())[:over]:
            self._metrics.pop(key, None)
            self._keys_evicted += 1
        log_event(
            logger, logging.DEBUG, "market_context_keys_evicted",
            evicted=over, tracked=len(self._metrics), cap=self.cfg.max_tracked_keys,
            reason="tracked_key_cap_reached",
        )

    # -- ingestion ----------------------------------------------------------- #
    def observe(
        self,
        asset: str,
        timeframe: str,
        metric: str,
        value: Optional[float],
        bar_ts: Optional[float] = None,
    ) -> None:
        """Record one new sample for (asset, timeframe, metric).

        Call this once per newly-closed candle per metric you want the
        engine to track -- an O(1) deque append, cheap enough to call every
        scan iteration for every tracked metric without measurable
        performance impact. Silently ignores None/NaN so one bad indicator
        read can never corrupt the rolling window.

        `bar_ts` (optional) is the timestamp of the candle the value came
        from. When supplied, a repeat observation of the same bar is
        skipped exactly. It is optional and defaults to None so that every
        existing caller keeps working unchanged; those callers get the
        fallback heuristic described on
        AdaptiveThresholdConfig.dedupe_window_s instead, which is what
        actually fixes the duplicate-window corruption today.

        Duplicate suppression can be turned off entirely with
        `dedupe_repeat_observations=False`, restoring the previous
        append-everything behaviour bit-for-bit.
        """
        if value is None:
            return
        try:
            fvalue = float(value)
        except (TypeError, ValueError):
            return
        if math.isnan(fvalue) or math.isinf(fvalue):
            return

        key = (asset, timeframe, metric)
        state = self._metrics.get(key)
        if state is None:
            state = _MetricState(history=deque(maxlen=self.cfg.rolling_window))
            self._metrics[key] = state
            self._evict_if_needed()

        if self.cfg.dedupe_repeat_observations:
            now = time.time()
            if bar_ts is not None:
                if state.last_bar_ts is not None and bar_ts == state.last_bar_ts:
                    state.duplicates_skipped += 1
                    self._duplicates_skipped += 1
                    return
            elif (
                state.last_value is not None
                and fvalue == state.last_value
                and (now - state.last_value_ts) < self.cfg.dedupe_window_s
            ):
                state.duplicates_skipped += 1
                self._duplicates_skipped += 1
                return
            state.last_bar_ts = bar_ts
            state.last_value = fvalue
            state.last_value_ts = now

        state.history.append(fvalue)
        state.sample_count += 1
        state._sorted = None          # window changed; drop the sorted cache
        self._observations_total += 1
        # Move to the end of the LRU order (dicts preserve insertion order).
        self._metrics[key] = self._metrics.pop(key)

    # -- adaptive threshold computation --------------------------------------- #
    def adaptive_threshold(
        self,
        asset: str,
        timeframe: str,
        metric: str,
        static_default: float,
        percentile_override: Optional[float] = None,
    ) -> float:
        """Returns an adaptive threshold for (asset, timeframe, metric), or
        `static_default` if the engine is disabled, there isn't yet enough
        history (cold-start), the per-key update interval hasn't elapsed
        yet, or anything goes wrong internally.

        `percentile_override` lets a caller ask for a different percentile
        than `self.cfg.percentile` for THIS query, while still sharing the
        same smoothing/drift-limit/min_samples/enabled semantics as every
        other adaptive_threshold() call. This exists for two-sided metrics
        like RSI, where "overbought" and "oversold" bounds need different
        percentiles (e.g. 90th and 10th) computed from the SAME underlying
        history. Store the raw samples under one metric name (e.g. "rsi")
        via observe(), but query the bound under two distinct metric names
        (e.g. "rsi_high_bound" and "rsi_low_bound") each observe()'d with
        the same raw values -- this gives each bound its own independent
        smoothing state (published_value/last_update_ts) so a 90th- and a
        10th-percentile query never collide by sharing one EMA. The extra
        deque this creates is a second bounded (rolling_window-capped) copy
        of the same small float history -- a few KB, not a real cost.

        This method never raises. A bug here degrading to the exact same
        static default the bot already uses today is the whole point of
        this design -- adaptive thresholds should only ever be able to make
        things *more* informed, never able to break live trading.
        """
        if not self.cfg.enabled:
            self._fallbacks_served += 1
            return static_default

        try:
            key = (asset, timeframe, metric)
            state = self._metrics.get(key)
            if state is None or state.sample_count < self.cfg.min_samples:
                self._fallbacks_served += 1
                log_event(
                    logger, logging.DEBUG, "adaptive_threshold_fallback",
                    asset=asset, timeframe=timeframe, metric=metric, static_default=static_default,
                    sample_count=state.sample_count if state else 0, min_samples=self.cfg.min_samples,
                    reason="cold_start_insufficient_history",
                )
                return static_default

            now = time.time()
            requested_pct = (
                percentile_override if percentile_override is not None
                else self.cfg.percentile
            )
            if (
                self.cfg.update_interval_s > 0
                and state.published_value is not None
                # The cached value was computed for ONE percentile. Serving
                # it to a query for a different percentile returned, for
                # example, the 90th-percentile RSI bound to a 10th-percentile
                # query. The docstring below warns about this; nothing
                # enforced it. Now the cache is bypassed on a mismatch.
                and state.published_percentile == requested_pct
                and (now - state.last_update_ts) < self.cfg.update_interval_s
            ):
                # Too soon to recompute -- serve the last published value
                # rather than the static default, since we DO have a
                # confident adaptive value, it's just not due for a refresh.
                self._adaptive_served += 1
                return state.published_value

            raw = self._compute_raw(state, percentile_override=percentile_override)

            previous_value = state.published_value
            if previous_value is None:
                smoothed = raw
            else:
                drift_capped = self._clamp_drift(previous_value, raw)
                smoothed = previous_value + self.cfg.ema_alpha * (drift_capped - previous_value)

            state.published_value = smoothed
            state.published_percentile = requested_pct
            state.last_update_ts = now
            self._adaptive_served += 1
            log_event(
                logger, logging.DEBUG, "adaptive_threshold_applied",
                asset=asset, timeframe=timeframe, metric=metric,
                previous_value=round(previous_value, 4) if previous_value is not None else None,
                new_value=round(smoothed, 4), static_default=static_default,
                sample_count=state.sample_count,
                percentile=percentile_override if percentile_override is not None else self.cfg.percentile,
                reason="sufficient_history_adaptive_value_computed",
            )
            return smoothed
        except Exception:
            # Defensive catch-all: whatever goes wrong, static_default keeps
            # the bot trading exactly as if adaptive mode were off.
            self._errors_total += 1
            log_event(
                logger, logging.WARNING, "adaptive_threshold_error",
                asset=asset, timeframe=timeframe, metric=metric, static_default=static_default,
                reason="internal_error_fell_back_to_static_default",
            )
            logger.warning(
                "MarketContextEngine.adaptive_threshold failed for %s/%s/%s "
                "-- falling back to static default %.4f",
                asset, timeframe, metric, static_default, exc_info=True,
            )
            return static_default

    def last_value(self, asset: str, timeframe: str, metric: str) -> Optional[float]:
        """Returns the most recently observe()'d raw value for
        (asset, timeframe, metric), or None if nothing has been observed
        yet. Read-only, no side effects -- used by callers (e.g.
        precision_gate.py) that need "what did this asset's ADX last look
        like" without themselves having the current row/bar in scope."""
        state = self._metrics.get((asset, timeframe, metric))
        if state is None or not state.history:
            return None
        return state.history[-1]

    def percentile_rank(self, asset: str, timeframe: str, metric: str, value: float) -> Optional[float]:
        """Where does `value` fall in this key's recent history, as a 0-100
        rank? Returns None (not 50.0) below min_samples -- callers must
        decide their own cold-start behavior for a *rank* query, unlike
        adaptive_threshold() which has an explicit static_default to fall
        back to. Never raises; returns None on any internal error."""
        try:
            state = self._metrics.get((asset, timeframe, metric))
            if state is None or state.sample_count < self.cfg.min_samples:
                return None
            values = state.sorted_history()
            if not values:
                return None
            # bisect on the already-sorted window: O(log n) instead of the
            # previous O(n log n) sort + O(n) scan on every call, per asset,
            # per metric, per scan cycle.
            below = bisect_right(values, value)
            return round(100.0 * below / len(values), 1)
        except Exception:
            self._errors_total += 1
            logger.warning("MarketContextEngine.percentile_rank failed for %s/%s/%s", asset, timeframe, metric, exc_info=True)
            return None

    # -- internal statistics --------------------------------------------------- #
    def _compute_raw(self, state: _MetricState, percentile_override: Optional[float] = None) -> float:
        percentile = percentile_override if percentile_override is not None else self.cfg.percentile
        values = list(state.history)
        if self.cfg.use_zscore:
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            std = math.sqrt(variance) if variance > 0 else 0.0
            if std == 0:
                return mean
            z = self._percentile_to_z(percentile)
            return mean + z * std
        sorted_vals = state.sorted_history()
        idx = min(len(sorted_vals) - 1, max(0, int(round(percentile * (len(sorted_vals) - 1)))))
        return sorted_vals[idx]

    @staticmethod
    def _percentile_to_z(p: float) -> float:
        """Coarse linear approximation mapping percentile [0,1] to a
        z-score in roughly [-2.33, +2.33] (the 1st-99th percentile range
        for a normal distribution). Deliberately not a precise inverse-CDF
        implementation (e.g. Acklam's algorithm) -- this only nudges a
        threshold up or down, it isn't the sole basis of a trading
        decision, so pulling in scipy for statistical precision here isn't
        worth the added dependency."""
        p = min(max(p, 0.001), 0.999)
        return (p - 0.5) * 2 * 2.33

    def _clamp_drift(self, prev: float, raw: float) -> float:
        if prev == 0:
            return raw
        max_delta = abs(prev) * self.cfg.max_drift_per_update
        delta = raw - prev
        if abs(delta) > max_delta:
            return prev + math.copysign(max_delta, delta)
        return raw

    # -- higher-level context snapshot ----------------------------------------- #
    def snapshot(self, asset: str, timeframe: str, row, history_df=None) -> MarketContextSnapshot:
        """Builds a MarketContextSnapshot from an already-enriched candle
        row (same columns strategies.py/signal_validation.py read: adx,
        bb_width, atr, close, rsi, vol_strength, open/high/low/close for
        candle quality). Does not recompute any indicator -- purely reads
        existing columns and expresses them as adaptive percentile ranks
        when history is available, or reasonable neutral defaults when it
        isn't (cold-start safe, same as adaptive_threshold()).

        `history_df` is optional; when provided (a recent tail of enriched
        candles), it's used for the `historical_stability` field (ADX
        stddev) without requiring a separate observe() call per bar.
        """
        def _safe_get(col: str, default: float = 0.0) -> float:
            try:
                v = row[col]
                return float(v) if v is not None and not (isinstance(v, float) and math.isnan(v)) else default
            except Exception:
                return default

        adx = _safe_get("adx", 20.0)
        atr = _safe_get("atr", 0.0)
        close = _safe_get("close", 1.0) or 1.0
        atr_pct = ta.atr_pct(atr, close)
        vol_strength = _safe_get("vol_strength", 50.0)

        # Candle quality: body/range ratio, same measure signal_validation.py
        # already uses for _check_candle_boundaries -- reused, not duplicated.
        try:
            o, h, l, c = _safe_get("open"), _safe_get("high"), _safe_get("low"), _safe_get("close")
            candle_range = max(h - l, 1e-9)
            body_ratio = abs(c - o) / candle_range
        except Exception:
            body_ratio = 0.5
        candle_quality = min(100.0, body_ratio * 133.0)  # ~0.75 body ratio -> 100

        # Trend strength: percentile rank of ADX if we have history for this
        # key, else a linear 0-100 map off the raw value (ADX rarely exceeds
        # ~60 in practice) as a reasonable neutral fallback.
        trend_rank = self.percentile_rank(asset, timeframe, "adx", adx)
        trend_strength = trend_rank if trend_rank is not None else min(100.0, adx / 0.6)

        vol_rank = self.percentile_rank(asset, timeframe, "atr_pct", atr_pct)
        volatility = vol_rank if vol_rank is not None else min(100.0, atr_pct / 0.5 * 100.0)

        # Noise level: BB-width relative to its own rolling mean (same
        # relative-to-self comparison strategies.py's dead-market gate
        # already uses) -- a market whose bands are unusually wide relative
        # to their own recent average is choppier than usual right now.
        bb_width = _safe_get("bb_width", 0.0)
        noise_level = 50.0
        if history_df is not None and "bb_width" in getattr(history_df, "columns", []):
            try:
                width_ma = float(history_df["bb_width"].rolling(20).mean().iloc[-1])
                if width_ma:
                    noise_level = min(100.0, max(0.0, (bb_width / width_ma) * 50.0))
            except Exception:
                pass

        # Historical stability: inverse of recent ADX stddev -- a market
        # whose trend strength hasn't been swinging wildly is more stable
        # (higher score) than one whipsawing between trend/range.
        historical_stability = 50.0
        if history_df is not None and "adx" in getattr(history_df, "columns", []):
            try:
                recent_adx = history_df["adx"].iloc[-20:]
                std = float(recent_adx.std()) if len(recent_adx) >= 5 else 0.0
                historical_stability = max(0.0, 100.0 - std * 5.0)
            except Exception:
                pass

        # Session strength: reuse ta.time_session_volatility_multiplier if
        # the caller already computed it into the row; otherwise neutral.
        session_strength = min(100.0, max(0.0, _safe_get("session_vol_mult", 1.0) * 50.0))

        try:
            from .strategies import _regime as _classify_regime
            regime = _classify_regime(history_df if history_df is not None else row.to_frame().T)
        except Exception:
            regime = "mixed"

        return MarketContextSnapshot(
            asset=asset,
            timeframe=timeframe,
            regime=regime,
            trend_strength=round(trend_strength, 1),
            volatility=round(volatility, 1),
            liquidity=50.0,  # neutral placeholder -- no per-asset liquidity/spread feed exists in market.py yet
            volume_quality=round(min(100.0, vol_strength), 1),
            candle_quality=round(candle_quality, 1),
            noise_level=round(noise_level, 1),
            session_strength=round(session_strength, 1),
            historical_stability=round(historical_stability, 1),
        )

    # -- introspection / observability ----------------------------------------- #
    def get_stats(self) -> Dict[str, object]:
        """Read-only snapshot for a future /api/market-context/stats
        endpoint or debugging. No side effects."""
        return {
            "enabled": self.cfg.enabled,
            "tracked_keys": len(self._metrics),
            "observations_total": self._observations_total,
            "adaptive_served": self._adaptive_served,
            "fallbacks_served": self._fallbacks_served,
            "errors_total": self._errors_total,
            "duplicates_skipped": self._duplicates_skipped,
            "keys_evicted": self._keys_evicted,
            # Capped: this previously serialised EVERY tracked key, so the
            # payload grew with the asset universe -- fine in a shell,
            # not fine behind an HTTP endpoint. Hottest keys first (dict
            # order is LRU, so the tail is the most recently observed).
            "sample_counts": {
                f"{a}/{tf}/{m}": st.sample_count
                for (a, tf, m), st in list(self._metrics.items())[
                    -self.cfg.stats_sample_limit:
                ]
            },
            "sample_counts_truncated": max(
                0, len(self._metrics) - self.cfg.stats_sample_limit
            ),
        }

    def reset(self, asset: Optional[str] = None, timeframe: Optional[str] = None) -> None:
        """Clears tracked history, optionally scoped to one asset and/or
        timeframe (e.g. after switching broker accounts, where old rolling
        stats no longer describe the new account's assets). No arguments
        clears everything."""
        if asset is None and timeframe is None:
            self._metrics.clear()
            return
        for key in list(self._metrics.keys()):
            a, tf, _m = key
            if (asset is None or a == asset) and (timeframe is None or tf == timeframe):
                del self._metrics[key]
