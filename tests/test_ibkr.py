"""IBKR adapter against a fake ``ib_async.IB``. Live behaviour must still be validated on paper."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS
from typing import Any

import pytest

from trader.broker.ibkr import IbkrBroker, from_ib_symbol, to_ib_symbol
from trader.domain import OrderRequest, OrderStatus, OrderType, Side

NOW = datetime(2026, 11, 24, 15, tzinfo=UTC)
NAN = float("nan")


class FakeIB:
    def __init__(self) -> None:
        self.connected = True
        self.placed: list[tuple[Any, Any]] = []
        self._trades: list[Any] = []
        self._fills: list[Any] = []
        self.cancelled: list[Any] = []
        self.values = [
            NS(tag="TotalCashValue", currency="USD", value="900.50"),
            NS(tag="SettledCash", currency="USD", value="800.25"),
            NS(tag="NetLiquidation", currency="BASE", value="1000"),
        ]
        self.items = [
            NS(
                contract=NS(symbol="BRK B", secType="STK", currency="USD"),
                position=0.5,
                averageCost=480.0,
                marketPrice=490.0,
            ),
            NS(
                contract=NS(symbol="EUFUND", secType="STK", currency="EUR"),
                position=3,
                averageCost=10.0,
                marketPrice=11.0,
            ),
        ]

    def isConnected(self) -> bool:
        return self.connected

    def qualifyContracts(self, c: Any) -> list[Any]:
        return [c] if c.symbol != "NOPE" else [None]

    def reqTickers(self, c: Any) -> list[Any]:
        return [NS(time=NOW, bid=49.9, ask=50.1, last=50.0, close=49.0)]

    def reqHistoricalData(self, c: Any, **kw: Any) -> list[Any]:
        return [NS(close=50.0, volume=1_000_000), NS(close=52.0, volume=500_000)]

    def accountValues(self, account: str) -> list[Any]:
        return self.values

    def portfolio(self, account: str) -> list[Any]:
        return self.items

    def placeOrder(self, contract: Any, order: Any) -> Any:
        order.permId, order.orderId = 777, 12
        trade = NS(
            contract=contract,
            order=order,
            fills=[],
            log=[NS(message="Submitted")],
            orderStatus=NS(status="Submitted", filled=0, avgFillPrice=0.0),
        )
        self.placed.append((contract, order))
        self._trades.append(trade)
        return trade

    def trades(self) -> list[Any]:
        return self._trades

    def cancelOrder(self, order: Any) -> None:
        self.cancelled.append(order)
        for t in self._trades:
            if t.order is order:
                t.orderStatus.status = "Cancelled"

    def fills(self) -> list[Any]:
        return self._fills

    def sleep(self, s: float) -> None:
        return None


@pytest.fixture
def ib() -> FakeIB:
    pytest.importorskip("ib_async")
    return FakeIB()


def test_symbol_mapping() -> None:
    assert to_ib_symbol("BRK-B") == "BRK B" and from_ib_symbol("BRK B") == "BRK-B"


def test_account_uses_settled_cash_and_usd_stocks(ib: FakeIB) -> None:
    acct = IbkrBroker(ib, clock=lambda: NOW).account()
    assert acct.settled_cash == D("800.25") and acct.unsettled_cash == D("100.25")
    assert set(acct.positions) == {"BRK-B"}
    assert acct.positions["BRK-B"].qty == D("0.5")


def test_account_conservative_fallback_without_settled_cash(ib: FakeIB) -> None:
    ib.values = [
        NS(tag="TotalCashValue", currency="USD", value="900"),
        NS(tag="AvailableFunds", currency="USD", value="700"),
    ]
    assert IbkrBroker(ib, clock=lambda: NOW).account().settled_cash == D("700")


def test_quote_with_adv(ib: FakeIB) -> None:
    q = IbkrBroker(ib, clock=lambda: NOW).quote("ACME")
    assert q is not None and q.bid == D("49.9") and q.ask == D("50.1")
    assert q.avg_daily_dollar_volume == D("38000000")
    assert IbkrBroker(ib, clock=lambda: NOW).quote("NOPE") is None


def test_place_is_idempotent_and_tagged(ib: FakeIB) -> None:
    broker = IbkrBroker(ib, clock=lambda: NOW, settle_wait=0)
    order = OrderRequest(ticker="BRK-B", side=Side.BUY, qty=D("0.25"), limit_price=D("490.10"))
    res = broker.place(order)
    assert res.status is OrderStatus.OPEN and res.broker_order_id == "777"
    contract, ib_order = ib.placed[0]
    assert contract.symbol == "BRK B" and ib_order.orderRef == order.client_order_id
    assert ib_order.lmtPrice == 490.10 and ib_order.totalQuantity == 0.25 and ib_order.outsideRth is False
    broker.place(order)
    assert len(ib.placed) == 1
    cancelled = broker.cancel(order.client_order_id)
    assert cancelled is not None and cancelled.status is OrderStatus.CANCELLED


def test_market_order_mapping(ib: FakeIB) -> None:
    broker = IbkrBroker(ib, clock=lambda: NOW, settle_wait=0)
    broker.place(OrderRequest(ticker="ACME", side=Side.SELL, qty=D("1"), order_type=OrderType.MARKET))
    assert ib.placed[0][1].action == "SELL" and ib.placed[0][1].orderType == "MKT"


def test_fills_wait_for_commission_report(ib: FakeIB) -> None:
    ex = NS(execId="E1", orderRef="coid1", orderId=1, side="BOT", shares=0.5, price=490.0, time=NOW)
    ib._fills = [NS(contract=NS(symbol="BRK B"), execution=ex, time=NOW, commissionReport=NS(commission=NAN))]
    broker = IbkrBroker(ib, clock=lambda: NOW)
    assert broker.fills_since(None) == []
    ib._fills[0].commissionReport = NS(commission=0.35)
    (fill,) = broker.fills_since(None)
    assert fill.ticker == "BRK-B" and fill.side is Side.BUY and fill.commission == D("0.35")
    assert fill.client_order_id == "coid1" and fill.qty == D("0.5")


def test_disconnected_gateway_raises_instead_of_reporting_nothing(ib: FakeIB) -> None:
    from trader.broker.base import BrokerError

    broker = IbkrBroker(ib, clock=lambda: NOW, settle_wait=0)
    broker.place(OrderRequest(ticker="ACME", side=Side.BUY, qty=D("1"), limit_price=D("50")))
    ib.connected = False
    ib._trades = []  # what ib_async exposes after a disconnect
    for call in (lambda: broker.order_status("x"), broker.account, lambda: broker.fills_since(None)):
        with pytest.raises(BrokerError):
            call()
