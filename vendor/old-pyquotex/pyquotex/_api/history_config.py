"""Configuration for the historical-candle pipeline.

Every tunable the history loader uses lives here, so concurrency,
retry, timeout, circuit-breaker and cache behaviour are configured in
one place instead of being hardcoded across call sites.

Nothing in this module touches trading logic. It only describes how
history is fetched and validated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Hard bounds on worker concurrency. The lower bound keeps a
# misconfiguration from producing a pool that can never make progress;
# the upper bound is the WebSocket-protection ceiling -- more parallel
# `history/load` frames than this against one socket is how the broker
# starts dropping or throttling responses.
MIN_WORKERS = 1
MAX_WORKERS = 32


class HistoryConfigError(ValueError):
    """Raised for an invalid HistoryConfig value.

    A distinct type so callers can tell "you configured this wrong"
    apart from a runtime data error.
    """


@dataclass
class HistoryConfig:
    """Tunables for the historical-candle pipeline.

    Defaults are chosen to reproduce the pre-refactor behaviour exactly
    where behaviour was already sane (``max_workers=5``,
    ``candles_per_chunk=200``, ``throttle_seconds=0.1``), so an existing
    deployment that never touches this config sees the same request
    pattern it saw before -- only with retries, validation and cleanup
    added around it.
    """

    # -- concurrency ------------------------------------------------- #
    max_workers: int = 5
    candles_per_chunk: int = 200        # candles per history/load frame

    # -- retry ------------------------------------------------------- #
    max_retries: int = 3                # TOTAL attempts per chunk, not retries after the first
    retry_base_delay: float = 0.5       # attempt N waits base * (factor ** (N-1)) ...
    retry_backoff_factor: float = 2.0
    retry_max_delay: float = 8.0        # ... capped here
    retry_jitter: float = 0.3           # +/- this fraction, so parallel workers don't retry in lockstep

    # -- adaptive timeout -------------------------------------------- #
    # Attempt N gets base_timeout * (timeout_scale ** (N-1)), capped.
    # Resets to base on the next success -- a slow patch must not
    # permanently slow down healthy traffic.
    timeout_scale: float = 1.5
    timeout_max_multiplier: float = 3.0

    # -- adaptive throttling ----------------------------------------- #
    # Healthy traffic is NOT delayed beyond throttle_seconds. Delay only
    # grows after failures and decays back on success.
    throttle_seconds: float = 0.1
    throttle_max_seconds: float = 2.0
    throttle_growth: float = 2.0
    throttle_decay: float = 0.5

    # -- circuit breaker (instance-scoped) --------------------------- #
    breaker_enabled: bool = True
    breaker_failure_threshold: int = 8
    breaker_cooldown_seconds: float = 20.0

    # -- cache -------------------------------------------------------- #
    # Instance-scoped, off by default -- get_candles(use_cache=True)
    # opts in, exactly as before.
    cache_maxsize: int = 128
    cache_ttl: float = 10.0

    # -- validation tolerances ---------------------------------------- #
    # Spacing within +/- this fraction of the period counts as normal.
    spacing_tolerance: float = 0.25
    # A gap larger than this multiple of the period is *reported* as a
    # gap. It is never filled and never causes valid candles to be
    # discarded -- real market gaps (weekends, halts) are legitimate.
    gap_multiplier: float = 1.5
    # Candles this far beyond now are impossible, not clock skew.
    # Generous enough that a currently-forming candle is never rejected.
    future_tolerance_seconds: float = 120.0
    # Timestamps below this are not plausible market data (2001-09-09).
    min_plausible_timestamp: int = 1_000_000_000

    # -- observability ------------------------------------------------ #
    metrics_latency_samples: int = 512   # bounded ring buffer for percentiles
    log_healthy_requests: bool = False   # keep healthy operation quiet

    # -- event registry ----------------------------------------------- #
    # Belt-and-braces cap on how many per-request events one history
    # operation may leave behind if cleanup somehow fails.
    max_tracked_events: int = 4096

    _validated: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> "HistoryConfig":
        """Fail loudly and specifically rather than misbehaving later."""
        if not isinstance(self.max_workers, int) or isinstance(self.max_workers, bool):
            raise HistoryConfigError(
                f"max_workers must be an int, got {type(self.max_workers).__name__}"
            )
        if not MIN_WORKERS <= self.max_workers <= MAX_WORKERS:
            raise HistoryConfigError(
                f"max_workers must be between {MIN_WORKERS} and {MAX_WORKERS}, "
                f"got {self.max_workers}"
            )
        if self.candles_per_chunk < 1:
            raise HistoryConfigError("candles_per_chunk must be >= 1")
        if self.max_retries < 1:
            raise HistoryConfigError("max_retries is the TOTAL attempt count and must be >= 1")
        if self.retry_base_delay < 0:
            raise HistoryConfigError("retry_base_delay must be >= 0")
        if self.retry_backoff_factor < 1.0:
            raise HistoryConfigError("retry_backoff_factor must be >= 1.0")
        if self.retry_max_delay < 0:
            raise HistoryConfigError("retry_max_delay must be >= 0")
        if not 0.0 <= self.retry_jitter < 1.0:
            raise HistoryConfigError("retry_jitter must be in [0.0, 1.0)")
        if self.timeout_scale < 1.0:
            raise HistoryConfigError("timeout_scale must be >= 1.0")
        if self.timeout_max_multiplier < 1.0:
            raise HistoryConfigError("timeout_max_multiplier must be >= 1.0")
        if self.throttle_seconds < 0:
            raise HistoryConfigError("throttle_seconds must be >= 0")
        if self.throttle_max_seconds < self.throttle_seconds:
            raise HistoryConfigError("throttle_max_seconds must be >= throttle_seconds")
        if self.breaker_failure_threshold < 1:
            raise HistoryConfigError("breaker_failure_threshold must be >= 1")
        if self.breaker_cooldown_seconds <= 0:
            raise HistoryConfigError("breaker_cooldown_seconds must be > 0")
        if self.cache_maxsize < 1:
            raise HistoryConfigError("cache_maxsize must be >= 1")
        if self.cache_ttl <= 0:
            raise HistoryConfigError("cache_ttl must be > 0")
        if not 0.0 <= self.spacing_tolerance < 1.0:
            raise HistoryConfigError("spacing_tolerance must be in [0.0, 1.0)")
        if self.gap_multiplier <= 1.0:
            raise HistoryConfigError("gap_multiplier must be > 1.0")
        if self.future_tolerance_seconds < 0:
            raise HistoryConfigError("future_tolerance_seconds must be >= 0")
        if self.metrics_latency_samples < 1:
            raise HistoryConfigError("metrics_latency_samples must be >= 1")
        object.__setattr__(self, "_validated", True)
        return self

    def resolve_workers(self, max_workers: int | None) -> int:
        """`None` -> configured default; an explicit value overrides it.

        An explicit value is still clamped to [MIN_WORKERS, MAX_WORKERS]
        rather than rejected, because existing callers pass literals
        (``max_workers=3``, ``max_workers=5``) and must keep working.
        A caller passing something non-integer is a programming error
        and does raise.
        """
        if max_workers is None:
            return self.max_workers
        if not isinstance(max_workers, int) or isinstance(max_workers, bool):
            raise HistoryConfigError(
                f"max_workers must be an int or None, got {type(max_workers).__name__}"
            )
        return max(MIN_WORKERS, min(MAX_WORKERS, max_workers))

    def attempt_timeout(self, base_timeout: float, attempt: int) -> float:
        """Adaptive per-attempt timeout, bounded."""
        multiplier = min(
            self.timeout_scale ** max(0, attempt - 1), self.timeout_max_multiplier
        )
        return float(base_timeout) * multiplier

    def retry_delay(self, attempt: int, rand: float = 0.5) -> float:
        """Exponential backoff with jitter, bounded.

        `rand` is injected (0..1) so tests are deterministic.
        """
        raw = self.retry_base_delay * (self.retry_backoff_factor ** max(0, attempt - 1))
        capped = min(raw, self.retry_max_delay)
        if self.retry_jitter <= 0:
            return capped
        spread = capped * self.retry_jitter
        return max(0.0, capped - spread + (2 * spread * rand))


#: Process-wide default. Instances may override via
#: ``Quotex(...).history_config = HistoryConfig(...)``.
DEFAULT_HISTORY_CONFIG = HistoryConfig()

__all__ = [
    "HistoryConfig",
    "HistoryConfigError",
    "DEFAULT_HISTORY_CONFIG",
    "MIN_WORKERS",
    "MAX_WORKERS",
]
