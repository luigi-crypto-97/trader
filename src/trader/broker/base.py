"""Broker abstraction. Strategy, risk and pipeline code depend only on this interface."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from trader.domain import AccountSnapshot, Fill, OrderRequest, OrderResult, Quote

NEW_YORK = ZoneInfo("America/New_York")


class BrokerError(RuntimeError):
    pass


@runtime_checkable
class Broker(Protocol):
    name: str

    def account(self) -> AccountSnapshot: ...

    def quote(self, ticker: str) -> Quote | None: ...

    def place(self, order: OrderRequest) -> OrderResult: ...

    def cancel(self, client_order_id: str) -> OrderResult | None: ...

    def order_status(self, client_order_id: str) -> OrderResult | None: ...

    def fills_since(self, since: datetime | None) -> list[Fill]: ...


def next_business_day(day: date) -> date:
    """Next weekday. US market holidays are not modelled (the broker is the source of truth)."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def market_date(ts: datetime) -> date:
    return ts.astimezone(NEW_YORK).date()


def is_regular_session(ts: datetime) -> bool:
    """NYSE regular session, 09:30-16:00 New York time, Monday-Friday (holidays not modelled)."""
    local = ts.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minutes < 16 * 60
