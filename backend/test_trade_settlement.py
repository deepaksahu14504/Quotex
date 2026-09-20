"""Regression tests for RCA F7 (P4) and RCA F8 (P5) — trade settlement.

P4 — TIMEOUT / TIE / REFUND MUST NOT BE BOOKED AS A LOSS
--------------------------------------------------------
pyquotex derives its verdict as `win = "win" if profit > 0 else "loss"` in
three places (api.py:643, 718, 762), and its `check_win()` returns the literal
`("loss", 0.0)` when `self.api is None`, when the payload is empty, and when
its own internal `slot.wait(timeout=300)` gives up (_api/trading.py:216-217).
The old `PyQuotexProvider.check_result` passed all of that straight through, so:

  * a merely SLOW confirmation became a $0 LOSS, and
  * a TIE or broker REFUND (stake returned, profit exactly 0) became a LOSS.

Neither is cosmetic. `RiskManager.record_result()` treats a loss by
incrementing `consecutive_losses` -- three in a row auto-pauses the bot -- and
`_martingale_step`, which escalates the stake on the next trade after one that
lost nothing. The old function's `else "draw"` branch was unreachable, because
the vendor only ever emits "win"/"loss" and both matched an earlier branch.

P5 — ONE SLOW TRADE MUST NOT BLOCK THE OTHERS
---------------------------------------------
`_result_loop` awaited `check_result` bare and sequentially, so a single trade
sitting in pyquotex's 300s wait stalled every other open trade behind it: at
`max_concurrent_trades=3` one pass could take ~900s, `active_trades` stayed
full, and `can_trade()` kept refusing new signals on the max_concurrent gate
long after those trades had really closed.

Verified against the repo's own trade history (25 closed trades): every `loss`
has profit < 0 (-1.4, -5.8, -6.6, ...) and every `win` has profit > 0 (4.968,
441.6, ...), so `profit` is signed net P&L and zero can only mean the stake
came back.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services import market as market_mod
from app.services.market import PyQuotexProvider


class _FakeClient:
    """Stands in for pyquotex's Quotex client at the check_win boundary."""

    def __init__(self, reply=None, delay=0.0, raises=None):
        self.reply = reply
        self.delay = delay
        self.raises = raises
        self.calls = 0

    async def check_win(self, order_id, duration=0):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.reply


def _provider(client):
    p = PyQuotexProvider("settlement@example.invalid", "pw", True)
    p._client = client
    p._connected = True
    return p


# --------------------------------------------------------------------------- #
# P4 — classification
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_win_with_positive_profit_is_a_win():
    p = _provider(_FakeClient(reply=("win", 4.968)))
    r = await p.check_result("1")
    assert r["status"] == "win"
    assert r["profit"] == pytest.approx(4.968)


@pytest.mark.asyncio
async def test_loss_with_negative_profit_is_a_loss():
    p = _provider(_FakeClient(reply=("loss", -1.4)))
    r = await p.check_result("1")
    assert r["status"] == "loss"
    assert r["profit"] == pytest.approx(-1.4)


@pytest.mark.asyncio
async def test_zero_profit_is_a_draw_not_a_loss():
    """THE regression. The vendor reports a tie/refund as ("loss", 0.0)."""
    p = _provider(_FakeClient(reply=("loss", 0.0)))
    r = await p.check_result("1")
    assert r["status"] == "draw", "a returned stake was booked as a loss"
    assert r["profit"] == 0.0


@pytest.mark.asyncio
async def test_zero_profit_reported_as_win_is_also_a_draw():
    """A "win" that paid nothing is economically a tie, not a win."""
    p = _provider(_FakeClient(reply=("win", 0.0)))
    r = await p.check_result("1")
    assert r["status"] == "draw"


@pytest.mark.asyncio
async def test_classification_uses_the_money_not_the_vendor_string():
    """If the broker's verdict string ever contradicts the profit sign, the
    money wins -- that is the value the balance is actually adjusted by."""
    p = _provider(_FakeClient(reply=("loss", 12.5)))
    assert (await p.check_result("1"))["status"] == "win"

    p = _provider(_FakeClient(reply=("win", -3.0)))
    assert (await p.check_result("1"))["status"] == "loss"


# --------------------------------------------------------------------------- #
# P4 — a timeout is "unknown", never a loss
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_slow_confirmation_returns_none_not_a_loss(monkeypatch):
    """pyquotex's own 300s wait ends in ("loss", 0.0). We must give up first
    and report "not yet known", leaving the trade pending."""
    monkeypatch.setattr(market_mod, "CHECK_RESULT_TIMEOUT", 0.2)
    p = _provider(_FakeClient(reply=("loss", 0.0), delay=5.0))

    started = time.monotonic()
    r = await p.check_result("1")
    elapsed = time.monotonic() - started

    assert r is None, "a timeout was reported as a settled result"
    assert elapsed < 2.0, f"the call was not bounded (took {elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_bounded_call_does_not_wait_the_vendors_full_300s(monkeypatch):
    """The whole point: without our ceiling this await would run for 300s."""
    monkeypatch.setattr(market_mod, "CHECK_RESULT_TIMEOUT", 0.1)
    p = _provider(_FakeClient(reply=("win", 1.0), delay=300.0))

    started = time.monotonic()
    assert await p.check_result("1") is None
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_provider_error_returns_none():
    p = _provider(_FakeClient(raises=RuntimeError("socket gone")))
    assert await p.check_result("1") is None


@pytest.mark.asyncio
async def test_none_verdict_returns_none():
    p = _provider(_FakeClient(reply=(None, 0.0)))
    assert await p.check_result("1") is None


def test_provider_timeout_is_below_the_orchestrator_backstop():
    """Ordering matters: the provider must give up before the outer
    `asyncio.wait_for` in _result_loop, otherwise the inner, better-informed
    "leave it pending" path never runs."""
    from app.orchestrator import RESULT_CHECK_TIMEOUT

    assert market_mod.CHECK_RESULT_TIMEOUT < RESULT_CHECK_TIMEOUT


def test_both_timeouts_are_well_below_the_vendors_300s():
    from app.orchestrator import RESULT_CHECK_TIMEOUT

    assert market_mod.CHECK_RESULT_TIMEOUT < 300.0
    assert RESULT_CHECK_TIMEOUT < 300.0


# --------------------------------------------------------------------------- #
# P5 — concurrency
# --------------------------------------------------------------------------- #

class _SlowProvider:
    """Each check_result takes `delay`; `hang_id` never returns in time."""

    name = "pyquotex"

    def __init__(self, delay, results, hang_id=None):
        self.delay = delay
        self.results = results
        self.hang_id = hang_id
        self.started = []

    async def check_result(self, oid):
        self.started.append((oid, time.monotonic()))
        if oid == self.hang_id:
            await asyncio.sleep(600)
        await asyncio.sleep(self.delay)
        return self.results.get(oid)


def _orchestrator_with(monkeypatch, provider, trades):
    """A real Orchestrator whose provider and open trades we control."""
    from app.config import RuntimeSettings
    from app.orchestrator import Orchestrator
    from app.session_manager import BrokerCredentials, session_manager

    creds = BrokerCredentials(
        quotex_email="broker@example.invalid", quotex_password="pw", quotex_is_demo=True
    )
    monkeypatch.setattr(session_manager, "get_credentials", lambda uid: creds)
    monkeypatch.setattr(session_manager, "set_account_type", lambda uid, d: True)
    monkeypatch.setattr(RuntimeSettings, "save", lambda self, user_id=None: None)

    orch = Orchestrator("p5-settlement-user", RuntimeSettings())
    orch.provider = provider
    orch.active_trades = dict(trades)
    return orch


@pytest.mark.asyncio
async def test_results_are_fetched_concurrently_not_sequentially(monkeypatch, pg):
    """Four trades at 0.4s each. Sequentially that is ~1.6s; concurrently it
    is ~0.4s. This is the assertion that fails against the pre-fix loop."""
    provider = _SlowProvider(
        delay=0.4,
        results={f"o{i}": {"status": "win", "profit": 1.0} for i in range(4)},
    )
    orch = _orchestrator_with(monkeypatch, provider, {f"o{i}": object() for i in range(4)})

    started = time.monotonic()
    settled = await orch._collect_results()
    elapsed = time.monotonic() - started

    assert len(settled) == 4
    assert elapsed < 1.0, f"checks ran sequentially ({elapsed:.2f}s for 4x0.4s)"


@pytest.mark.asyncio
async def test_one_hanging_trade_does_not_block_the_others(monkeypatch, pg):
    """Pre-fix, a trade stuck in pyquotex's 300s wait stalled the whole pass.
    The others must still settle promptly."""
    monkeypatch.setattr("app.orchestrator.RESULT_CHECK_TIMEOUT", 0.5)
    provider = _SlowProvider(
        delay=0.1,
        results={"good": {"status": "win", "profit": 2.0}},
        hang_id="bad",
    )
    orch = _orchestrator_with(monkeypatch, provider, {"bad": object(), "good": object()})

    started = time.monotonic()
    settled = await orch._collect_results()
    elapsed = time.monotonic() - started

    got = {oid: r for oid, _t, r in settled}
    assert got["good"] == {"status": "win", "profit": 2.0}
    assert got["bad"] is None, "the hung trade must come back pending, not settled"
    assert elapsed < 3.0, f"the hung trade blocked the pass ({elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_a_provider_exception_does_not_take_down_the_pass(monkeypatch, pg):
    class _Boom:
        name = "pyquotex"

        async def check_result(self, oid):
            if oid == "boom":
                raise RuntimeError("socket gone")
            return {"status": "win", "profit": 1.0}

    orch = _orchestrator_with(monkeypatch, _Boom(), {"boom": object(), "ok": object()})
    settled = await orch._collect_results()

    got = {oid: r for oid, _t, r in settled}
    assert "boom" not in got, "a raising check must be dropped, not propagated"
    assert got["ok"]["status"] == "win"


@pytest.mark.asyncio
async def test_no_open_trades_is_a_noop(monkeypatch, pg):
    orch = _orchestrator_with(monkeypatch, _SlowProvider(0.0, {}), {})
    assert await orch._collect_results() == []


@pytest.mark.asyncio
async def test_result_loop_skips_trades_settled_while_checks_were_in_flight(monkeypatch, pg):
    """The gather window is a real concurrency window: a trade can leave
    active_trades between the check and the bookkeeping. Applying a stale
    result would double-count it."""
    provider = _SlowProvider(delay=0.2, results={"gone": {"status": "win", "profit": 1.0}})
    orch = _orchestrator_with(monkeypatch, provider, {"gone": object()})

    async def _remove_soon():
        await asyncio.sleep(0.05)
        orch.active_trades.pop("gone", None)

    task = asyncio.ensure_future(_remove_soon())
    settled = await orch._collect_results()
    await task

    assert len(settled) == 1
    assert "gone" not in orch.active_trades
