"""Broker abstraction. Strategy, risk and pipeline code depend only on this interface."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from trader.broker.calendar import NEW_YORK, is_regular_session, next_trading_day
from trader.domain import AccountSnapshot, Fill, OrderRequest, OrderResult, Quote

__all__ = ["NEW_YORK", "Broker", "BrokerError", "is_regular_session", "market_date", "next_business_day"]


class BrokerError(RuntimeError):
    pass


@runtime_checkable
class Broker(Protocol):
    name: str

    def account(self) -> AccountSnapshot: ...

    def quote(self, ticker: str) -> Quote | None: ...

    def place(self, order: OrderRequest) -> OrderResult: ...

    def cancel(self, client_order_id: str) -> OrderResult | None: ...

    def order_status(self, client_order_id: str) -> OrderResult | None:
        """None means the broker positively does not know the order. A broker that cannot tell
        (disconnected, error) must raise BrokerError instead of returning None."""
        ...

    def fills_since(self, since: datetime | None) -> list[Fill]: ...


def next_business_day(day: date) -> date:
    """Next NYSE trading day (used for T+1 settlement in the simulator)."""
    return next_trading_day(day)


def market_date(ts: datetime) -> date:
    return ts.astimezone(NEW_YORK).date()
