"""Settings Advisor — "which setting should I change, and should I?"

Read-only. This module NEVER mutates settings; it produces a ranked list of
recommendations that the UI shows next to each setting, each one carrying the
evidence it was derived from. Applying is always an explicit user action.

Why this exists
---------------
The engine already measures everything needed to answer "should I loosen this
gate?" — `rejection_reconciliation` resolves what WOULD have happened to every
rejected signal, with a Wilson-bounded win rate judged against the
payout-implied breakeven (not 50%). Until now that lived in a read-only
Analytics panel and nobody connected it to the setting it implies. This turns
that measurement into a concrete suggestion:

    "confidence_threshold rejected 84 trending-regime signals whose worst-case
     plausible win rate (57.1%) is still above breakeven (54.1%) — that gate is
     costing edge here. Try 70 → 66."

Hard rules baked in (they mirror the project's own operating rules)
------------------------------------------------------------------
  * Never recommend weakening RISK MANAGEMENT to increase signal count.
    Daily loss limit, max consecutive losses, drawdown breaker, correlation
    guard, cooldown, max concurrent — these are only ever recommended in the
    SAFER direction, never the looser one. Signal throughput is raised through
    the candidate funnel and the confluence/quality gates, never by removing a
    stop.
  * Never recommend switching DEMO → LIVE. That is always a human decision.
  * Never recommend enabling martingale. If it IS enabled, that generates a
    safety recommendation to turn it off.
  * Never recommend a threshold change from an insignificant sample. Below the
    minimum resolved-sample bar, the only recommendation is "collect more data"
    — with shadow mode named as the way to do it without risking money.
  * Every recommendation states its evidence tier, so a heuristic is never
    mistaken for a measurement.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# Evidence tiers, strongest first.
MEASURED = "measured"        # derived from this account's own resolved outcomes
OBSERVED = "observed"        # derived from live telemetry (funnel/throughput), not outcomes
HEURISTIC = "heuristic"      # a sensible default, not evidence about you
NEEDS_DATA = "needs_data"    # cannot be answered yet

# Effect on the pipeline, so the UI can group "more signals" vs "safer".
MORE_SIGNALS = "more_signals"
FEWER_SIGNALS = "fewer_signals"
SAFETY = "safety"
RELIABILITY = "reliability"

# Settings this module is structurally forbidden from loosening. Anything
# here can only ever appear in a recommendation that makes it stricter.
RISK_INVARIANTS = {
    "risk.daily_loss_limit",
    "risk.max_consecutive_losses",
    "risk.max_trades_per_day",
    "risk.max_concurrent_trades",
    "risk.cooldown_seconds",
    "risk.drawdown_breaker_enabled",
    "risk.max_drawdown_pct",
    "risk.correlation_guard_enabled",
    "risk.stale_data_pause_seconds",
    "risk.martingale_enabled",
    "trading.account_mode",
    "trading.is_demo",
}


@dataclass
class Recommendation:
    id: str
    setting: Optional[str]            # dotted path, None for "no setting to change"
    label: str
    current: Any
    suggested: Any
    effect: str
    evidence: str
    why: str                          # the numbers this came from
    risk: str = ""                    # what could go wrong if applied
    applyable: bool = True            # can the UI one-click apply it
    priority: int = 50                # lower sorts first
    samples: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _cell_is_too_conservative(cell: dict) -> bool:
    return bool(cell.get("significant")) and str(cell.get("verdict", "")).startswith("Possibly too conservative")


def _cell_is_well_calibrated(cell: dict) -> bool:
    return bool(cell.get("significant")) and str(cell.get("verdict", "")).startswith("Well-calibrated")


def build_recommendations(
    *,
    runtime,
    rejection_report: Optional[dict] = None,
    telemetry: Optional[dict] = None,
    funnel: Optional[dict] = None,
    stats: Optional[dict] = None,
    telegram_status: Optional[dict] = None,
    validation_progress: Optional[dict] = None,
    typical_payout_pct: float = 85.0,
) -> dict:
    """Pure function. Returns {'data_confidence', 'summary', 'recommendations'}."""
    rejection_report = rejection_report or {}
    telemetry = telemetry or {}
    funnel = funnel or {}
    stats = stats or {}
    recs: List[Recommendation] = []

    risk = runtime.risk
    trading = runtime.trading
    breakeven = 100.0 / (1.0 + typical_payout_pct / 100.0)

    cells: List[dict] = rejection_report.get("cells") or []
    sample_size = int(rejection_report.get("sample_size") or 0)
    min_sample = int(rejection_report.get("min_sample_size") or 30)
    significant_cells = [c for c in cells if c.get("significant")]

    # ------------------------------------------------------------------ #
    # 0. Data confidence — decides whether threshold advice is allowed at all
    # ------------------------------------------------------------------ #
    if significant_cells:
        data_confidence = MEASURED
    elif sample_size:
        data_confidence = NEEDS_DATA
    else:
        data_confidence = NEEDS_DATA

    if data_confidence == NEEDS_DATA:
        recs.append(Recommendation(
            id="collect_evidence_first",
            setting="trading.shadow_mode_enabled" if not getattr(trading, "shadow_mode_enabled", False) else None,
            label="Collect outcome data before tuning thresholds",
            current=getattr(trading, "shadow_mode_enabled", False),
            suggested=True,
            effect=RELIABILITY,
            evidence=NEEDS_DATA,
            why=(f"Only {sample_size} rejected signals have resolved; {min_sample} per (gate, regime) cell "
                 f"are needed before any threshold change is more than a guess. Shadow mode resolves "
                 f"signals against live prices without placing real orders."),
            risk="None — shadow mode places no orders. It only costs time.",
            applyable=not getattr(trading, "shadow_mode_enabled", False),
            priority=5,
            samples=sample_size,
        ))

    # ------------------------------------------------------------------ #
    # 1. MEASURED: gates whose own rejections would have been profitable
    # ------------------------------------------------------------------ #
    for cell in sorted(cells, key=lambda c: -(c.get("total") or 0)):
        if not _cell_is_too_conservative(cell):
            continue
        gate = cell.get("gate")
        regime = cell.get("regime", "?")
        n = int(cell.get("total") or 0)
        wl = cell.get("wilson_lower")
        evidence_line = (
            f"In the {regime} regime, {gate} rejected {n} signals that resolved; even the most "
            f"pessimistic plausible win rate for them ({wl}%) is above the {typical_payout_pct:.0f}% "
            f"payout's breakeven ({breakeven:.1f}%). That gate is turning away edge here."
        )

        if gate == "confidence_threshold":
            cur = int(risk.confidence_threshold)
            suggested = max(55, cur - 4)
            if suggested < cur:
                recs.append(Recommendation(
                    id=f"loosen_confidence_{regime}",
                    setting="risk.confidence_threshold",
                    label="Confidence threshold",
                    current=cur, suggested=suggested,
                    effect=MORE_SIGNALS, evidence=MEASURED,
                    why=evidence_line,
                    risk=("Lower threshold = more signals AND more marginal ones. Move in 4-point steps "
                          "and re-check this panel after ~30 more resolved trades; revert if win rate drops."),
                    priority=10, samples=n,
                ))

        elif gate == "precision_gate" and getattr(trading, "precision_mode_enabled", False):
            cur = int(getattr(trading, "precision_max_opposing", 0))
            recs.append(Recommendation(
                id=f"loosen_precision_{regime}",
                setting="trading.precision_max_opposing",
                label="Precision mode — max opposing votes",
                current=cur, suggested=cur + 1,
                effect=MORE_SIGNALS, evidence=MEASURED,
                why=evidence_line,
                risk=("Allowing one more counter-vote widens the funnel. If the added signals underperform, "
                      "put it back to 0 rather than lowering the confidence threshold as well."),
                priority=12, samples=n,
            ))

        elif gate == "confluence_engine":
            cur = int(getattr(trading, "confluence_min_confirmations", 2))
            if cur > 1:
                recs.append(Recommendation(
                    id=f"loosen_confluence_{regime}",
                    setting="trading.confluence_min_confirmations",
                    label="Minimum agreeing strategies (when opposed)",
                    current=cur, suggested=cur - 1,
                    effect=MORE_SIGNALS, evidence=MEASURED,
                    why=evidence_line,
                    risk="Fewer required confirmations means more single-strategy signals get through.",
                    priority=14, samples=n,
                ))
            else:
                cur_edge = float(getattr(trading, "confluence_min_directional_edge", 0.6))
                suggested = round(max(0.5, cur_edge - 0.05), 2)
                if suggested < cur_edge:
                    recs.append(Recommendation(
                        id=f"loosen_edge_{regime}",
                        setting="trading.confluence_min_directional_edge",
                        label="Minimum directional edge",
                        current=cur_edge, suggested=suggested,
                        effect=MORE_SIGNALS, evidence=MEASURED,
                        why=evidence_line,
                        risk="Accepts more evenly-split setups. 0.50 is a coin flip — do not go below it.",
                        priority=15, samples=n,
                    ))

        elif gate == "signal_validation" and getattr(trading, "signal_revalidation", True):
            cur = float(getattr(trading, "reval_veto_adjustment", -20.0))
            recs.append(Recommendation(
                id=f"loosen_reval_{regime}",
                setting="trading.reval_veto_adjustment",
                label="Revalidation veto tolerance",
                current=cur, suggested=round(cur - 5.0, 1),
                effect=MORE_SIGNALS, evidence=MEASURED,
                why=evidence_line,
                risk=("Revalidation exists to drop setups that decayed between signal and entry. "
                      "Loosen it one step only, and never disable it outright."),
                priority=16, samples=n,
            ))

    # ------------------------------------------------------------------ #
    # 2. MEASURED: gates that are earning their keep — explicitly say "leave it"
    # ------------------------------------------------------------------ #
    for cell in cells:
        if not _cell_is_well_calibrated(cell):
            continue
        saved = cell.get("total_pnl")
        recs.append(Recommendation(
            id=f"hold_{cell.get('gate')}_{cell.get('regime')}",
            setting=None,
            label=f"Leave {cell.get('gate')} as-is ({cell.get('regime')} regime)",
            current="unchanged", suggested="unchanged",
            effect=FEWER_SIGNALS, evidence=MEASURED,
            why=(f"Those {cell.get('total')} rejections would have won only {cell.get('win_rate')}% "
                 f"(best case {cell.get('wilson_upper')}%) against a {breakeven:.1f}% breakeven"
                 + (f", i.e. about {saved} saved at the reference stake." if saved is not None else ".")),
            risk="", applyable=False, priority=40, samples=int(cell.get("total") or 0),
        ))

    # ------------------------------------------------------------------ #
    # 3. OBSERVED: throughput — the safe way to get more signals
    #    (widen the candidate funnel instead of lowering quality bars)
    # ------------------------------------------------------------------ #
    by_cap = int(funnel.get("candidates_excluded_by_cap") or 0)
    cur_cap = int(getattr(trading, "max_scan_candidates", 12))
    if by_cap > 0:
        recs.append(Recommendation(
            id="raise_scan_cap",
            setting="trading.max_scan_candidates",
            label="Assets scanned per cycle",
            current=cur_cap, suggested=min(cur_cap + 6, cur_cap + by_cap),
            effect=MORE_SIGNALS, evidence=OBSERVED,
            why=(f"{by_cap} READY, payout-qualified assets are being dropped every cycle purely by the "
                 f"top-{cur_cap} cap — not by any quality gate. Scanning more assets raises signal count "
                 f"without lowering a single quality bar."),
            risk=("Each extra asset costs scan time. If scan cycle age starts climbing past ~15s, "
                  "step it back down."),
            priority=8, samples=by_cap,
        ))

    by_payout = int(funnel.get("candidates_excluded_by_payout")
                    or telemetry.get("candidates_excluded_by_payout") or 0)
    open_n = int(funnel.get("open_count") or 0)
    if by_payout and open_n and by_payout >= max(3, int(open_n * 0.5)) and risk.min_payout > 70:
        recs.append(Recommendation(
            id="lower_min_payout",
            setting="risk.min_payout",
            label="Minimum payout",
            current=risk.min_payout, suggested=max(70.0, round(risk.min_payout - 5.0, 1)),
            effect=MORE_SIGNALS, evidence=OBSERVED,
            why=(f"{by_payout} of {open_n} open assets are excluded for paying below {risk.min_payout:g}%. "
                 f"This is a throughput lever, not a quality one."),
            risk=("Lower payout raises the breakeven win rate you need. At 75% payout breakeven is "
                  f"~57.1%; at {max(70.0, risk.min_payout - 5.0):g}% it is higher still — do not pair this "
                  "with a lower confidence threshold in the same change."),
            priority=20, samples=by_payout,
        ))

    by_ready = int(funnel.get("candidates_excluded_by_readiness")
                   or telemetry.get("candidates_excluded_by_readiness") or 0)
    if by_ready and by_ready >= 3:
        recs.append(Recommendation(
            id="warmup_bottleneck",
            setting=None,
            label="Warm-up is the bottleneck, not your thresholds",
            current=f"{by_ready} assets not READY", suggested="fix warm-up first",
            effect=RELIABILITY, evidence=OBSERVED,
            why=(f"{by_ready} eligible assets never reached READY, so the scanner never sees them. "
                 f"Loosening quality gates will not recover these — check Market history warm-up in "
                 f"System Status and the broker connection."),
            risk="", applyable=False, priority=9, samples=by_ready,
        ))

    whitelist = list(getattr(trading, "asset_whitelist", []) or [])
    elig = int(funnel.get("open_and_payout_qualified_count") or 0)
    if whitelist and elig and int(funnel.get("whitelist_count") or 0) <= 1:
        recs.append(Recommendation(
            id="whitelist_too_narrow",
            setting="trading.asset_whitelist",
            label="Asset whitelist",
            current=f"{len(whitelist)} asset(s)", suggested="clear it (all open assets)",
            effect=MORE_SIGNALS, evidence=OBSERVED,
            why=(f"{elig} assets qualify on payout but the whitelist reduces that to "
                 f"{funnel.get('whitelist_count')}. The whitelist, not the strategy, is capping signals."),
            risk="A wider universe includes assets you have not validated. Check the Validation matrix first.",
            priority=18,
        ))

    blocks = telemetry.get("scan_gate_blocks") or {}
    if isinstance(blocks, dict) and blocks and not any(_cell_is_too_conservative(c) for c in cells):
        top = max(((k, v) for k, v in blocks.items() if isinstance(v, (int, float))),
                  key=lambda kv: kv[1], default=None)
        if top and top[1] >= 5:
            recs.append(Recommendation(
                id="top_gate_block_info",
                setting=None,
                label=f"Most cycles are ending at: {top[0]}",
                current=f"{top[1]} blocks", suggested="see evidence before changing it",
                effect=RELIABILITY, evidence=OBSERVED,
                why=(f"'{top[0]}' consumed {top[1]} scan cycles. That tells you WHERE signals stop — it does "
                     f"not yet tell you whether stopping there was right. The rejection report decides that, "
                     f"once {min_sample} of those have resolved."),
                risk="", applyable=False, priority=25, samples=int(top[1]),
            ))

    # ------------------------------------------------------------------ #
    # 4. SAFETY — only ever in the stricter direction
    # ------------------------------------------------------------------ #
    if getattr(risk, "martingale_enabled", False):
        recs.append(Recommendation(
            id="disable_martingale",
            setting="risk.martingale_enabled",
            label="Martingale",
            current=True, suggested=False,
            effect=SAFETY, evidence=HEURISTIC,
            why=("Martingale does not change your win rate; it changes the shape of your losses so that a "
                 "normal losing streak takes an abnormal share of the account. With max_consecutive_losses "
                 f"= {getattr(risk, 'max_consecutive_losses', 3)}, the last step is "
                 f"{getattr(risk, 'martingale_multiplier', 2.0) ** getattr(risk, 'martingale_max_steps', 2):.0f}x "
                 "your base stake."),
            risk="", priority=1,
        ))

    if not getattr(risk, "drawdown_breaker_enabled", True):
        recs.append(Recommendation(
            id="enable_drawdown_breaker",
            setting="risk.drawdown_breaker_enabled",
            label="Drawdown circuit breaker",
            current=False, suggested=True,
            effect=SAFETY, evidence=HEURISTIC,
            why="With the breaker off, nothing stops a bad session other than the daily loss limit.",
            risk="", priority=2,
        ))

    trades = int(stats.get("total_trades") or 0)
    win_rate = stats.get("win_rate")
    if trades >= 30 and isinstance(win_rate, (int, float)) and win_rate < breakeven:
        cur = int(risk.confidence_threshold)
        recs.append(Recommendation(
            id="raise_confidence_threshold",
            setting="risk.confidence_threshold",
            label="Confidence threshold",
            current=cur, suggested=min(90, cur + 4),
            effect=FEWER_SIGNALS, evidence=MEASURED,
            why=(f"Realized win rate over {trades} trades is {win_rate}%, below the {breakeven:.1f}% "
                 f"breakeven implied by a {typical_payout_pct:.0f}% payout. At this rate more signals "
                 f"means more net loss, so the throughput recommendations above should wait."),
            risk="Fewer signals. That is the intended effect while the edge is negative.",
            priority=3, samples=trades,
        ))

    # ------------------------------------------------------------------ #
    # 5. RELIABILITY — things that are simply misconfigured
    # ------------------------------------------------------------------ #
    if telegram_status and telegram_status.get("enabled") and telegram_status.get("last_error") \
            and not telegram_status.get("sent_count"):
        recs.append(Recommendation(
            id="fix_telegram",
            setting=None,
            label="Telegram is enabled but nothing is being delivered",
            current=str(telegram_status.get("last_error"))[:120], suggested="fix token / chat ID",
            effect=RELIABILITY, evidence=OBSERVED,
            why=f"Telegram's last response was: {telegram_status.get('last_error')}",
            risk="", applyable=False, priority=6,
        ))

    if not getattr(runtime.validation, "enabled", True):
        recs.append(Recommendation(
            id="enable_validation",
            setting="validation.enabled",
            label="Auto strategy validation",
            current=False, suggested=True,
            effect=RELIABILITY, evidence=HEURISTIC,
            why=("Validation is what mutes a strategy that has stopped working on an asset. With it off, "
                 "a decayed strategy keeps voting indefinitely."),
            risk="Runs in the background; never blocks live trading.",
            priority=22,
        ))
    elif validation_progress and validation_progress.get("status") == "failed":
        recs.append(Recommendation(
            id="validation_failing",
            setting=None,
            label="Validation runs are failing",
            current=str(validation_progress.get("error") or "unknown")[:120],
            suggested="see the Validation page",
            effect=RELIABILITY, evidence=OBSERVED,
            why="Health scores are stale, so strategy muting is running on old evidence.",
            risk="", applyable=False, priority=7,
        ))

    if getattr(trading, "mode", "manual") == "off":
        recs.append(Recommendation(
            id="mode_off",
            setting=None,
            label="Mode is Off — the scanner is not producing signals at all",
            current="off", suggested="manual",
            effect=RELIABILITY, evidence=OBSERVED,
            why="No setting below this matters while the mode is Off.",
            risk="", applyable=False, priority=4,
        ))

    # ------------------------------------------------------------------ #
    # Assemble
    # ------------------------------------------------------------------ #
    recs.sort(key=lambda r: (r.priority, -r.samples))
    more = sum(1 for r in recs if r.effect == MORE_SIGNALS)
    safety = sum(1 for r in recs if r.effect == SAFETY)

    if data_confidence == NEEDS_DATA:
        summary = (f"Not enough resolved outcomes yet ({sample_size}/{min_sample}) to justify a threshold "
                   f"change. Throughput and reliability items below are safe to act on now.")
    elif more:
        summary = (f"{more} evidence-backed way(s) to increase signal count, "
                   f"{safety} safety item(s). Apply one change at a time.")
    else:
        summary = ("Your gates are currently rejecting signals that would have lost. "
                   "No loosening is justified by your own data right now.")

    return {
        "generated_at": time.time(),
        "data_confidence": data_confidence,
        "resolved_samples": sample_size,
        "min_sample_size": min_sample,
        "typical_payout_pct": round(typical_payout_pct, 1),
        "breakeven_win_rate": round(breakeven, 1),
        "summary": summary,
        "recommendations": [r.as_dict() for r in recs],
    }


def is_applyable(setting_path: str, suggested: Any, runtime) -> tuple[bool, str]:
    """Guard for the apply endpoint. A recommendation id is not trusted on its
    way back in — the setting path and direction are re-checked server-side so
    a crafted request can't use this endpoint to weaken a risk invariant."""
    if setting_path in RISK_INVARIANTS:
        # Only stricter moves allowed, and only for the ones that make sense.
        if setting_path == "risk.martingale_enabled" and suggested is False:
            return True, ""
        if setting_path == "risk.drawdown_breaker_enabled" and suggested is True:
            return True, ""
        if setting_path == "risk.correlation_guard_enabled" and suggested is True:
            return True, ""
        return False, f"{setting_path} is a risk invariant and cannot be changed from a recommendation"
    section, _, field_name = setting_path.partition(".")
    if not hasattr(runtime, section) or not hasattr(getattr(runtime, section), field_name):
        return False, f"Unknown setting {setting_path}"
    return True, ""
