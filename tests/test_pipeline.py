from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.conftest import NOW
from trader.audit import AuditLog
from trader.broker.commissions import IbkrTieredUS
from trader.broker.simulated import SimulatedBroker
from trader.config import RiskLimits, Settings
from trader.db import Database, EquitySnapshotRow
from trader.domain import OrderRequest, OrderStatus, OrderType, Quote, Side, Signal
from trader.pipeline import TradingSystem


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def quote(ticker: str, price: str, ts: datetime, spread: str = "0.02") -> Quote:
    p = D(price)
    half = D(spread) / 2
    return Quote(
        ticker=ticker, ts=ts, bid=p - half, ask=p + half, last=p, avg_daily_dollar_volume=D("100000000")
    )


class Recorder:
    def __init__(self) -> None:
        self.pending: list[dict[str, object]] = []
        self.messages: list[str] = []

    def notify_pending(self, signal: dict[str, object]) -> None:
        self.pending.append(signal)

    def notify(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 't.sqlite'}")
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock, slippage_bps=D("0"))
    broker.set_quote(quote("ACME", "50", clock()))
    notifier = Recorder()
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=RiskLimits(),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        notifier=notifier,
        clock=clock,
    )
    return system, broker, clock, notifier


def make_signal(ticker: str = "ACME", key: str = "k1", horizon: int = 30, at: datetime = NOW) -> Signal:
    return Signal(
        source="test",
        ticker=ticker,
        horizon_days=horizon,
        conviction=D("0.8"),
        stop_loss_pct=D("0.08"),
        thesis="test thesis",
        dedup_key=key,
        created_at=at,
    )


def test_full_lifecycle_entry_approval_fill_stop_exit(env) -> None:  # type: ignore[no-untyped-def]
    system, broker, clock, notifier = env
    sig = make_signal()
    assert system.submit_signal(sig)
    assert not system.submit_signal(make_signal())  # dedup

    report = system.run_cycle()
    assert report.in_session and report.pending_approval == 1
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "PENDING_APPROVAL"
    assert notifier.pending and notifier.pending[0]["id"] == sig.id
    planned_qty = D(s["plan"]["entry"]["qty"])

    ok, _ = system.decide(sig.id, approve=True, actor="tester")
    assert ok
    clock.advance(minutes=1)
    broker.set_quote(quote("ACME", "50", clock()))
    report = system.run_cycle()
    assert report.executed == 1 and report.new_fills >= 1
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "ORDERED"
    status = system.status()
    assert D(status["positions"]["ACME"]["qty"]) == planned_qty  # exactly what was approved
    stop = D(status["positions"]["ACME"]["exit_plan"]["stop_price"])
    assert stop < D("50")

    # Price falls through the stop: exit happens automatically, without approval.
    clock.advance(minutes=5)
    broker.set_quote(quote("ACME", str(stop - D("0.5")), clock()))
    report = system.run_cycle()
    assert report.exits == 1
    assert "ACME" not in system.status()["positions"]
    assert broker.account().unsettled_cash > 0

    # T+1: proceeds settle on the next business day.
    clock.advance(days=1)
    acct = broker.account()
    assert acct.unsettled_cash == 0 and acct.settled_cash > D("900")
    assert system.audit.verify().ok


def test_human_rejection_and_double_decision(env) -> None:  # type: ignore[no-untyped-def]
    system, *_ = env
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    assert system.decide(sig.id, approve=False, actor="tester") == (True, "rejected")
    ok, msg = system.decide(sig.id, approve=True, actor="tester")
    assert not ok and "REJECTED_HUMAN" in msg
    assert system.run_cycle().executed == 0


def test_pending_approval_expires(env) -> None:  # type: ignore[no-untyped-def]
    system, broker, clock, _ = env
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    clock.advance(minutes=RiskLimits().signal_ttl_minutes + 1)
    broker.set_quote(quote("ACME", "50", clock()))
    ok, msg = system.decide(sig.id, approve=True, actor="tester")
    assert not ok and "expired" in msg


def test_restricted_ticker_never_reaches_approval(tmp_path: Path) -> None:
    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'r.sqlite'}")
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock)
    broker.set_quote(quote("ACME", "50", clock()))
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=RiskLimits(restricted=["ACME"]),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        clock=clock,
    )
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "REJECTED_RISK" and "restricted" in s["status_reason"]


def test_outside_session_nothing_happens(env) -> None:  # type: ignore[no-untyped-def]
    system, _, clock, _ = env
    clock.now = datetime(2026, 11, 28, 15, 0, tzinfo=NOW.tzinfo)  # Saturday
    sig = make_signal(at=clock.now)
    system.submit_signal(sig)
    report = system.run_cycle()
    assert not report.in_session
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "PROPOSED"


def test_unreviewed_proposals_expire(env) -> None:  # type: ignore[no-untyped-def]
    system, broker, clock, _ = env
    sig = make_signal()
    system.submit_signal(sig)
    clock.advance(hours=RiskLimits().signal_max_age_hours + 1)
    broker.set_quote(quote("ACME", "50", clock()))
    system.run_cycle()
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "EXPIRED"


def test_kill_switch_blocks_new_entries(env) -> None:  # type: ignore[no-untyped-def]
    system, *_ = env
    system.set_kill_switch(True, reason="manual test", actor="tester")
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "REJECTED_RISK" and "kill_switch" in s["status_reason"]


def test_drawdown_trips_kill_switch(env) -> None:  # type: ignore[no-untyped-def]
    system, *_ = env
    with system.db.session() as s:
        s.add(
            EquitySnapshotRow(
                ts=NOW - timedelta(days=10),
                equity=D("1300"),
                settled_cash=D("1300"),
                unsettled_cash=D("0"),
                positions_value=D("0"),
            )
        )
    system.run_cycle()
    assert system.kill_switch_on()


def test_time_based_exit(env) -> None:  # type: ignore[no-untyped-def]
    system, broker, clock, _ = env
    sig = make_signal(horizon=2)
    system.submit_signal(sig)
    system.run_cycle()
    system.decide(sig.id, approve=True, actor="tester")
    system.run_cycle()
    assert "ACME" in system.status()["positions"]
    clock.advance(days=2, minutes=1)
    broker.set_quote(quote("ACME", "51", clock()))
    report = system.run_cycle()
    assert report.exits == 1
    assert "ACME" not in system.status()["positions"]


def test_auto_mode_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError):
        Settings(approval_mode="auto", allow_auto_execution=False)
    assert Settings(approval_mode="auto", allow_auto_execution=True).approval_mode == "auto"


# ------------------------------------------------------------ simulated broker


def test_sim_broker_cash_account_rules() -> None:
    clock = Clock(NOW)
    b = SimulatedBroker(settled_cash=D("100"), clock=clock, slippage_bps=D("0"))
    b.set_quote(quote("ACME", "50", clock()))
    too_big = b.place(OrderRequest(ticker="ACME", side=Side.BUY, qty=D("3"), limit_price=D("51")))
    assert too_big.status is OrderStatus.REJECTED
    short = b.place(OrderRequest(ticker="ACME", side=Side.SELL, qty=D("1"), limit_price=D("49")))
    assert short.status is OrderStatus.REJECTED

    resting = OrderRequest(ticker="ACME", side=Side.BUY, qty=D("1"), limit_price=D("45"))
    assert b.place(resting).status is OrderStatus.OPEN
    assert b.place(resting).status is OrderStatus.OPEN  # idempotent
    b.set_quote(quote("ACME", "44.9", clock()))
    result = b.order_status(resting.client_order_id)
    assert result is not None and result.status is OrderStatus.FILLED
    assert result.avg_fill_price is not None and result.avg_fill_price <= D("45")
    assert len(b.fills) == 1


def test_sim_broker_state_roundtrip() -> None:
    clock = Clock(NOW)
    b = SimulatedBroker(settled_cash=D("1000"), clock=clock)
    b.set_quote(quote("ACME", "50", clock()))
    b.place(OrderRequest(ticker="ACME", side=Side.BUY, qty=D("1.5"), limit_price=D("51")))
    b.place(OrderRequest(ticker="ACME", side=Side.SELL, qty=D("0.5"), order_type=OrderType.MARKET))
    restored = SimulatedBroker.from_state(b.to_state(), clock=clock)
    assert restored.account() == b.account()
    assert restored.fills == b.fills
