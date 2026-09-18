"""Structured logging setup.

Previously nothing in this codebase ever called logging.basicConfig() or
configured any handler/formatter. In practice that means:
  - Python's logging module defaults to WARNING level with no handler until
    something configures it, so most logger.info()/logger.debug() calls
    throughout orchestrator.py and the engine/ modules (reconnect status,
    revalidation outcomes, precision-gate rejections, circuit-breaker trips,
    etc.) were silently dropped in production rather than actually reaching
    a log file or stdout in a useful form.
  - What did get through had no consistent structure (asset, user_id,
    latency_ms as separate fields), making it hard to query/alert on in any
    log aggregation tool (journalctl, Loki, CloudWatch, etc.) for real 24x7
    monitoring.

This module is additive only — it doesn't change what any log CALL site
says, only how those calls are formatted and where they go. Call
configure_logging() once, as early as possible in the process (main.py's
module scope, before the FastAPI app object is even built), so every
logger created afterwards (including engine module-level loggers) inherits
it via the root logger.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback


class JSONFormatter(logging.Formatter):
    """One JSON object per line — easy to pipe into any log aggregator
    without a custom parser, and easy to grep/jq locally too."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Common extras this codebase's log calls tend to pass via %s
        # formatting already end up inside `msg` above; explicit `extra=`
        # kwargs (if a call site ever adds them) surface as real fields here
        # rather than being silently dropped.
        for key, value in record.__dict__.items():
            if key in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "taskName",
            ):
                continue
            try:
                json.dumps(value)  # only include JSON-serializable extras
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)
        if record.exc_info:
            payload["exc_info"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable single-line format for local dev (LOG_FORMAT=plain)."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


_configured = False


def configure_logging() -> None:
    """Idempotent — safe to call more than once (e.g. from tests). Reads:
      LOG_LEVEL  (default INFO)
      LOG_FORMAT (default json; set to "plain" for readable local dev logs)
    """
    global _configured
    if _configured:
        return
    _configured = True

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt_name = os.environ.get("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter() if fmt_name == "json" else PlainFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    # Replace any handlers a library may have already attached (e.g.
    # uvicorn's own root config) so there's exactly one, consistent output
    # instead of duplicate/mismatched log lines.
    root.handlers = [handler]

    # uvicorn's access/error loggers propagate to root by default once we've
    # taken over root's handlers above; explicitly quiet the very chatty
    # access log to WARNING so normal request logging doesn't drown out
    # trading-path logs, while still surfacing 4xx/5xx.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """The one convention every structured log call in this codebase
    should use from here on: `event` is a short, stable, machine-queryable
    name (e.g. "signal_rejected", "adaptive_threshold_applied",
    "trade_executed") -- NOT a sentence -- and every piece of context goes
    in **fields, which JSONFormatter surfaces as top-level JSON keys via
    `extra=`.

    Cheap when disabled: this calls logger.isEnabledFor() before building
    the extra dict, so a DEBUG-level call site costs one integer comparison
    in production with LOG_LEVEL=INFO -- no string formatting, no dict
    construction, no measurable execution-path overhead. This is why every
    call site should route through this helper (or at least guard with
    isEnabledFor itself) rather than calling logger.debug(...) with
    eagerly-computed arguments directly.
    """
    if not logger.isEnabledFor(level):
        return
    fields["event"] = event
    logger.log(level, event, extra=fields)
