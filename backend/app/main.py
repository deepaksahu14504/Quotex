"""FastAPI application: REST control plane + WebSocket live stream.

Multi-user: every route below (except /api/auth/*) requires a valid Bearer
token and operates on *that user's* Orchestrator instance via the registry.
No shared/global trading state.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from .logging_setup import configure_logging
configure_logging()
logger = logging.getLogger("qat.main")

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .auth import get_current_user, get_user_from_ws, require_admin, router as auth_router
from .config import RuntimeSettings, read_env_file, settings as env_settings, update_env_file
from .engine.backtest import BacktestResult, run_backtest
from .engine.pattern_risk_validation import compute_validation_report
from .engine.rejection_reconciliation import compute_rejection_report
from .engine.settings_advisor import build_recommendations, is_applyable
from .engine.trade_engine import OrderUnreconciled
from .engine.task_supervisor import TaskSupervisor, asyncio_task_count_monitor, event_loop_lag_monitor
from .orchestrator import HTF_MAP, WATCHDOG_INTERVAL, Orchestrator
from .orchestrator_registry import registry
from .schemas import Candle, now_ts
from .session_manager import session_manager
from .ws_hub import hub


_process_supervisor = TaskSupervisor(owner_label="process")


async def _restore_session_safely(user_id: str) -> None:
    """Startup session-restore used to be a bare fire-and-forget
    `asyncio.create_task(registry.get_or_create(user_id))` with nothing
    ever looking at the task's result. If construction/start failed for
    any reason -- a corrupted per-user settings file, a stale or
    undecryptable session, anything -- that user's orchestrator silently
    never started: no log line anywhere, no retry, no visible symptom
    other than that user's bot never running after a backend restart.
    get_or_create() itself now logs failures with full context (see
    orchestrator_registry.py); this wrapper's job is to make sure that
    failure can't vanish into an unretrieved task exception, and to retry
    a few times, since the most common real-world trigger -- Postgres not
    yet accepting connections in the first second after a coordinated
    reboot -- is transient.
    """
    delay = 2.0
    for attempt in range(1, 4):
        try:
            await registry.get_or_create(user_id)
            return
        except Exception:
            logger.exception(
                "[startup] Session restore attempt %d/3 failed for user %s",
                attempt, user_id,
            )
            if attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Off the event loop: init_db() runs Alembic synchronously, so calling it
    # inline blocked the loop for the whole migration and uvicorn could never
    # report "Application startup complete". Bounded above by lock_timeout;
    # bounded here too so a wedged migration cannot hold startup open past
    # the point where the failure should be visible.
    try:
        await asyncio.wait_for(asyncio.to_thread(db.init_db), timeout=360)
    except asyncio.TimeoutError:
        logger.error(
            "Database migration did not finish within 360s -- aborting startup. "
            "See the migration log line above for the underlying cause."
        )
        raise
    # Restore sessions: any user with a still-valid (or re-loginable) broker
    # session gets their Orchestrator started in the background, so a backend
    # restart / VPS reboot doesn't force everyone to log in again.
    for user_id in session_manager.all_user_ids_with_sessions():
        if session_manager.has_restorable_session(user_id):
            asyncio.create_task(_restore_session_safely(user_id))
    # Process-wide reliability monitors. Not per-user -- these observe the
    # one shared event loop/process, so one instance covers every user's
    # orchestrator at once; running a copy per user would just be N
    # monitors measuring the same thing.
    lag_task = _process_supervisor.supervise("event_loop_lag", event_loop_lag_monitor)
    task_count_task = _process_supervisor.supervise("task_count", asyncio_task_count_monitor)
    try:
        yield
    finally:
        _process_supervisor.stop()
        lag_task.cancel()
        task_count_task.cancel()
        await registry.stop_all()
        # Graceful shutdown: drain the PostgreSQL connection pool so no
        # connections are left open against the server after the process
        # exits (relevant for orderly redeploys/blue-green swaps where the
        # old process's connections would otherwise count against
        # max_connections until the OS reaps the socket).
        db.close_conn()


app = FastAPI(title="QuotexAutoTrader API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=env_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)


async def get_orch(user=Depends(get_current_user)) -> Orchestrator:
    """Resolves (creating if needed) the calling user's Orchestrator. This is
    the per-request replacement for the old global `O()` singleton lookup."""
    return await registry.get_or_create(user["id"])


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health():
    return {"ok": True, "version": "2.0.0"}


@app.get("/api/time")
async def server_time():
    """Server UTC epoch (seconds) used by the UI to render a synced IST clock."""
    import time as _t
    return {"utc": _t.time()}


@app.get("/api/status")
async def system_status(o: Orchestrator = Depends(get_orch)):
    """End-to-end health of every subsystem so the UI can show what's working."""
    import time as _t
    st = o.state
    h = o.health
    checks: list[dict] = []

    def add(key: str, label: str, status: str, detail: str) -> None:
        checks.append({"key": key, "label": label, "status": status, "detail": detail})

    add("backend", "Backend API", "ok", "Responding")

    add("broker", "Broker connection", "ok" if st.connected else "error",
        f"{o.provider.name} · {'demo' if st.is_demo else 'live'}" + ("" if st.connected else " · disconnected"))

    if st.otp_verifying:
        add("login", "Login / OTP", "warn", "Verifying the PIN with Quotex…")
    elif st.otp_required:
        add("login", "Login / OTP", "warn", st.otp_message or "Waiting for PIN code")

    add("account", "Account & balance", "ok" if st.connected else "error",
        f"${st.balance:.2f} {st.currency}" if st.connected else ("Awaiting OTP" if st.otp_required else "Not connected"))

    data_status, data_detail = "error", "No data"
    try:
        assets = await o.provider.get_assets()
        # True only while the latest instrument refresh was unusable and the
        # preserved last-known-good universe is being served. Sampling still
        # works (that is the point of preserving it), but this must not be
        # reported as plain HEALTHY -- the open-flags backing it are stale.
        _feed = (o.provider.get_asset_feed_status()
                 if hasattr(o.provider, "get_asset_feed_status") else {})
        _degraded = _feed.get("serving_last_known_good", False)
        open_assets = [a for a in assets if a.is_open]
        if open_assets:
            sym = open_assets[0].symbol
            candles = await o.provider.get_candles(sym, o.runtime.trading.timeframe, 10)
            if len(candles) >= 2:
                last_age = _t.time() - candles[-1].timestamp
                data_status = "ok" if last_age < 600 else "warn"
                data_detail = f"{sym.replace('_otc','')}: {len(candles)} candles, last {int(max(0, last_age))}s ago"
                if _degraded:
                    data_status = "warn"
                    data_detail += " · instrument feed degraded, using last known-good universe"
            else:
                data_status, data_detail = "warn", f"{sym}: insufficient candles"
        else:
            # Deliberately defers to the assets check below rather than
            # repeating a verdict it cannot justify: "No open assets" here
            # was the single most misleading string in the whole status
            # panel, because it was printed for broker outages too.
            data_status, data_detail = "warn", "No eligible assets to sample — see Assets feed"
    except Exception as exc:
        data_detail = f"Fetch failed: {exc}"
    add("data", "Market data feed", data_status, data_detail)

    # ASSET FEED -- five distinguishable states.
    #
    # This check used to be `len([a for a in get_assets() if a.is_open])`
    # with an `except` branch that could never fire, because get_assets()
    # swallowed every failure into an empty list. A broker timeout, a
    # malformed payload and a genuinely closed market all rendered as the
    # same quiet "0 open assets", which is exactly why a real asset-feed
    # outage was indistinguishable from a weekend.
    try:
        await o._eligible_assets()          # refreshes the cache + funnel
        funnel = o.asset_funnel()
        status_key = funnel.get("status", "never_fetched")
        total = funnel.get("total_instruments", 0)
        open_n = funnel.get("open_count", 0)
        elig = funnel.get("open_and_payout_qualified_count", 0)
        wl_n = funnel.get("whitelist_count", elig)
        ready = funnel.get("ready_count")
        final = funnel.get("final_candidate_count")
        min_payout = funnel.get("min_payout") or funnel.get("min_payout_used") or 0
        age = funnel.get("instrument_data_age_seconds")

        if status_key == "error":
            # UNAVAILABLE: no usable snapshot at all.
            add("assets", "Assets feed", "error",
                f"Asset feed unavailable — {funnel.get('last_instrument_error') or 'unknown error'}")
        elif status_key == "stale":
            # DEGRADED / RECOVERING: the latest refresh was unusable, but a
            # valid universe is preserved and still being served. Reporting
            # this as "no eligible assets" was false -- the assets are
            # there. Warn, not error: the pipeline is still working.
            why = funnel.get("last_instrument_error") or "empty instrument response"
            add("assets", "Assets feed", "warn",
                f"Instrument feed temporarily unavailable ({why}) — "
                f"serving last known-good snapshot, {age}s old · {open_n} open · "
                f"{wl_n} eligible · retrying")
        elif status_key == "never_fetched":
            add("assets", "Assets feed", "warn", "Waiting for the first instrument snapshot")
        elif total == 0:
            add("assets", "Assets feed", "warn", "Broker returned an empty instrument response")
        elif open_n == 0:
            add("assets", "Assets feed", "warn",
                f"Broker returned {total} instruments, 0 currently open")
        elif elig == 0:
            add("assets", "Assets feed", "warn",
                f"{open_n} open assets, 0 meet minimum payout {min_payout:g}%")
        elif wl_n == 0:
            add("assets", "Assets feed", "warn",
                f"{elig} eligible assets, 0 pass the asset whitelist")
        elif ready is not None and ready == 0:
            add("assets", "Assets feed", "warn",
                f"{wl_n} eligible assets, 0 READY — history warm-up pending")
        else:
            detail = f"{open_n} open · {wl_n} eligible"
            if final is not None:
                detail += f" · scanning {final}"
            add("assets", "Assets feed", "ok", detail)
        # Parser diagnostics come from ONE refresh attempt, so the two
        # numbers always describe the same thing. Previously the numerator
        # was carried over from an earlier refresh while the denominator
        # had been reset, producing the impossible "4/0 instrument rows
        # failed validation".
        rows = funnel.get("last_attempt_rows", 0)
        bad = funnel.get("last_attempt_malformed", 0)
        shortr = funnel.get("last_attempt_short_rows", 0)
        shape = funnel.get("last_attempt_shape") or []
        if bad:
            # Include a redacted shape sample. "4/4 rows failed" alone does
            # not say WHAT arrived, and that was the missing piece when this
            # was first reported from production -- it could equally be a
            # dict read as keys, four short rows, or four non-row values.
            detail = f"{bad}/{rows} instrument rows failed validation"
            if shape:
                detail += f" — got {shape[0]}"
            add("assets_parse", "Instrument parsing", "warn", detail)
        elif shortr:
            add("assets_parse", "Instrument parsing", "warn",
                f"{shortr}/{rows} rows shorter than expected — treated as closed"
                + (f" — {shape[0]}" if shape else ""))
        elif rows == 0 and status_key in ("stale", "empty", "error"):
            add("assets_parse", "Instrument parsing", "warn",
                "Broker returned an empty instrument response"
                if status_key != "error" else "Instrument response unavailable")
    except Exception as exc:
        add("assets", "Assets feed", "error", f"{type(exc).__name__}: {exc}")

    scan_age = (_t.time() - h["last_scan_at"]) if h["last_scan_at"] else None
    # BUG FIX: this used to bottom out at "warn" forever, however long the
    # scan loop had been silent -- no distinct signal for "this is actually
    # stuck", so it was easy to miss until someone noticed no new signals.
    # Now folds in pipeline_health()'s per-task verdict and escalates to
    # "error" past a minute of scan silence, matching the same threshold
    # pipeline_health() uses to decide `stuck`.
    pipeline = o.pipeline_health()
    stuck_tasks = [name for name, info in pipeline["tasks"].items() if info["stuck"]]
    if not o._running:
        add("engine", "Strategy engine", "error", "Loop not running")
    elif stuck_tasks:
        add("engine", "Strategy engine", "error", f"Stuck ({', '.join(stuck_tasks)}) — use Restart Pipeline")
    elif st.mode == "off":
        add("engine", "Strategy engine", "warn", "Mode is Off (not scanning)")
    elif scan_age is None:
        add("engine", "Strategy engine", "warn", "Warming up…")
    elif scan_age < 15:
        add("engine", "Strategy engine", "ok", f"Scanning · {h['assets_scanned']} assets")
    elif scan_age < 60:
        add("engine", "Strategy engine", "warn", f"Last scan {int(scan_age)}s ago")
    else:
        add("engine", "Strategy engine", "error", f"No scan in {int(scan_age)}s — use Restart Pipeline")

    # Pipeline supervision: makes a background loop that crashed and was
    # auto-restarted (or, worse, one that stopped ticking entirely) visible
    # here instead of only in logs -- see engine/task_supervisor.py.
    # SCANNER PROGRESS -- driven by CYCLE PROGRESS, never by signal output.
    #
    # "no signal" and "scanner stalled" are different states and must never
    # render the same. A Live Pipeline entry only appears when an asset is
    # actually evaluated to a stage, so pipeline age alone proves nothing
    # either way; cycle count and cycle age are what separate them.
    try:
        # BUG FIX: this used to bind to `st`, shadowing `st = o.state` from the
        # top of the handler. Every successful call turned `st` into a dict,
        # so the risk-guard check at the bottom (`st.paused`) raised
        # AttributeError and /api/status returned 500 on EVERY request --
        # the header dot stuck on "error", the Status modal stuck on "Loading".
        tel = o.scan_telemetry()
        state = tel["state"]
        age = tel["last_scan_age_seconds"]
        cycles = tel["scan_cycle_count"]
        blocks = tel["scan_gate_blocks"] or {}
        core = (f"cycle #{cycles}, {int(age)}s ago · "
                f"{tel['assets_evaluated']}/{tel['candidates_selected']} evaluated · "
                f"{tel['pipeline_snapshots_this_cycle']} pipeline update(s) · "
                f"{tel['signals_this_cycle']} signal(s)") if age is not None else ""

        if state == "STARTING":
            add("scan_progress", "Scan progress", "warn",
                "STARTING — no scan cycle has completed yet")
        elif state == "STALLED":
            add("scan_progress", "Scan progress", "error",
                f"STALLED — no completed scan cycle in "
                f"{int(age) if age is not None else '∞'}s (last completed cycle "
                f"#{cycles}). The scanner itself is not advancing.")
        elif state == "STOPPING":
            add("scan_progress", "Scan progress", "warn", "STOPPING — shutting down")
        elif state == "DATA_STALE":
            add("scan_progress", "Scan progress", "warn",
                f"DATA_STALE — {core}. Cycles are advancing; signals are "
                f"correctly suppressed until fresh data returns.")
        elif state == "SCANNING":
            add("scan_progress", "Scan progress", "ok", f"SCANNING — {core}")
        else:  # HEALTHY_NO_SIGNAL
            detail = f"HEALTHY_NO_SIGNAL — {core}"
            if blocks:
                top = max(blocks.items(), key=lambda kv: kv[1])
                detail += f" · top gate: {top[0]} ×{top[1]}"
            detail += " — quiet market, not stalled"
            lvl = "warn" if tel["consecutive_scan_failures"] >= 3 else "ok"
            add("scan_progress", "Scan progress", lvl, detail)

        # Explain the funnel so "52 eligible -> 9 selected" does not read as
        # 43 assets vanishing.
        by_ready = tel["candidates_excluded_by_readiness"]
        by_cap = tel["candidates_excluded_by_cap"]
        by_payout = tel.get("instruments_excluded_by_payout", 0)
        if by_ready or by_payout or by_cap:
            add("candidate_funnel", "Candidate selection", "ok",
                f"{tel['candidates_available']} eligible (−{by_payout} below min payout) → "
                f"{tel['candidates_selected']} scanned "
                f"(−{by_ready} not READY, −{by_cap} beyond top-N)")
    except Exception as exc:
        add("scan_progress", "Scan progress", "warn", f"{type(exc).__name__}: {exc}")

    supervisor_snap = o._supervisor.snapshot()
    total_restarts = sum(s["restart_count"] for s in supervisor_snap.values())
    stuck_loops = o._supervisor.stuck_names()
    watchdog_age = (_t.time() - h["watchdog_last_run_at"]) if h.get("watchdog_last_run_at") else None
    # MERGE FIX: watchdog_last_run_at starts at 0.0, so a freshly-started
    # orchestrator reported a hard "error" here for its first few seconds --
    # a red card on every restart, which trains people to ignore this check.
    # A watchdog that has genuinely never ticked is only a problem once it
    # has had time to; before that it's just "starting up".
    uptime = _t.time() - (h.get("started_at") or _t.time())
    if stuck_loops:
        add("supervision", "Pipeline supervision", "error",
            f"Gave up auto-restarting: {', '.join(stuck_loops)} — use Restart Pipeline")
    elif watchdog_age is None and uptime < WATCHDOG_INTERVAL * 5:
        add("supervision", "Pipeline supervision", "warn", "Starting up…")
    elif watchdog_age is None or watchdog_age > WATCHDOG_INTERVAL * 5:
        add("supervision", "Pipeline supervision", "error",
            "Watchdog has not run recently — reconnect/stale-data detection may be stalled")
    elif total_restarts > 0:
        recent = ", ".join(f"{name} x{s['restart_count']}" for name, s in supervisor_snap.items() if s["restart_count"] > 0)
        add("supervision", "Pipeline supervision", "warn", f"Auto-recovered background task crash(es): {recent}")
    else:
        add("supervision", "Pipeline supervision", "ok", "All background loops running, no restarts")

    # Market history warm-up: the READY gate in front of the scan loop. An
    # asset that never leaves LOADING/RETRYING is invisible in every other
    # check here (nothing crashes, the scan loop just has fewer candidates),
    # which is exactly how the original "assets stuck without warm
    # indicators" bug hid for so long.
    try:
        mi = o.market_init.snapshot()
        by_state = mi.get("by_state", {})
        ready = by_state.get("ready", 0)
        failed = by_state.get("failed", 0)
        pending = by_state.get("loading", 0) + by_state.get("retrying", 0) + by_state.get("unknown", 0)
        total = ready + failed + pending
        if mi.get("any_task_stuck"):
            add("market_init", "Market history warm-up", "error",
                "Warm-up workers stopped auto-recovering — use Restart Pipeline")
        elif total == 0:
            add("market_init", "Market history warm-up", "warn", "Waiting for connection…")
        elif ready == 0:
            # Say WHY. "0 ready, 47 failed" alone cannot distinguish a dead
            # broker connection from a candle threshold set higher than this
            # broker will serve -- and the fix for each is the opposite of
            # the other.
            need = mi.get("required_candles", 0)
            best = mi.get("best_candle_count", 0)
            reasons = mi.get("top_failure_reasons") or []
            if need and best and best < need:
                add("market_init", "Market history warm-up", "error",
                    f"No assets READY — the broker is returning at most {best} candles but "
                    f"{need} are required. Either history requests are failing, or lower "
                    f"'Min candles for a signal' in Settings to at most {best}.")
            else:
                detail = f" · {reasons[0]}" if reasons else ""
                add("market_init", "Market history warm-up", "error",
                    f"No assets READY ({pending} loading, {failed} failed) — scanner has "
                    f"nothing to evaluate{detail}")
        elif failed:
            add("market_init", "Market history warm-up", "warn",
                f"{ready} ready · {failed} failed (auto-retrying) · {pending} loading")
        else:
            add("market_init", "Market history warm-up", "ok", f"{ready}/{total} assets ready")
    except Exception as exc:
        add("market_init", "Market history warm-up", "warn", str(exc))

    # Separate from the warm-up check above: an asset can be fully READY and
    # still be sitting at a broker-imposed history ceiling below what was
    # configured. Without this line that state was invisible -- the only
    # symptom was scan cycles quietly slowing down over time as more assets
    # crossed the ceiling and each ate a repeated ~150s dead-end seed attempt.
    try:
        plateau = o.provider.seed_plateau_status() if hasattr(o.provider, "seed_plateau_status") else {}
        if plateau.get("plateaued_count"):
            add("history_ceiling", "Broker history ceiling", "info",
                f"{plateau['plateaued_count']} asset/timeframe pair(s) capped at what the "
                f"broker actually delivers — accepted for now, growing slowly from live "
                f"ticks, full re-check every 30 min.")
    except Exception:
        pass

    add("ws", "Live UI link", "ok" if hub.client_count(o.user_id) > 0 else "warn", f"{hub.client_count(o.user_id)} client(s)")

    tg = o.runtime.telegram
    has_token = bool(tg.bot_token or env_settings.telegram_bot_token)
    has_chat = bool(tg.chat_id or env_settings.telegram_chat_id)
    tg_status = o.telegram.status()
    if not tg.enabled:
        add("telegram", "Telegram", "info", "Disabled")
    elif not (has_token and has_chat):
        add("telegram", "Telegram", "warn", "Enabled but missing token/chat ID")
    elif tg_status.get("last_error") and not tg_status.get("sent_count"):
        # Configured, but Telegram has rejected everything we have sent. This
        # used to render as a green "Enabled & configured" tick.
        add("telegram", "Telegram", "error", f"Delivery failing: {tg_status['last_error']}")
    elif tg_status.get("last_error"):
        add("telegram", "Telegram", "warn",
            f"{tg_status['sent_count']} sent · last error: {tg_status['last_error']}")
    else:
        add("telegram", "Telegram", "ok", f"Enabled · {tg_status.get('sent_count', 0)} sent")

    te = o.trade_engine.status()
    if te.get("blocked"):
        add("execution", "Order execution", "error",
            f"BLOCKED — order {te['unreconciled_signal_id']} outcome unknown for "
            f"{te['unreconciled_age_seconds']}s. It was not retried. Verify the position at the "
            f"broker, then clear it.")
    elif not te.get("running"):
        add("execution", "Order execution", "warn", "Execution queue not running")
    elif te.get("timeout_count"):
        add("execution", "Order execution", "warn",
            f"{te['executed_count']} placed · {te['timeout_count']} timed out (all resolved)")
    else:
        add("execution", "Order execution", "ok",
            f"{te['executed_count']} placed · {te['queued']} queued")

    if st.paused:
        add("risk", "Risk guard", "warn", st.pause_reason or "Paused")
    else:
        add("risk", "Risk guard", "ok", f"{st.mode} · {st.trades_today} trades today")

    statuses = [c["status"] for c in checks]
    overall = "error" if "error" in statuses else "warn" if "warn" in statuses else "ok"
    return {"overall": overall, "checks": checks, "uptime": int(_t.time() - h["started_at"])}


@app.get("/api/state")
async def get_state(o: Orchestrator = Depends(get_orch)):
    return o.state_dict()


@app.get("/api/shadow/stats")
async def get_shadow_stats(o: Orchestrator = Depends(get_orch)):
    """Shadow Mode results -- what would have happened, simulated against
    live prices, no real orders placed. Empty/zeroed out until
    shadow_mode_enabled is on and trades have had time to resolve."""
    return o.shadow_store.stats()


@app.get("/api/settings")
async def get_settings(o: Orchestrator = Depends(get_orch)):
    return o.runtime.model_dump()


@app.get("/api/broker/session")
async def get_broker_session(user=Depends(get_current_user)):
    """Broker (Quotex) session status for this user — separate from app login."""
    info = session_manager.get_session_info(user["id"])
    if not info:
        return {"connected": False, "has_session": False}
    return {
        "connected": True,
        "has_session": True,
        "provider": info.provider,
        "account_type": info.account_type,
        "login_at": info.login_at,
        "expires_at": info.expires_at,
        "is_stale": info.is_stale,
        "has_ssid": info.has_ssid,
    }


@app.put("/api/broker/credentials")
async def put_broker_credentials(payload: dict, user=Depends(get_current_user)):
    """Store this user's own Quotex email/password (encrypted at rest) and
    reconnect their orchestrator with the new credentials.

    Important: this also forces runtime.provider to "pyquotex". Without this,
    a user who saved credentials here without first also saving the "Data
    provider" dropdown in Settings would have their orchestrator quietly stay
    on whatever provider it already had (often "paper"/demo data) — the
    Quotex login would never even be attempted, no OTP prompt, no error,
    just silence. This makes credential-save unconditionally mean "connect
    me to Quotex now."
    """
    # BUG FIX: Validate input types and lengths to prevent injection
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Invalid payload format"}, status_code=400)
    
    email = (payload.get("quotex_email") or "").strip()
    password = payload.get("quotex_password") or ""
    is_demo = bool(payload.get("quotex_is_demo", True))
    
    if not email or not password:
        return JSONResponse({"error": "quotex_email and quotex_password are required"}, status_code=400)
    
    # BUG FIX: Enforce reasonable length limits on credentials
    if len(email) > 255 or len(password) > 1024:
        return JSONResponse({"error": "Email or password too long"}, status_code=400)
    
    if not isinstance(email, str) or not isinstance(password, str):
        return JSONResponse({"error": "Invalid credential types"}, status_code=400)
    
    session_manager.save_credentials(user["id"], email, password, is_demo)
    session_manager.clear_session_token(user["id"])
    o = await registry.get_or_create(user["id"])
    o.runtime.provider = "pyquotex"
    o.runtime.trading.is_demo = is_demo
    o.runtime.save(user["id"])
    await o.switch_provider()
    return {"ok": True, "connected": o.state.connected, "otp_required": o.state.otp_required}


def _env_view() -> dict:
    e = read_env_file()
    return {
        "telegram_bot_token_set": bool(env_settings.telegram_bot_token or e.get("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_id": env_settings.telegram_chat_id or e.get("TELEGRAM_CHAT_ID", "") or "",
        "host": e.get("HOST", env_settings.host),
        "port": int(e.get("PORT", env_settings.port) or env_settings.port),
        "cors_origins": e.get("CORS_ORIGINS", env_settings.cors_origins),
    }


@app.get("/api/env")
async def get_env(_user=Depends(get_current_user)):
    """Server-level config (secrets masked — never returned). Per-user broker
    credentials now live under /api/broker/credentials instead."""
    return _env_view()


@app.put("/api/env")
async def put_env(payload: dict, o: Orchestrator = Depends(get_orch), _admin=Depends(require_admin)):
    """Save server-level .env values (host/port/cors/telegram) and apply live.
    Restart required only for host/port/cors changes.

    SECURITY FIX: this writes shared, global config (host/port/CORS/Telegram
    bot token) that affects every user on the deployment, via a single
    process-wide .env file -- previously any authenticated user could call
    it, not just an operator/admin. Now requires is_admin=1 on the calling
    account (see auth.require_admin for how to grant it)."""
    # BUG FIX: Validate payload type and structure
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Invalid payload format"}, status_code=400)
    
    updates: dict = {}
    restart_required = False

    if "telegram_chat_id" in payload:
        val = (payload.get("telegram_chat_id") or "").strip()
        if len(val) > 255:
            return JSONResponse({"error": "Telegram chat ID too long"}, status_code=400)
        updates["TELEGRAM_CHAT_ID"] = val
        env_settings.telegram_chat_id = val or None
    
    if payload.get("telegram_bot_token"):
        val = str(payload["telegram_bot_token"]).strip()
        if len(val) > 1024:
            return JSONResponse({"error": "Telegram bot token too long"}, status_code=400)
        updates["TELEGRAM_BOT_TOKEN"] = val
        env_settings.telegram_bot_token = val
    
    for key, env_key in (("host", "HOST"), ("port", "PORT"), ("cors_origins", "CORS_ORIGINS")):
        if key in payload and str(payload[key]).strip() != "":
            val = str(payload[key]).strip()
            # BUG FIX: Validate host/port values
            if env_key == "PORT":
                try:
                    port = int(val)
                    if port < 1 or port > 65535:
                        return JSONResponse({"error": "Invalid port number"}, status_code=400)
                except ValueError:
                    return JSONResponse({"error": "Port must be a number"}, status_code=400)
            elif env_key == "HOST":
                if len(val) > 255 or not all(c.isalnum() or c in '.-' for c in val):
                    return JSONResponse({"error": "Invalid host format"}, status_code=400)
            updates[env_key] = val
            restart_required = True

    if updates:
        update_env_file(updates)

    o._configure_telegram()

    return {**_env_view(), "restart_required": restart_required}


@app.put("/api/settings")
async def put_settings(payload: dict, o: Orchestrator = Depends(get_orch)):
    try:
        new = RuntimeSettings.model_validate(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    o.apply_settings(new)
    await o.broadcast_state()
    return o.runtime.model_dump()


@app.get("/api/assets")
async def get_assets(o: Orchestrator = Depends(get_orch)):
    assets = await o.provider.get_assets()
    return [a.model_dump() for a in assets]


@app.get("/api/candles")
async def get_candles(asset: str, timeframe: str = "1m", count: int = 120, o: Orchestrator = Depends(get_orch)):
    candles = await o.provider.get_candles(asset, timeframe, count)
    return [c.model_dump() for c in candles]


@app.get("/api/scan-telemetry")
async def scan_telemetry(o: Orchestrator = Depends(get_orch)):
    """Per-cycle scanner state. 'Pipeline supervision: OK' only proves the
    task object is alive; this proves cycles are actually completing, and
    shows which gate consumed a cycle that produced no signal."""
    return o.scan_telemetry()


@app.get("/api/asset-universe")
async def asset_universe_status(o: Orchestrator = Depends(get_orch)):
    """Why the scanner is seeing N assets: the full reduction funnel plus
    instrument-fetch health, so a zero-candidate state can be attributed
    without reproducing the run."""
    await o._eligible_assets()
    return o.asset_funnel()


@app.get("/api/market-init")
async def market_init_status(o: Orchestrator = Depends(get_orch)):
    """Asset warm-up state from MarketInitManager: which symbols have
    loaded validated, warm history and are eligible for the scan loop,
    which are still LOADING/RETRYING, and which are FAILED and waiting on
    the 60s background recovery sweep."""
    return o.market_init.snapshot()


@app.get("/api/signals")
async def get_signals(limit: int = 30, o: Orchestrator = Depends(get_orch)):
    sigs = sorted(o.signals.values(), key=lambda s: s.created_at, reverse=True)[:limit]
    # Refresh the plan for signals that can still be entered: entry countdown,
    # stake (balance/martingale may have moved) and blocked_reason are all
    # time-dependent, so serving the value frozen at creation would show a
    # countdown that never ticks and a reason that may no longer be true.
    for sig in sigs:
        if sig.status in ("new", "approved"):
            try:
                sig.trade_plan = o.build_trade_plan(sig)
            except Exception:
                logger.exception("[trade-plan] refresh failed for %s", sig.id)
    return [s.model_dump() for s in sigs]


@app.get("/api/signals/{signal_id}/plan")
async def get_signal_plan(signal_id: str, o: Orchestrator = Depends(get_orch)):
    """The exact trade auto-trade would place for this signal — entry time,
    expiry, stake, payout, and whether it will fire (with the reason if not).
    Same code path the executor uses, so manual entries can mirror the bot."""
    sig = o.signals.get(signal_id)
    if not sig:
        return JSONResponse({"error": "Unknown signal"}, status_code=404)
    return o.build_trade_plan(sig).model_dump()


@app.get("/api/strategies/performance")
async def strategies_performance(o: Orchestrator = Depends(get_orch)):
    return o.strategy_manager.status_for_ui(o.runtime.trading.enabled_strategies)


@app.get("/api/calibration")
async def calibration_status(o: Orchestrator = Depends(get_orch)):
    if o.calibrator.needs_refresh(len(o.store.all())):
        o.calibrator.refresh(o.store.all())
    return o.calibrator.status_for_ui()


@app.get("/api/regime/status")
async def regime_status(o: Orchestrator = Depends(get_orch)):
    """Current detected regime per asset, from the most recent scan."""
    return {"regimes": o.live_regime}


@app.get("/api/regime/performance")
async def regime_performance(o: Orchestrator = Depends(get_orch)):
    if o.regime_tracker.needs_refresh(len(o.store.all())):
        o.regime_tracker.refresh(o.store.all())
    return o.regime_tracker.status_for_ui(o.runtime.trading.enabled_strategies)


@app.post("/api/strategies/{name}/override")
async def strategies_set_override(name: str, payload: dict, o: Orchestrator = Depends(get_orch)):
    override = payload.get("override")
    if override not in ("active", "muted", None):
        return JSONResponse({"error": "override must be 'active', 'muted', or null"}, status_code=400)
    o.strategy_manager.set_override(name, override)
    return {"ok": True, "status": next(iter(o.strategy_manager.status_for_ui([name])))}


@app.get("/api/trades")
async def get_trades(limit: int = 100, o: Orchestrator = Depends(get_orch)):
    return [t.model_dump() for t in o.store.recent(limit)]


@app.delete("/api/trades")
async def delete_all_trades(o: Orchestrator = Depends(get_orch)):
    n = o.store.clear()
    return {"ok": True, "deleted": n}


@app.delete("/api/trades/{trade_id}")
async def delete_trade(trade_id: str, o: Orchestrator = Depends(get_orch)):
    ok = o.store.remove(trade_id)
    return {"ok": ok}


@app.post("/api/trades/delete")
async def delete_selected_trades(payload: dict, o: Orchestrator = Depends(get_orch)):
    ids = payload.get("ids") or []
    removed = o.store.remove_many(ids)
    return {"ok": True, "deleted": removed}


@app.get("/api/stats")
async def get_stats(o: Orchestrator = Depends(get_orch)):
    trades = [t for t in o.store.all() if t.status.value in ("win", "loss", "draw")]
    wins = sum(1 for t in trades if t.status.value == "win")
    losses = sum(1 for t in trades if t.status.value == "loss")
    pnl = round(sum(t.profit for t in trades), 2)
    gross_win = sum(t.profit for t in trades if t.profit > 0)
    gross_loss = abs(sum(t.profit for t in trades if t.profit < 0))
    pf = round(gross_win / gross_loss, 2) if gross_loss else None
    equity, curve = 0.0, []
    for t in trades:
        equity = round(equity + t.profit, 2)
        curve.append({"t": t.closed_at or t.created_at, "equity": equity})
    return {
        "total": len(trades), "wins": wins, "losses": losses,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
        "pnl": pnl, "profit_factor": pf, "equity_curve": curve,
    }


@app.get("/api/insights")
async def get_insights(o: Orchestrator = Depends(get_orch)):
    import time as _t
    from collections import defaultdict

    trades = [t for t in o.store.all() if t.status.value in ("win", "loss", "draw")]
    closed = [t for t in trades if t.status.value in ("win", "loss")]

    def agg():
        return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}

    by_strategy = defaultdict(agg)
    by_asset = defaultdict(agg)
    by_combo = defaultdict(agg)  # (strategy, asset) -> stats
    by_tf = defaultdict(agg)
    by_dir = {"call": agg(), "put": agg()}
    by_hour = [agg() for _ in range(24)]
    conf_buckets = {"50-65": agg(), "65-75": agg(), "75-85": agg(), "85-100": agg()}

    def add(bucket, t):
        bucket["trades"] += 1
        bucket["pnl"] = round(bucket["pnl"] + t.profit, 2)
        if t.status.value == "win":
            bucket["wins"] += 1
        elif t.status.value == "loss":
            bucket["losses"] += 1

    for t in trades:
        for s in (t.strategies or ["(none)"]):
            add(by_strategy[s], t)
            add(by_combo[(s, t.asset)], t)
        add(by_asset[t.asset], t)
        if t.timeframe:
            add(by_tf[t.timeframe], t)
        add(by_dir.get(t.direction.value, by_dir["call"]), t)
        hour = _t.localtime(t.closed_at or t.created_at).tm_hour
        add(by_hour[hour], t)
        c = t.confidence or 0
        key = "50-65" if c < 65 else "65-75" if c < 75 else "75-85" if c < 85 else "85-100"
        add(conf_buckets[key], t)

    def finalize(d, label_key):
        out = []
        for k, v in d.items():
            wl = v["wins"] + v["losses"]
            out.append({label_key: k, **v, "win_rate": round(v["wins"] / wl * 100, 1) if wl else 0.0})
        return sorted(out, key=lambda x: (x["win_rate"], x["pnl"]), reverse=True)

    cur_streak, cur_type = 0, None
    longest_win = longest_loss = run = 0
    last = None
    for t in closed:
        r = t.status.value
        run = run + 1 if r == last else 1
        if r == "win":
            longest_win = max(longest_win, run)
        else:
            longest_loss = max(longest_loss, run)
        last = r
    for t in reversed(closed):
        if cur_type is None:
            cur_type = t.status.value
        if t.status.value == cur_type:
            cur_streak += 1
        else:
            break

    wins = sum(1 for t in closed if t.status.value == "win")
    losses = len(closed) - wins
    gross_win = sum(t.profit for t in closed if t.profit > 0)
    gross_loss = abs(sum(t.profit for t in closed if t.profit < 0))
    net = round(sum(t.profit for t in trades), 2)
    best = max((t for t in trades), key=lambda t: t.profit, default=None)
    worst = min((t for t in trades), key=lambda t: t.profit, default=None)
    win_profits = [t.profit for t in closed if t.profit > 0]
    loss_profits = [t.profit for t in closed if t.profit < 0]
    avg_win = round(sum(win_profits) / len(win_profits), 2) if win_profits else 0.0
    avg_loss = round(sum(loss_profits) / len(loss_profits), 2) if loss_profits else 0.0

    strat_list = finalize(by_strategy, "name")
    asset_list = finalize(by_asset, "asset")
    rated_strats = [s for s in strat_list if s["trades"] >= 3] or strat_list
    best_strategy = max(rated_strats, key=lambda x: x["win_rate"], default=None)
    worst_strategy = min(rated_strats, key=lambda x: x["win_rate"], default=None)
    most_profitable_asset = max(asset_list, key=lambda x: x["pnl"], default=None)
    least_profitable_asset = min(asset_list, key=lambda x: x["pnl"], default=None)

    combo_list = []
    for (strat, sym), v in by_combo.items():
        wl = v["wins"] + v["losses"]
        combo_list.append({
            "strategy": strat, "asset": sym, **v,
            "win_rate": round(v["wins"] / wl * 100, 1) if wl else 0.0,
        })
    combo_list.sort(key=lambda x: (x["win_rate"], x["pnl"]), reverse=True)
    rated_combos = [c for c in combo_list if c["trades"] >= 3] or combo_list
    best_combo = max(rated_combos, key=lambda x: (x["win_rate"], x["pnl"]), default=None)
    top_combos = rated_combos[:6]

    equity, curve = 0.0, []
    for t in trades:
        equity = round(equity + t.profit, 2)
        curve.append({"t": t.closed_at or t.created_at, "equity": equity})

    return {
        "summary": {
            "total": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0.0,
            "net_pnl": net,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "expectancy": round(net / len(closed), 2) if closed else 0.0,
            "avg_confidence": round(sum(t.confidence or 0 for t in trades) / len(trades), 1) if trades else 0.0,
            "current_streak": cur_streak,
            "current_streak_type": cur_type,
            "longest_win_streak": longest_win,
            "longest_loss_streak": longest_loss,
            "best_trade": best.model_dump() if best else None,
            "worst_trade": worst.model_dump() if worst else None,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "best_strategy": best_strategy,
            "worst_strategy": worst_strategy,
            "most_profitable_asset": most_profitable_asset,
            "least_profitable_asset": least_profitable_asset,
            "best_combo": best_combo,
        },
        "top_combos": top_combos,
        "by_strategy": strat_list,
        "by_asset": asset_list[:10],
        "by_timeframe": finalize(by_tf, "timeframe"),
        "by_direction": [{"direction": k, **v, "win_rate": round(v["wins"] / (v["wins"] + v["losses"]) * 100, 1) if (v["wins"] + v["losses"]) else 0.0} for k, v in by_dir.items()],
        "by_hour": [{"hour": i, **v, "win_rate": round(v["wins"] / (v["wins"] + v["losses"]) * 100, 1) if (v["wins"] + v["losses"]) else 0.0} for i, v in enumerate(by_hour)],
        "by_confidence": [{"bucket": k, **v, "win_rate": round(v["wins"] / (v["wins"] + v["losses"]) * 100, 1) if (v["wins"] + v["losses"]) else 0.0} for k, v in conf_buckets.items()],
        "form": [t.status.value for t in closed[-20:]],
        "equity_curve": curve,
    }


class BacktestRequest(BaseModel):
    asset: str
    timeframe: Optional[str] = None            # defaults to the user's current trading timeframe
    bars: int = 2000                            # how much history to pull, in candles
    duration_seconds: Optional[int] = None       # defaults to the user's current trade duration
    confidence_threshold: Optional[int] = None   # defaults to the user's current risk setting
    enabled_strategies: Optional[List[str]] = None
    starting_balance: float = 1000.0
    multi_timeframe_confirmation: Optional[bool] = None
    payout_pct: Optional[float] = None           # defaults to the asset's live payout, or 80 if unknown


@app.post("/api/backtest/run", response_model=BacktestResult)
async def backtest_run(payload: BacktestRequest, o: Orchestrator = Depends(get_orch)):
    timeframe = payload.timeframe or o.runtime.trading.timeframe
    duration_seconds = payload.duration_seconds or o.runtime.trading.duration_seconds
    confidence_threshold = payload.confidence_threshold if payload.confidence_threshold is not None else o.runtime.risk.confidence_threshold
    enabled_strategies = payload.enabled_strategies or o.runtime.trading.enabled_strategies
    use_htf = payload.multi_timeframe_confirmation if payload.multi_timeframe_confirmation is not None else o.runtime.trading.multi_timeframe_confirmation

    try:
        candles: List[Candle] = await o.provider.fetch_history(payload.asset, timeframe, payload.bars)
    except Exception as exc:
        return JSONResponse({"error": f"Could not fetch history: {exc}"}, status_code=502)
    if not candles:
        return JSONResponse({"error": "No historical candles returned for this asset/timeframe"}, status_code=404)

    htf_candles = None
    if use_htf:
        htf_tf = HTF_MAP.get(timeframe, timeframe)
        if htf_tf != timeframe:
            try:
                htf_candles = await o.provider.fetch_history(payload.asset, htf_tf, max(payload.bars // 4, 200))
            except Exception:
                htf_candles = None

    payout_pct = payload.payout_pct
    if payout_pct is None:
        try:
            payout_pct = await o.provider.get_payout(payload.asset, timeframe)
        except Exception:
            payout_pct = 0.0
        if not payout_pct:
            assets = await o.provider.get_assets()
            match = next((a for a in assets if a.symbol == payload.asset), None)
            payout_pct = match.payout if match else 80.0

    try:
        result = await run_backtest(
            candles, asset=payload.asset, timeframe=timeframe,
            enabled_strategies=enabled_strategies, confidence_threshold=confidence_threshold,
            duration_seconds=duration_seconds, payout_pct=payout_pct,
            starting_balance=payload.starting_balance, risk_settings=o.runtime.risk,
            multi_timeframe_confirmation=bool(htf_candles), htf_candles=htf_candles,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return result


@app.post("/api/signals/{signal_id}/execute")
async def execute_signal(signal_id: str, o: Orchestrator = Depends(get_orch)):
    """Manual entry. Every refusal now names its actual cause.

    This used to answer every single failure with the same sentence --
    "Signal not executable (risk gate or stale)" -- whether the entry window
    had closed, the risk gate had tripped, the asset already had an open
    trade, or the execution queue was blocked on an unreconciled order. The
    UI then discarded even that, so pressing Enter Trade looked like it did
    nothing at all."""
    sig = o.signals.get(signal_id)
    if not sig:
        return JSONResponse({"error": "Unknown signal — it may have been cleared from memory."},
                            status_code=404)

    if sig.status not in ("new", "approved"):
        return JSONResponse(
            {"error": f"This signal is already '{sig.status}' — only a new signal can be entered."},
            status_code=409)

    if sig.expires_at and now_ts() > sig.expires_at:
        return JSONResponse(
            {"error": f"Entry window closed {int(now_ts() - sig.expires_at)}s ago. "
                      f"The setup this signal was based on is no longer current."},
            status_code=409)

    # The trade plan already computes exactly why auto-trade would refuse. Mode
    # is the one reason that does NOT apply here -- a manual press IS the
    # override for manual mode.
    try:
        plan = o.build_trade_plan(sig)
        if plan.blocked_reason and not plan.blocked_reason.startswith("Mode is"):
            return JSONResponse({"error": plan.blocked_reason}, status_code=409)
    except Exception:
        logger.exception("[execute] could not build a plan for %s", signal_id)

    try:
        trade = await o.execute_signal(signal_id)
    except OrderUnreconciled as exc:
        return JSONResponse(
            {"error": f"{exc} — the order was NOT retried. Check the position at your broker "
                      f"before trading again."},
            status_code=409)
    except Exception as exc:
        logger.exception("[execute] manual execution failed for %s", signal_id)
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    if not trade:
        # Name the actual cause. This used to answer with one sentence listing
        # every possibility ("a duplicate request, or the broker did not
        # respond in time"), which told the user nothing and hid the one case
        # that matters most -- an order still in flight, which must NOT be
        # retried by pressing the button again.
        # Order matters: the executor's own reason is the most specific thing
        # we know ("payout dropped", "risk gate: ...", "setup no longer valid"),
        # so it wins over the queue-level ones and over the generic fallback
        # that was all the UI could show before.
        reason = (
            getattr(o, "_last_execution_refusal", None)
            or o.trade_engine.last_refusal
            or o.trade_engine.status().get("last_error")
            or "The execution queue did not return a trade. Check Order execution in System Status."
        )
        return JSONResponse({"error": reason, "execution": o.trade_engine.status()},
                            status_code=409)
    return trade.model_dump()


@app.post("/api/signals/{signal_id}/skip")
async def skip_signal(signal_id: str, o: Orchestrator = Depends(get_orch)):
    o.skip_signal(signal_id)
    return {"ok": True}


@app.post("/api/control/mode")
async def set_mode(payload: dict, o: Orchestrator = Depends(get_orch)):
    mode = payload.get("mode")
    if mode not in ("auto", "manual", "off"):
        return JSONResponse({"error": "invalid mode"}, status_code=400)
    o.runtime.trading.mode = mode
    o.state.mode = mode
    o.runtime.save(o.user_id)
    await o.broadcast_state()
    return {"mode": mode}


@app.post("/api/control/kill")
async def kill(o: Orchestrator = Depends(get_orch)):
    o.kill_switch()
    await o.broadcast_state()
    return {"ok": True, "mode": "off"}


@app.post("/api/control/resume")
async def resume(o: Orchestrator = Depends(get_orch)):
    o.resume()
    await o.broadcast_state()
    return {"ok": True}


@app.post("/api/otp")
async def submit_otp(payload: dict, o: Orchestrator = Depends(get_orch)):
    code = str(payload.get("code", "")).strip()
    if not code:
        return JSONResponse({"error": "code required"}, status_code=400)
    ok, reason = o.submit_otp(code)
    if not ok:
        return JSONResponse({"ok": False, "error": reason}, status_code=400)
    return {"ok": True, "message": reason}


@app.post("/api/control/switch-account")
async def switch_account(payload: dict, o: Orchestrator = Depends(get_orch)):
    """Switch between live/demo/tournament. See
    Orchestrator.switch_account_mode for the full safety/isolation/
    persistence flow -- this endpoint is a thin wrapper, same pattern as
    every other /api/control/* action above."""
    account_mode = str(payload.get("account_mode", "")).strip().lower()
    tournament_id = payload.get("tournament_id")
    try:
        tournament_id = int(tournament_id) if tournament_id not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "tournament_id must be an integer"}, status_code=400)
    ok, message = await o.switch_account_mode(account_mode, tournament_id)
    if not ok:
        return JSONResponse({"error": message}, status_code=400)
    return {"ok": True, "message": message, "state": o.state.model_dump()}


# --------------------------------------------------------------------------- #
# Auto Validation Engine
# --------------------------------------------------------------------------- #
@app.post("/api/validation/run")
async def validation_run(o: Orchestrator = Depends(get_orch)):
    ok = o.validation.enqueue_manual()
    if not ok:
        return JSONResponse({"error": "Validation already running"}, status_code=409)
    return {"ok": True, "status": o.validation.progress_dict()}


@app.get("/api/validation/status")
async def validation_status(o: Orchestrator = Depends(get_orch)):
    return o.validation.progress_dict()


@app.get("/api/validation/health")
async def validation_health(o: Orchestrator = Depends(get_orch)):
    return {"matrix": o.health_manager.status_matrix()}


@app.get("/api/validation/leaderboard")
async def validation_leaderboard(o: Orchestrator = Depends(get_orch)):
    results = o.validation.store.latest_run_results()
    if not results:
        return {"asset_rankings": [], "strategy_rankings": [], "results": []}

    by_asset: dict = {}
    by_strategy: dict = {}
    for r in results:
        a = by_asset.setdefault(r["asset"], {"asset": r["asset"], "score_sum": 0, "count": 0, "pnl": 0.0, "trades": 0})
        a["score_sum"] += r["health_score"]
        a["count"] += 1
        a["pnl"] += r["net_profit"]
        a["trades"] += r["total_trades"]
        s = by_strategy.setdefault(r["strategy"], {"strategy": r["strategy"], "score_sum": 0, "count": 0, "pnl": 0.0, "trades": 0})
        s["score_sum"] += r["health_score"]
        s["count"] += 1
        s["pnl"] += r["net_profit"]
        s["trades"] += r["total_trades"]

    asset_rankings = sorted([
        {**v, "avg_health": round(v["score_sum"] / v["count"], 1), "net_profit": round(v["pnl"], 2)}
        for v in by_asset.values()
    ], key=lambda x: x["avg_health"], reverse=True)
    strategy_rankings = sorted([
        {**v, "avg_health": round(v["score_sum"] / v["count"], 1), "net_profit": round(v["pnl"], 2)}
        for v in by_strategy.values()
    ], key=lambda x: x["avg_health"], reverse=True)
    return {"asset_rankings": asset_rankings, "strategy_rankings": strategy_rankings, "results": results}


@app.get("/api/validation/runs")
async def validation_runs(limit: int = 20, o: Orchestrator = Depends(get_orch)):
    return o.validation.store.list_runs(limit=limit)


@app.get("/api/validation/runs/{run_id}")
async def validation_run_detail(run_id: str, o: Orchestrator = Depends(get_orch)):
    run = o.validation.store.get_run(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return run


@app.get("/api/validation/history")
async def validation_history(asset: str, strategy: str, timeframe: Optional[str] = None,
                           o: Orchestrator = Depends(get_orch)):
    tf = timeframe or o.runtime.trading.timeframe
    return o.validation.store.history_for_combo(asset, strategy, tf)


@app.post("/api/execution/clear-unreconciled")
async def clear_unreconciled_order(o: Orchestrator = Depends(get_orch)):
    """Operator acknowledgement that a timed-out order has been checked against
    the broker account. Deliberately manual: the engine never clears this by
    itself for an order whose outcome it never observed, and nothing is retried
    on the way out -- resuming is a separate, explicit action."""
    signal_id = o.trade_engine.unreconciled_signal_id
    cleared = o.trade_engine.clear_unreconciled("acknowledged via API")
    if not cleared:
        return JSONResponse({"error": "No unreconciled order to clear"}, status_code=400)
    logger.warning("[execution] user=%s cleared unreconciled order %s", o.user_id, signal_id)
    return {"ok": True, "cleared_signal_id": signal_id, "status": o.trade_engine.status(),
            "note": "Trading is still paused. Resume explicitly once you have verified the account."}


@app.get("/api/execution/status")
async def execution_status(o: Orchestrator = Depends(get_orch)):
    return o.trade_engine.status()


@app.get("/api/telegram/status")
async def telegram_status(o: Orchestrator = Depends(get_orch)):
    """Live Telegram transport state: is the sender running, how many messages
    went out, and -- the part that was missing -- the last error Telegram
    itself returned ('chat not found', 'Unauthorized', ...)."""
    return o.telegram.status()


@app.post("/api/telegram/test")
async def telegram_test(o: Orchestrator = Depends(get_orch)):
    """Send a real message right now and return Telegram's own verdict. There
    was previously no way to tell a working bot from a silently broken one
    without reading server logs."""
    o._configure_telegram()  # pick up any just-saved token/chat id
    ok, error = await o.telegram.send_now(
        "<b>\u2705 QuotexAutoTrader</b>\nTest message \u2014 Telegram alerts are working."
    )
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return {"ok": True, "status": o.telegram.status()}


@app.get("/api/settings/recommendations")
async def settings_recommendations(o: Orchestrator = Depends(get_orch)):
    """Evidence-ranked settings advice. Read-only -- nothing is applied here.

    Each recommendation carries its evidence tier so a heuristic is never
    mistaken for a measurement, and risk-management settings can only ever
    appear in the stricter direction (see settings_advisor.RISK_INVARIANTS)."""
    typical_payout = 85.0
    try:
        assets = await o.provider.get_assets()
        payouts = [float(getattr(a, "payout", 0) or 0) for a in assets if getattr(a, "payout", 0)]
        if payouts:
            typical_payout = sum(payouts) / len(payouts)
    except Exception:
        pass

    try:
        await o._eligible_assets()
        funnel = o.asset_funnel()
    except Exception:
        funnel = {}

    report = compute_rejection_report(
        o.rejected_outcome_store.all_resolved(),
        typical_payout_pct=typical_payout,
        min_sample_size=int(getattr(o.runtime.trading, "rejection_min_sample_size", 30)),
    )
    report_dict = {
        "sample_size": report.sample_size,
        "min_sample_size": report.min_sample_size,
        "cells": [
            {"gate": c.gate, "regime": c.regime, "total": c.total, "win_rate": c.win_rate,
             "wilson_lower": c.wilson_lower, "wilson_upper": c.wilson_upper,
             "significant": c.significant, "total_pnl": c.total_pnl, "verdict": c.verdict}
            for c in report.cells
        ],
    }

    trades = [t for t in o.store.all() if t.status.value in ("win", "loss", "draw")]
    wins = sum(1 for t in trades if t.status.value == "win")
    losses = sum(1 for t in trades if t.status.value == "loss")
    stats = {
        "total_trades": len(trades),
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
    }

    try:
        telemetry = o.scan_telemetry()
    except Exception:
        telemetry = {}

    return build_recommendations(
        runtime=o.runtime,
        rejection_report=report_dict,
        telemetry=telemetry,
        funnel=funnel,
        stats=stats,
        telegram_status=o.telegram.status(),
        validation_progress=o.validation.progress_dict(),
        typical_payout_pct=typical_payout,
    )


@app.post("/api/settings/recommendations/apply")
async def settings_recommendation_apply(payload: dict, o: Orchestrator = Depends(get_orch)):
    """Apply ONE recommended setting change. The setting path and direction are
    re-validated server-side -- a crafted request cannot use this endpoint to
    weaken a risk invariant, regardless of what the UI sent."""
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Invalid payload"}, status_code=400)
    setting = str(payload.get("setting") or "")
    if "value" not in payload:
        return JSONResponse({"error": "value is required"}, status_code=400)
    value = payload["value"]

    ok, reason = is_applyable(setting, value, o.runtime)
    if not ok:
        return JSONResponse({"error": reason}, status_code=400)

    section, _, field_name = setting.partition(".")
    current = o.runtime.model_dump()
    current[section][field_name] = value
    try:
        new = RuntimeSettings.model_validate(current)
    except Exception as exc:
        return JSONResponse({"error": f"Invalid value for {setting}: {exc}"}, status_code=400)

    o.apply_settings(new)
    await o.broadcast_state()
    logger.info("[settings-advisor] user=%s applied %s = %r", o.user_id, setting, value)
    return {"ok": True, "setting": setting, "value": value, "settings": o.runtime.model_dump()}


@app.get("/api/latency")
async def latency_metrics(o: Orchestrator = Depends(get_orch)):
    """Rolling end-to-end latency stats (avg/min/max/p95, ms) per pipeline
    hop: provider_receive -> eval_start -> signal_generated -> trade_request
    -> broker_ack, plus the full provider_receive->broker_ack span."""
    return o.latency.snapshot()


@app.get("/api/production/report")
async def production_report(o: Orchestrator = Depends(get_orch)):
    """Production health report: signal generation/execution rates, win
    rate, per-strategy breakdown, health indicators, and threshold alerts.
    Backed by ProductionMetrics, populated live from the trading pipeline
    (previously built but never wired in anywhere)."""
    return {
        "overall": o.production_metrics.get_overall_report(),
        "strategies": o.production_metrics.get_strategy_report(),
        "health": o.production_metrics.get_health_indicators(),
        "alerts": o.production_metrics.get_alerts(),
        "pipeline_stats": o.pipeline_tracer.get_stats(),
    }


@app.get("/api/pattern-risk/validation-report")
async def pattern_risk_validation_report(o: Orchestrator = Depends(get_orch)):
    """Read-only shadow-validation report: compares what Pattern Risk WOULD
    have recommended against real trades' actual outcomes, using
    non-overlapping Wilson confidence intervals to produce a plain-language
    verdict on whether the evidence currently supports enabling
    enforcement (config.py's pattern_risk_enforcement_enabled, which stays
    False regardless of this report until a human acts on it)."""
    import dataclasses
    report = compute_validation_report(
        o.pattern_risk_validation.all(),
        min_sample_size=int(getattr(o.runtime.trading, "pattern_risk_min_sample_size", 10)) * 3,
    )
    return dataclasses.asdict(report)


@app.get("/api/pipeline/health")
async def pipeline_health(o: Orchestrator = Depends(get_orch)):
    """Per-loop liveness detail behind the 'engine' check in /api/status --
    which background task (scan/result/watchdog/shadow/rejection_reconciliation)
    is alive, which has crash-looped past its auto-restart budget and needs
    a manual restart, and the scan heartbeat age."""
    return o.pipeline_health()


@app.post("/api/pipeline/restart")
async def pipeline_restart(user=Depends(get_current_user)):
    """Manually restarts the CALLING user's own pipeline only -- every other
    logged-in user's Orchestrator is untouched, since registry.restart()
    only ever pops/rebuilds the one key matching user["id"] from this
    caller's own JWT (the same scoping every other route in this file
    relies on via get_orch). No admin gate: every user needs this for their
    own stuck pipeline, not just operators.

    This is the safety net for whatever auto-recovery doesn't catch --
    the same full teardown-and-rebuild a systemd restart would do, just
    scoped to one user instead of forced on everyone."""
    orch, did_restart = await registry.restart(user["id"])
    if did_restart:
        await orch.broadcast_state()
    return {
        "ok": True,
        "restarted": did_restart,
        "message": "Pipeline restarted." if did_restart
                   else "Already restarted moments ago — pipeline is fresh, hang tight.",
    }


@app.get("/api/pipeline/trace/{signal_id}")
async def pipeline_trace(signal_id: str, o: Orchestrator = Depends(get_orch)):
    """Read-only: the complete decision tree for one signal attempt --
    every pipeline stage it passed through, with timing, status, and the
    already-captured data for each stage. Thin wrapper over
    PipelineTracer.export_json(), which already builds this exact
    structure (bug detection included) -- nothing new computed here."""
    trace = o.pipeline_tracer.export_json(signal_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="No trace found for this signal_id (it may have aged out of the in-memory buffer, or never reached a traced stage).")
    return trace


@app.get("/api/pipeline/replay")
async def pipeline_replay(limit: int = 100, o: Orchestrator = Depends(get_orch)):
    """Read-only: summary list of the most recent signal traces (newest
    first), for the replay view. Full per-signal detail is fetched
    per-click via /api/pipeline/trace/{signal_id} above rather than
    inlined here, to keep this endpoint cheap regardless of trace size."""
    limit = max(1, min(limit, 500))  # PipelineTracer itself caps at 500 in-memory traces
    traces = sorted(o.pipeline_tracer.traces.values(), key=lambda t: t.created_at, reverse=True)[:limit]
    return {
        "count": len(traces),
        "signals": [
            {
                "signal_id": t.signal_id, "asset": t.asset, "timeframe": t.timeframe,
                "direction": t.direction, "created_at": t.created_at,
                "successful": t.successful(), "stage_count": len(t.stages),
                "total_duration_ms": t.total_duration_ms(),
                "final_stage": t.stages[-1].stage.value if t.stages else None,
            }
            for t in traces
        ],
    }


@app.get("/api/pipeline/snapshots")
async def pipeline_snapshots(o: Orchestrator = Depends(get_orch)):
    """Read-only: current live per-asset pipeline state (connection,
    latest candle, indicators, regime) for every actively-scanned asset.
    This is the initial-load equivalent of the pipeline_snapshot WS
    event -- the dashboard calls this once on mount, then relies on the
    WS stream for updates rather than polling this endpoint."""
    return o._pipeline_events.all_snapshots()


@app.get("/api/rejections/report")
async def get_rejections_report(o: Orchestrator = Depends(get_orch)):
    """Decision Intelligence Engine Phase 3: for every gate that rejected a
    signal, what would have actually happened had it been taken anyway --
    broken down by (rejecting gate, regime), with a Wilson-bound win rate
    and a verdict against the PAYOUT-IMPLIED breakeven rate (not 50%,
    since binary options have a structural house edge). Empty/insufficient
    until decision_engine_enabled is on and enough rejected signals have
    had time to resolve. Read-only -- nothing here feeds back into any
    gate or threshold automatically."""
    typical_payout = 85.0
    try:
        assets = await o.provider.get_assets()
        payouts = [float(getattr(a, "payout", 0) or 0) for a in assets if getattr(a, "payout", 0)]
        if payouts:
            typical_payout = sum(payouts) / len(payouts)
    except Exception:
        pass
    resolved = o.rejected_outcome_store.all_resolved()
    report = compute_rejection_report(
        resolved, typical_payout_pct=typical_payout,
        min_sample_size=int(getattr(o.runtime.trading, "rejection_min_sample_size", 30)),
    )
    return {
        "sample_size": report.sample_size,
        "min_sample_size": report.min_sample_size,
        "note": report.note,
        "cells": [
            {
                "gate": c.gate, "regime": c.regime, "total": c.total, "wins": c.wins, "losses": c.losses,
                "win_rate": c.win_rate, "wilson_lower": c.wilson_lower, "wilson_upper": c.wilson_upper,
                "significant": c.significant, "total_pnl": c.total_pnl, "verdict": c.verdict,
            }
            for c in report.cells
        ],
    }


@app.get("/api/drift/status")
async def get_drift_status(o: Orchestrator = Depends(get_orch)):
    """Phase 4: read-only snapshot of every tracked (strategy, regime)
    Page-Hinkley cell's currently accumulated evidence -- lets a user see
    a cell approaching its threshold before it actually fires an alert and
    forces a trial. Empty until drift_detection_enabled is on."""
    return {"cells": o.drift_detector.status_for_ui(), "threshold": o.drift_detector.threshold,
            "delta": o.drift_detector.delta, "min_samples": o.drift_detector.min_samples}



async def pattern_risk_assess(candidate: dict, o: Orchestrator = Depends(get_orch)):
    """Read-only / advisory Pattern Risk assessment (Loss Intelligence
    Modules 4+5+7+10). Scores how much `candidate` (a dict of current
    market/signal features -- adx, atr_pct, rsi, volatility_percentile,
    volume_ratio, regime, session, timeframe, asset, otc_or_live) resembles
    historical LOSSES more than historical WINS, and returns a bounded,
    advisory-only set of recommended adjustments.

    This endpoint does not execute, reject, or modify any trade -- it is
    not called from anywhere in the scan/execute path. It exists so
    candidate scoring logic can be reviewed and tested against real trade
    history before any future integration decision."""
    import dataclasses
    assessment = o.pattern_risk_engine.assess(candidate, o.store.all())
    return dataclasses.asdict(assessment)


@app.get("/api/recovery/assess")
async def recovery_assess(o: Orchestrator = Depends(get_orch)):
    """Read-only / advisory Recovery assessment (Loss Intelligence Module
    6). Scores recent Shadow Mode performance for consistency and
    statistical significance and reports whether resuming live trading
    looks supported by the evidence -- it does NOT pause or resume
    anything. Not called from anywhere in the scan/pause/resume path."""
    import dataclasses
    assessment = o.recovery_engine.assess(o.shadow_store.all_resolved())
    return dataclasses.asdict(assessment)



async def analytics_report(o: Orchestrator = Depends(get_orch)):
    """Read-only trade analytics (Loss Intelligence Modules 2+3+8): win-rate
    breakdowns by weekday/hour/session/asset/OTC/timeframe/strategy/regime/
    volatility/confidence/trend, root-cause loss-reason tags compared
    against win prevalence (never loss-only), and best/worst summaries --
    every number gated by Wilson-interval statistical significance rather
    than presented as reliable at any sample size.

    This endpoint computes on demand from existing trade history and does
    not affect live trading in any way -- nothing in orchestrator.py's
    scan/execute path calls AnalyticsEngine."""
    import dataclasses
    report = o.analytics_engine.compute(o.store.all())
    return dataclasses.asdict(report)


# --------------------------------------------------------------------------- #
# WebSocket — auth via ?token=, one connection maps to exactly one user's
# broadcast stream (hub is keyed by user_id). Heartbeats keep the connection
# alive through proxies/load balancers that drop idle sockets, and let us
# detect and clean up a client that vanished without a close frame.
# --------------------------------------------------------------------------- #
HEARTBEAT_INTERVAL = 20.0  # seconds between server pings
HEARTBEAT_TIMEOUT = 45.0   # no client activity for this long => treat as dead


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    user = await get_user_from_ws(ws)
    if user is None:
        return  # already closed with 4401 inside get_user_from_ws
    o = await registry.get_or_create(user["id"])
    await hub.connect(ws, user["id"])
    last_seen = asyncio.get_event_loop().time()

    async def heartbeat() -> None:
        nonlocal last_seen
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if asyncio.get_event_loop().time() - last_seen > HEARTBEAT_TIMEOUT:
                try:
                    await ws.close(code=4000, reason="heartbeat timeout")
                except Exception:
                    pass
                return
            try:
                await ws.send_json({"event": "ping", "data": {"t": last_seen}})
            except Exception:
                # Root cause of the "pipeline frozen for one user" class of
                # incident: a failed outbound ping means this connection is
                # dead (commonly a half-open TCP connection -- laptop sleep,
                # network switch, idle proxy timeout -- where neither side
                # ever receives a clean close). Previously this branch just
                # returned, leaving the dead socket registered in the hub
                # forever with no further liveness checks (this task, the
                # only thing checking liveness, has now exited) -- every
                # future broadcast to this user silently vanishes into it,
                # and the client's browser WebSocket object never gets a
                # close event, so its own reconnect logic never fires either.
                # Actively closing here (matching what the timeout branch
                # above already does) makes the main loop's
                # ws.receive_text() raise, which runs the existing
                # finally-block cleanup (hub.disconnect) and sends the
                # client a real close frame so it reconnects on its own.
                logger.warning("[ws] Heartbeat ping failed for a connection -- closing it so the client reconnects (dead/half-open connection)")
                try:
                    await ws.close(code=4000, reason="heartbeat send failed")
                except Exception:
                    pass
                return

    hb_task = asyncio.create_task(heartbeat())
    try:
        await ws.send_json({"event": "state", "data": o.state_dict()})
        while True:
            raw = await ws.receive_text()
            last_seen = asyncio.get_event_loop().time()
            # Client sends {"event":"ping"} on its own cadence too (in case
            # the server->client leg is fine but client->server stalled);
            # answer it so the client's own liveness check is satisfied.
            try:
                msg = json.loads(raw)
                if msg.get("event") == "ping":
                    await ws.send_json({"event": "pong", "data": {}})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hb_task.cancel()
        await hub.disconnect(ws, user["id"])


# --------------------------------------------------------------------------- #
# Serve built frontend (production); in dev the Vite server is used instead.
# --------------------------------------------------------------------------- #
_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(_DIST / "index.html")

    @app.get("/{path:path}")
    async def spa(path: str):
        target = _DIST / path
        if target.exists() and target.is_file():
            return FileResponse(target)
        return FileResponse(_DIST / "index.html")
