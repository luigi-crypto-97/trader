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
from trader.domain import AccountSnapshot, OrderRequest, OrderStatus, OrderType, Position, Quote, Side, Signal
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

    def system_with(restricted: list[str]) -> TradingSystem:
        return TradingSystem(
            db=db,
            broker=broker,
            limits=RiskLimits(restricted=restricted),
            commissions=IbkrTieredUS(),
            audit=AuditLog(db),
            clock=clock,
        )

    assert not system_with(["ACME"]).submit_signal(make_signal(key="r0"))  # dropped at the door
    sig = make_signal(key="r1")
    assert system_with([]).submit_signal(sig)  # queued before the list changed
    restricted = system_with(["ACME"])
    restricted.run_cycle()
    assert restricted.get_signal(sig.id) is None  # deleted: no row links the ticker to the list
    dropped = [r for r in restricted.audit.tail(100) if r["event"] == "signal.dropped"]
    assert dropped and all(sig.id not in r["payload"] and "ACME" not in r["payload"] for r in dropped)


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
    clock.advance(days=3, minutes=1)  # Thu 26/11/2026 is Thanksgiving (closed): exit on Friday
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


def test_second_signal_on_same_ticker_blocked_while_first_order_pending(env) -> None:  # type: ignore[no-untyped-def]
    system, broker, clock, _ = env
    first, second = make_signal(key="k-a"), make_signal(key="k-b")
    system.submit_signal(first)
    system.submit_signal(second)
    system.run_cycle()
    assert system.decide(first.id, approve=True, actor="t")[0]
    assert system.decide(second.id, approve=True, actor="t")[0]
    # The market moves up 1%: the approved limit (ask + 0.2%) no longer crosses, the order rests.
    clock.advance(minutes=1)
    broker.set_quote(quote("ACME", "50.50", clock()))
    report = system.run_cycle()
    assert report.executed == 1
    statuses = {system.get_signal(first.id)["status"], system.get_signal(second.id)["status"]}  # type: ignore[index]
    assert statuses == {"ORDERED", "FAILED"}
    failed = next(
        s for s in (system.get_signal(first.id), system.get_signal(second.id)) if s["status"] == "FAILED"
    )  # type: ignore[index]
    assert "no_add_to_position" in failed["status_reason"]  # type: ignore[index]
    assert len([o for o in broker.orders.values() if o.request.side.value == "BUY"]) == 1


def test_pending_order_on_other_ticker_uses_a_position_slot(tmp_path: Path) -> None:
    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'slots.sqlite'}")
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock, slippage_bps=D("0"))
    broker.set_quote(quote("ACME", "50", clock()))
    broker.set_quote(quote("BETA", "40", clock()))
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=RiskLimits(max_positions=1),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        clock=clock,
    )
    a, b = make_signal("ACME", key="ka"), make_signal("BETA", key="kb")
    system.submit_signal(a)
    system.run_cycle()
    system.decide(a.id, approve=True, actor="t")
    clock.advance(minutes=1)
    broker.set_quote(quote("ACME", "50.50", clock()))  # limit no longer crosses: ACME order rests
    system.run_cycle()
    assert system.get_signal(a.id)["status"] == "ORDERED"  # type: ignore[index]
    system.submit_signal(b)
    clock.advance(minutes=1)
    broker.set_quote(quote("BETA", "40", clock()))
    system.run_cycle()
    s_b = system.get_signal(b.id)
    assert s_b is not None and s_b["status"] == "REJECTED_RISK" and "max_positions" in s_b["status_reason"]


def test_restricted_signal_dropped_without_trace(tmp_path: Path) -> None:
    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'priv.sqlite'}")
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock)
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=RiskLimits(restricted=["SECRT"]),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        clock=clock,
    )
    assert not system.submit_signal(make_signal("SECRT", key="p1"))
    assert system.list_signals() == []
    assert "SECRT" not in str(system.audit.tail(50))


def big_limits() -> RiskLimits:
    return RiskLimits(risk_per_trade_pct=D("0.1"), max_position_pct=D("0.6"))


def test_committed_cash_blocks_second_order_and_shrinks_next_size(tmp_path: Path) -> None:
    from trader.db import OrderRow

    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'cash.sqlite'}")
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock, slippage_bps=D("0"))
    for t in ("ACME", "BETA", "GAMA"):
        broker.set_quote(quote(t, "50", clock()))
    system = TradingSystem(
        db=db, broker=broker, limits=big_limits(), commissions=IbkrTieredUS(), audit=AuditLog(db), clock=clock
    )
    a, b = make_signal("ACME", key="ca"), make_signal("BETA", key="cb")
    system.submit_signal(a)
    system.submit_signal(b)
    system.run_cycle()
    assert system.decide(a.id, approve=True, actor="t")[0] and system.decide(b.id, approve=True, actor="t")[0]
    clock.advance(minutes=1)
    broker.set_quote(quote("ACME", "50.50", clock()))  # ACME limit no longer crosses: it rests
    broker.set_quote(quote("BETA", "50", clock()))
    system.run_cycle()
    sa, sb = system.get_signal(a.id), system.get_signal(b.id)
    assert sa is not None and sa["status"] == "ORDERED"
    assert sb is not None and sb["status"] == "FAILED" and "settled_cash" in sb["status_reason"]

    # A new proposal is sized on the cash not committed, without shrinking equity.
    g = make_signal("GAMA", key="cg")
    system.submit_signal(g)
    clock.advance(minutes=1)
    broker.set_quote(quote("GAMA", "50", clock()))
    system.review_proposed(broker.account())
    plan = system.get_signal(g.id)["plan"]["entry"]  # type: ignore[index]
    assert plan["binding_constraint"] == "settled_cash"
    assert D(plan["notional"]) <= D("1000") - system.risk_state().pending_total

    # Exact commitment of a partially filled order: remaining qty x limit + commission.
    with db.session() as s:
        row = s.get(OrderRow, a.id)
        assert row is not None and row.limit_price is not None
        row.status, row.filled_qty = "PARTIALLY_FILLED", row.qty / 2
        remaining, limit = row.qty - row.qty / 2, row.limit_price
    expected = remaining * limit + IbkrTieredUS().estimate(remaining, limit)
    assert system.risk_state().pending_buys == {"ACME": expected}


def test_orders_unknown_to_broker_are_reconciled_next_day(tmp_path: Path) -> None:
    from trader.db import OrderRow

    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'rec.sqlite'}")
    notifier = Recorder()
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock)
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=RiskLimits(),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        notifier=notifier,
        clock=clock,
    )
    with db.session() as s:
        s.add(
            OrderRow(
                client_order_id="ghost",
                signal_id=None,
                purpose="EXIT_STOP",
                ticker="ACME",
                side="SELL",
                qty=D("1"),
                order_type="LIMIT",
                limit_price=D("40"),
                status="OPEN",
                filled_qty=D("0"),
                commission=D("0"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
    system.sync()
    with db.session() as s:
        assert s.get(OrderRow, "ghost").status == "OPEN"  # type: ignore[union-attr]  # same session: wait
    clock.advance(days=1)
    system.sync()
    with db.session() as s:
        assert s.get(OrderRow, "ghost").status == "CANCELLED"  # type: ignore[union-attr]
    assert any("non più noto al broker" in m for m in notifier.messages)


class RaisingNotifier(Recorder):
    def notify(self, text: str) -> None:
        raise RuntimeError("telegram down")


def ghost_order(db: Database, coid: str, created: datetime, side: str = "SELL") -> None:
    from trader.db import OrderRow

    with db.session() as s:
        s.add(
            OrderRow(
                client_order_id=coid,
                signal_id=None,
                purpose="EXIT_STOP",
                ticker="ACME",
                side=side,
                qty=D("1"),
                order_type="LIMIT",
                limit_price=D("40"),
                status="OPEN",
                filled_qty=D("0"),
                commission=D("0"),
                created_at=created,
                updated_at=created,
            )
        )


def order_status(db: Database, coid: str) -> str:
    from trader.db import OrderRow

    with db.session() as s:
        row = s.get(OrderRow, coid)
        assert row is not None
        return row.status


def test_order_still_known_to_broker_is_never_reconciled(env) -> None:  # type: ignore[no-untyped-def]
    system, _broker, clock, _ = env
    resting = OrderRequest(ticker="ACME", side=Side.BUY, qty=D("1"), limit_price=D("40"))
    system._place(resting, purpose="ENTRY", signal_id=None)  # rests: limit below the market
    clock.advance(days=2)
    system.sync()
    assert order_status(system.db, resting.client_order_id) == "OPEN"


def test_order_sent_on_holiday_survives_until_next_session_closes(tmp_path: Path) -> None:
    thanksgiving_noon = datetime(2026, 11, 26, 17, 0, tzinfo=NOW.tzinfo)  # market closed all day
    clock = Clock(thanksgiving_noon)
    db = Database(f"sqlite:///{tmp_path / 'hol.sqlite'}")
    system = TradingSystem(
        db=db,
        broker=SimulatedBroker(settled_cash=D("1000"), clock=clock),
        limits=RiskLimits(),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        clock=clock,
    )
    ghost_order(db, "h1", thanksgiving_noon)
    clock.now = datetime(2026, 11, 27, 16, 0, tzinfo=NOW.tzinfo)  # Friday 11:00 ET, half day still open
    system.sync()
    assert order_status(db, "h1") == "OPEN"
    clock.now = datetime(2026, 11, 27, 18, 31, tzinfo=NOW.tzinfo)  # 13:31 ET: early close + grace passed
    system.sync()
    assert order_status(db, "h1") == "CANCELLED"


def test_notification_failure_does_not_stop_exits(tmp_path: Path) -> None:
    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'nf.sqlite'}")
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock, slippage_bps=D("0"))
    broker.set_quote(quote("ACME", "50", clock()))
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=RiskLimits(),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        notifier=RaisingNotifier(),
        clock=clock,
    )
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    system.decide(sig.id, approve=True, actor="t")
    system.run_cycle()  # fill notification raises inside sync: must be swallowed
    ghost_order(db, "g1", NOW - timedelta(days=3))  # reconciliation notification raises too
    clock.advance(minutes=5)
    stop = D(system.status()["positions"]["ACME"]["exit_plan"]["stop_price"])
    broker.set_quote(quote("ACME", str(stop - 1), clock()))
    report = system.run_cycle()
    assert report.exits == 1 and order_status(db, "g1") == "CANCELLED"


def test_open_sell_prevents_a_second_sell(env) -> None:  # type: ignore[no-untyped-def]
    from trader.risk.engine import RiskState

    system, _broker, _clock, _ = env
    acct = AccountSnapshot(
        ts=NOW, settled_cash=D("0"), positions={"ACME": Position(ticker="ACME", qty=D("2"), avg_cost=D("50"))}
    )
    sell = OrderRequest(ticker="ACME", side=Side.SELL, qty=D("2"), order_type=OrderType.MARKET)
    state = RiskState(False, 0, None, None, pending_sells={"ACME": D("2")})
    d = system.risk.evaluate(sell, quote=None, account=acct, state=state, now=NOW)
    assert not d.approved and "long_only_no_short" in d.summary()


def test_signal_dropped_at_execution_when_ticker_becomes_restricted(env) -> None:  # type: ignore[no-untyped-def]
    system, _broker, _clock, _ = env
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    system.decide(sig.id, approve=True, actor="t")
    system.limits = RiskLimits(restricted=["ACME"])
    system.risk = type(system.risk)(system.limits, system.commissions)
    report = system.run_cycle()
    assert report.executed == 0 and system.get_signal(sig.id) is None
    assert not [r for r in system.audit.tail(200) if r["event"] == "order.submit"]


class FlakyPlaceBroker(SimulatedBroker):
    """Accepts the order, then the connection drops before the reply arrives."""

    def place(self, order: OrderRequest):  # type: ignore[no-untyped-def]
        super().place(order)
        raise ConnectionError("socket closed after submit")


def test_uncertain_submission_stays_open_and_blocks_a_second_sell(tmp_path: Path) -> None:
    from trader.broker.base import BrokerError

    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'flaky.sqlite'}")
    broker = FlakyPlaceBroker(settled_cash=D("1000"), clock=clock)
    broker.set_quote(quote("ACME", "50", clock()))
    system = TradingSystem(
        db=db, broker=broker, limits=RiskLimits(), commissions=IbkrTieredUS(), audit=AuditLog(db), clock=clock
    )
    sell = OrderRequest(ticker="ACME", side=Side.SELL, qty=D("1"), limit_price=D("49"))
    result = system._place(sell, purpose="EXIT_STOP", signal_id=None)
    assert result.status is OrderStatus.OPEN and "unknown" in result.message
    assert system.risk_state().pending_sells.get("ACME") == D("1")

    class SureFailBroker(SimulatedBroker):
        def place(self, order: OrderRequest):  # type: ignore[no-untyped-def]
            raise BrokerError("cannot qualify contract")

    system.broker = SureFailBroker(settled_cash=D("1000"), clock=clock)
    sure = system._place(
        OrderRequest(ticker="ZZZ", side=Side.BUY, qty=D("1"), limit_price=D("10")),
        purpose="ENTRY",
        signal_id=None,
    )
    assert sure.status is OrderStatus.REJECTED


def test_only_day_orders() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError):
        OrderRequest(ticker="ACME", side=Side.BUY, qty=D("1"), limit_price=D("10"), tif="GTC")  # type: ignore[arg-type]


def test_pending_sells_boundaries(env) -> None:  # type: ignore[no-untyped-def]
    from trader.risk.engine import RiskState

    system, *_ = env
    acct = AccountSnapshot(
        ts=NOW, settled_cash=D("0"), positions={"ACME": Position(ticker="ACME", qty=D("3"), avg_cost=D("50"))}
    )
    sell2 = OrderRequest(ticker="ACME", side=Side.SELL, qty=D("2"), order_type=OrderType.MARKET)
    ok = RiskState(False, 0, None, None, pending_sells={"ACME": D("1"), "OTHER": D("100")})
    assert system.risk.evaluate(sell2, quote=None, account=acct, state=ok, now=NOW).approved
    too_much = RiskState(False, 0, None, None, pending_sells={"ACME": D("1.5")})
    assert not system.risk.evaluate(sell2, quote=None, account=acct, state=too_much, now=NOW).approved
