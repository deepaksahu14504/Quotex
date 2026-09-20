"""Regression tests for RCA F8 — restart recovery of open trades.

THE BUG
-------
`_reconcile_open_trades()` ran at the top of `start()` and its entire body was:

    stale = [t.id for t in self.store.all()
             if t.status in (TradeStatus.OPEN, TradeStatus.PENDING)]
    if stale:
        self.store.remove_many(stale)

Every position that was live when the process died was DELETED from the trade
ledger. That is not a neutral "keep history accurate" choice -- it destroys the
record of wins and losses alike, so whatever the broker actually did with those
orders never reaches the risk counters, the strategy performance tracker, the
win-rate calibration, `daily_pnl` or the trade log. The statistics are
systematically skewed towards whatever survived, and a restart during a losing
streak erased the streak -- so the drawdown and consecutive-loss circuit
breakers could be walked around by restarting the process.

THE FIX
-------
Ask the broker instead. `provider.recover_result()` reads the broker's own
trade history (`get_trader_history`), which is server-side and survives our
restart. Whatever it can settle is queued in `_recovered_results` and the trade
is put back into `active_trades`, so it flows through the ordinary
`_result_loop` settlement path rather than a second, divergent bookkeeping
path. Anything the broker does not recognise is still tracked, not deleted;
only after UNRECOVERABLE_TRADE_AFTER_SECONDS is a trade marked `error` --
visibly unresolved -- and even then it is never removed.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.schemas import Direction, TradeRecord, TradeStatus
from app.store import TradeStore

USER = "p6-restart-recovery-user"


# --------------------------------------------------------------------------- #
# Provider-level recovery
# --------------------------------------------------------------------------- #

class _HistoryClient:
    """Stands in for pyquotex at the get_result boundary."""

    def __init__(self, history=None, delay=0.0, raises=None):
        self.history = history if history is not None else (None, "OperationID Not Found.")
        self.delay = delay
        self.raises = raises
        self.calls = []

    async def get_result(self, operation_id):
        self.calls.append(operation_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.history


def _provider(client):
    from app.services.market import PyQuotexProvider

    p = PyQuotexProvider("recovery@example.invalid", "pw", True)
    p._client = client
    p._connected = True
    return p


@pytest.mark.asyncio
async def test_recover_result_classifies_a_win():
    p = _provider(_HistoryClient(history=("win", {"ticket": "42", "profitAmount": 8.75})))
    r = await p.recover_result("42")
    assert r["status"] == "win"
    assert r["profit"] == pytest.approx(8.75)


@pytest.mark.asyncio
async def test_recover_result_classifies_a_loss():
    p = _provider(_HistoryClient(history=("loss", {"ticket": "42", "profitAmount": -5.0})))
    r = await p.recover_result("42")
    assert r["status"] == "loss"
    assert r["profit"] == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_recover_result_treats_a_returned_stake_as_a_draw():
    """The vendor would report this as a loss (`profitAmount > 0` else loss).
    A refunded stake must not trip consecutive_losses / _martingale_step."""
    p = _provider(_HistoryClient(history=("loss", {"ticket": "42", "profitAmount": 0})))
    r = await p.recover_result("42")
    assert r["status"] == "draw"


@pytest.mark.asyncio
async def test_recover_result_ignores_the_vendor_verdict_string():
    """Classification is on the money, exactly as in check_result (RCA F7)."""
    p = _provider(_HistoryClient(history=("win", {"ticket": "42", "profitAmount": -3.0})))
    assert (await p.recover_result("42"))["status"] == "loss"


@pytest.mark.asyncio
async def test_recover_result_returns_none_when_the_broker_has_no_record():
    p = _provider(_HistoryClient(history=(None, "OperationID Not Found.")))
    assert await p.recover_result("42") is None


@pytest.mark.asyncio
async def test_recover_result_is_bounded(monkeypatch):
    from app.services import market as market_mod

    monkeypatch.setattr(market_mod, "CHECK_RESULT_TIMEOUT", 0.2)
    p = _provider(_HistoryClient(history=("win", {"ticket": "42", "profitAmount": 1.0}), delay=5.0))

    started = time.monotonic()
    assert await p.recover_result("42") is None
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_recover_result_swallows_provider_errors():
    p = _provider(_HistoryClient(raises=RuntimeError("socket gone")))
    assert await p.recover_result("42") is None


@pytest.mark.asyncio
async def test_paper_provider_has_no_durable_history():
    """The base default must be a graceful None, so recovery is a no-op for
    providers that cannot answer -- it must never raise."""
    from app.services.market import PublicDataProvider

    assert await PublicDataProvider().recover_result("42") is None


# --------------------------------------------------------------------------- #
# Orchestrator-level recovery
# --------------------------------------------------------------------------- #

def _trade(oid: str, created_at: float) -> TradeRecord:
    """`created_at` is TradeRecord's entry timestamp -- it has no `opened_at`
    field, and passing one is silently ignored by pydantic."""
    return TradeRecord(
        id=oid,
        signal_id=f"sig-{oid}",
        asset="EURUSD",
        timeframe="1m",
        direction=Direction.CALL,
        amount=10.0,
        open_price=1.1,
        payout=87.0,
        duration=60,
        created_at=created_at,
        status=TradeStatus.OPEN,
        is_demo=True,
    )


class _RecoveringProvider:
    """Returns `results[oid]` from recover_result; records check_result calls."""

    name = "pyquotex"
    connected = False

    def __init__(self, results=None):
        self.results = results or {}
        self.recovered_calls = []
        self.check_calls = []

    def supports_tournaments(self):
        return True

    async def recover_result(self, oid):
        self.recovered_calls.append(oid)
        return self.results.get(str(oid))

    async def check_result(self, oid):
        self.check_calls.append(oid)
        return None


@pytest.fixture()
def orch(monkeypatch, pg, tmp_path):
    """A real Orchestrator with an isolated TradeStore."""
    from app.config import RuntimeSettings
    from app.orchestrator import Orchestrator
    from app.session_manager import BrokerCredentials, session_manager

    creds = BrokerCredentials(
        quotex_email="broker@example.invalid", quotex_password="pw", quotex_is_demo=True
    )
    monkeypatch.setattr(session_manager, "get_credentials", lambda uid: creds)
    monkeypatch.setattr(session_manager, "set_account_type", lambda uid, d: True)
    monkeypatch.setattr(RuntimeSettings, "save", lambda self, user_id=None: None)

    o = Orchestrator(USER, RuntimeSettings())
    o.store = TradeStore(USER, scope_dir=tmp_path)
    return o


@pytest.mark.asyncio
async def test_open_trades_are_not_deleted_on_restart(orch):
    """THE regression. Pre-fix this wiped every open trade from the ledger."""
    orch.provider = _RecoveringProvider()
    now = time.time()
    for oid in ("a", "b"):
        orch.store.add(_trade(oid, now))

    assert len(orch.store.all()) == 2
    await orch._recover_open_trades()

    remaining = orch.store.all()
    assert len(remaining) == 2, "open trades were deleted instead of recovered"
    assert {t.id for t in remaining} == {"a", "b"}


@pytest.mark.asyncio
async def test_recovered_trade_is_queued_and_re_adopted(orch):
    orch.provider = _RecoveringProvider(results={"a": {"status": "win", "profit": 8.75}})
    orch.store.add(_trade("a", time.time()))

    await orch._recover_open_trades()

    assert orch._recovered_results == {"a": {"status": "win", "profit": 8.75}}
    assert "a" in orch.active_trades, "the trade was not put back under tracking"


@pytest.mark.asyncio
async def test_unknown_but_recent_trade_is_still_tracked(orch):
    """The broker has no record yet -- the order may still be running, so it
    must keep being tracked rather than dropped."""
    orch.provider = _RecoveringProvider()          # recovers nothing
    orch.store.add(_trade("live", time.time() - 30))

    await orch._recover_open_trades()

    assert "live" in orch.active_trades
    assert orch.store.all()[0].status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_unknown_and_ancient_trade_is_marked_error_not_deleted(orch):
    from app.orchestrator import UNRECOVERABLE_TRADE_AFTER_SECONDS

    orch.provider = _RecoveringProvider()
    orch.store.add(_trade("ancient", time.time() - UNRECOVERABLE_TRADE_AFTER_SECONDS - 60))

    await orch._recover_open_trades()

    trades = orch.store.all()
    assert len(trades) == 1, "the unresolved trade was deleted"
    assert trades[0].status == TradeStatus.ERROR, "it must be visibly unresolved"
    assert "ancient" not in orch.active_trades, "it must stop holding a concurrency slot"


@pytest.mark.asyncio
async def test_mixed_batch_is_handled_per_trade(orch):
    from app.orchestrator import UNRECOVERABLE_TRADE_AFTER_SECONDS

    now = time.time()
    orch.provider = _RecoveringProvider(results={"won": {"status": "win", "profit": 9.0}})
    orch.store.add(_trade("won", now))
    orch.store.add(_trade("recent", now - 10))
    orch.store.add(_trade("ancient", now - UNRECOVERABLE_TRADE_AFTER_SECONDS - 10))

    await orch._recover_open_trades()

    assert "won" in orch._recovered_results
    assert "recent" in orch.active_trades
    assert "ancient" not in orch.active_trades
    by_id = {t.id: t for t in orch.store.all()}
    assert by_id["ancient"].status == TradeStatus.ERROR
    assert by_id["won"].status == TradeStatus.OPEN     # settled later by _result_loop
    assert by_id["recent"].status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_no_open_trades_is_a_noop(orch):
    orch.provider = _RecoveringProvider()
    await orch._recover_open_trades()
    assert orch._recovered_results == {}
    assert orch.active_trades == {}
    assert orch.provider.recovered_calls == []


@pytest.mark.asyncio
async def test_a_raising_provider_does_not_abort_the_recovery(orch, monkeypatch):
    class _Boom:
        name = "pyquotex"

        async def recover_result(self, oid):
            raise RuntimeError("socket gone")

    orch.provider = _Boom()
    orch.store.add(_trade("a", time.time()))

    await orch._recover_open_trades()          # must not raise

    assert len(orch.store.all()) == 1, "the trade was deleted after a provider error"
    assert "a" in orch.active_trades


@pytest.mark.asyncio
async def test_bounded_check_result_uses_the_recovered_result(orch):
    """After a restart the websocket slot is gone, so these orders must be
    settled from the recovered result instead of waiting out a timeout."""
    orch.provider = _RecoveringProvider()
    orch._recovered_results["a"] = {"status": "win", "profit": 8.75}

    got = await orch._bounded_check_result("a")

    assert got == {"status": "win", "profit": 8.75}
    assert orch.provider.check_calls == [], "it waited on check_result for an already-settled trade"
    assert "a" not in orch._recovered_results, "the recovered result was not consumed"


@pytest.mark.asyncio
async def test_bounded_check_result_falls_through_when_nothing_was_recovered(orch):
    orch.provider = _RecoveringProvider()
    assert await orch._bounded_check_result("zz") is None
    assert orch.provider.check_calls == ["zz"]


@pytest.mark.asyncio
async def test_start_runs_the_recovery(orch, monkeypatch):
    """The wiring: start() must actually call the recovery, and must not
    delete anything."""
    orch.provider = _RecoveringProvider(results={"a": {"status": "loss", "profit": -10.0}})
    orch.store.add(_trade("a", time.time()))

    calls = []
    real = orch._recover_open_trades

    async def _spy():
        calls.append(True)
        return await real()

    monkeypatch.setattr(orch, "_recover_open_trades", _spy)
    # start() launches the background loops; stub the supervisor so the test
    # does not leave tasks running.
    monkeypatch.setattr(orch._supervisor, "supervise", lambda *a, **k: None)

    await orch.start()
    try:
        assert calls == [True], "start() did not run the restart recovery"
        assert len(orch.store.all()) == 1, "start() deleted the open trade"
    finally:
        orch._running = False
