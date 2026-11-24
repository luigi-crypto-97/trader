"""In-process simulated broker for tests, dry runs and demos.

Models a US *cash* account: buys are paid only from settled cash, sale proceeds settle T+1,
no short selling, limit orders fill only when marketable against the current quote, with a
configurable adverse slippage. Its state can be serialised and persisted between runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from trader.broker.base import market_date, next_business_day
from trader.broker.commissions import CommissionModel, IbkrTieredUS
from trader.domain import (
    ZERO,
    AccountSnapshot,
    Fill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    new_id,
)

BPS = Decimal("0.0001")


@dataclass
class _Holding:
    qty: Decimal
    avg_cost: Decimal


@dataclass
class _Order:
    request: OrderRequest
    result: OrderResult


@dataclass
class SimulatedBroker:
    settled_cash: Decimal
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    commissions: CommissionModel = field(default_factory=IbkrTieredUS)
    slippage_bps: Decimal = Decimal("5")
    name: str = "sim"
    pending: list[tuple[date, Decimal]] = field(default_factory=list)
    holdings: dict[str, _Holding] = field(default_factory=dict)
    orders: dict[str, _Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    quotes: dict[str, Quote] = field(default_factory=dict)

    # ----------------------------------------------------------- market data
    def set_quote(self, quote: Quote) -> None:
        self.quotes[quote.ticker] = quote
        self._work_open_orders(quote.ticker)

    def quote(self, ticker: str) -> Quote | None:
        return self.quotes.get(ticker.upper())

    # --------------------------------------------------------------- account
    def _settle(self) -> None:
        today = market_date(self.clock())
        due = [amount for d, amount in self.pending if d <= today]
        self.pending = [(d, amount) for d, amount in self.pending if d > today]
        self.settled_cash += sum(due, ZERO)

    def account(self) -> AccountSnapshot:
        self._settle()
        positions = {}
        for ticker, h in self.holdings.items():
            q = self.quotes.get(ticker)
            price = q.ref_price if q else None
            positions[ticker] = Position(ticker=ticker, qty=h.qty, avg_cost=h.avg_cost, market_price=price)
        return AccountSnapshot(
            ts=self.clock(),
            settled_cash=self.settled_cash,
            unsettled_cash=sum((a for _, a in self.pending), ZERO),
            positions=positions,
        )

    # ---------------------------------------------------------------- orders
    def place(self, order: OrderRequest) -> OrderResult:
        if order.client_order_id in self.orders:  # idempotent resubmission
            return self.orders[order.client_order_id].result
        self._settle()
        result = OrderResult(
            client_order_id=order.client_order_id,
            broker_order_id=f"SIM-{new_id()[:12]}",
            status=OrderStatus.OPEN,
        )
        self.orders[order.client_order_id] = _Order(order, result)
        reject = self._precheck(order)
        if reject:
            return self._update(order.client_order_id, status=OrderStatus.REJECTED, message=reject)
        self._try_fill(order.client_order_id)
        return self.orders[order.client_order_id].result

    def cancel(self, client_order_id: str) -> OrderResult | None:
        entry = self.orders.get(client_order_id)
        if entry is None:
            return None
        if entry.result.status.is_terminal:
            return entry.result
        return self._update(client_order_id, status=OrderStatus.CANCELLED, message="cancelled")

    def order_status(self, client_order_id: str) -> OrderResult | None:
        entry = self.orders.get(client_order_id)
        return entry.result if entry else None

    def fills_since(self, since: datetime | None) -> list[Fill]:
        return [f for f in self.fills if since is None or f.ts > since]

    # -------------------------------------------------------------- internals
    def _precheck(self, order: OrderRequest) -> str | None:
        if self.quotes.get(order.ticker) is None:
            return "no market data for ticker"
        if order.side is Side.SELL:
            held = self.holdings.get(order.ticker)
            if held is None or order.qty > held.qty:
                return "short selling not allowed in cash account"
        return None

    def _work_open_orders(self, ticker: str) -> None:
        for coid, entry in list(self.orders.items()):
            if entry.request.ticker == ticker and entry.result.status is OrderStatus.OPEN:
                self._try_fill(coid)

    def _fill_price(self, order: OrderRequest, quote: Quote) -> Decimal | None:
        slip = self.slippage_bps * BPS
        if order.side is Side.BUY:
            touch = quote.ask if quote.ask is not None else quote.last
            if touch is None:
                return None
            price = touch * (1 + slip)
            if order.order_type is OrderType.LIMIT:
                assert order.limit_price is not None
                if order.limit_price < touch:
                    return None
                price = min(price, order.limit_price)
        else:
            touch = quote.bid if quote.bid is not None else quote.last
            if touch is None:
                return None
            price = touch * (1 - slip)
            if order.order_type is OrderType.LIMIT:
                assert order.limit_price is not None
                if order.limit_price > touch:
                    return None
                price = max(price, order.limit_price)
        return price.quantize(Decimal("0.0001"))

    def _try_fill(self, coid: str) -> None:
        entry = self.orders[coid]
        order = entry.request
        quote = self.quotes.get(order.ticker)
        if quote is None:
            return
        price = self._fill_price(order, quote)
        if price is None:
            return
        commission = self.commissions.estimate(order.qty, price)
        if order.side is Side.BUY:
            cost = order.qty * price + commission
            if cost > self.settled_cash:
                self._update(coid, status=OrderStatus.REJECTED, message="insufficient settled cash at fill")
                return
            self.settled_cash -= cost
            h = self.holdings.get(order.ticker)
            if h is None:
                self.holdings[order.ticker] = _Holding(order.qty, price)
            else:
                total = h.qty + order.qty
                h.avg_cost = ((h.qty * h.avg_cost) + order.qty * price) / total
                h.qty = total
        else:
            h = self.holdings.get(order.ticker)
            if h is None or order.qty > h.qty:
                self._update(coid, status=OrderStatus.REJECTED, message="position changed; cannot sell")
                return
            h.qty -= order.qty
            if h.qty == 0:
                del self.holdings[order.ticker]
            proceeds = order.qty * price - commission
            self.pending.append((next_business_day(market_date(self.clock())), proceeds))
        fill = Fill(
            fill_id=f"F-{new_id()[:16]}",
            client_order_id=coid,
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            price=price,
            commission=commission,
            ts=self.clock(),
        )
        self.fills.append(fill)
        self._update(
            coid,
            status=OrderStatus.FILLED,
            filled_qty=order.qty,
            avg_fill_price=price,
            commission=commission,
            message="filled",
        )

    def _update(self, coid: str, **changes: Any) -> OrderResult:
        entry = self.orders[coid]
        entry.result = entry.result.model_copy(update=changes)
        return entry.result

    # ---------------------------------------------------------- persistence
    def to_state(self) -> dict[str, Any]:
        return {
            "settled_cash": str(self.settled_cash),
            "pending": [[d.isoformat(), str(a)] for d, a in self.pending],
            "holdings": {t: [str(h.qty), str(h.avg_cost)] for t, h in self.holdings.items()},
            "orders": {
                k: {"request": v.request.model_dump(mode="json"), "result": v.result.model_dump(mode="json")}
                for k, v in self.orders.items()
            },
            "fills": [f.model_dump(mode="json") for f in self.fills],
            "quotes": {t: q.model_dump(mode="json") for t, q in self.quotes.items()},
        }

    @classmethod
    def from_state(cls, state: dict[str, Any], **kwargs: Any) -> SimulatedBroker:
        broker = cls(settled_cash=Decimal(state["settled_cash"]), **kwargs)
        broker.pending = [(date.fromisoformat(d), Decimal(a)) for d, a in state.get("pending", [])]
        broker.holdings = {t: _Holding(Decimal(q), Decimal(c)) for t, (q, c) in state.get("holdings", {}).items()}
        broker.orders = {
            k: _Order(OrderRequest.model_validate(v["request"]), OrderResult.model_validate(v["result"]))
            for k, v in state.get("orders", {}).items()
        }
        broker.fills = [Fill.model_validate(f) for f in state.get("fills", [])]
        broker.quotes = {t: Quote.model_validate(q) for t, q in state.get("quotes", {}).items()}
        return broker
