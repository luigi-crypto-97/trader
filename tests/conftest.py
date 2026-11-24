from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from trader.config import RiskLimits
from trader.domain import AccountSnapshot, Position, Quote

# A Tuesday, 15:00 UTC = 10:00 New York (regular session).
NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("TRADER_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live test; set TRADER_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits()


@pytest.fixture
def make_quote() -> Callable[..., Quote]:
    def _make(
        ticker: str = "ACME",
        bid: str = "49.95",
        ask: str = "50.05",
        last: str = "50.00",
        adv: str | None = "50000000",
        ts: datetime = NOW,
    ) -> Quote:
        return Quote(
            ticker=ticker,
            ts=ts,
            bid=D(bid),
            ask=D(ask),
            last=D(last),
            avg_daily_dollar_volume=D(adv) if adv is not None else None,
        )

    return _make


@pytest.fixture
def make_account() -> Callable[..., AccountSnapshot]:
    def _make(cash: str = "1000", positions: dict[str, tuple[str, str]] | None = None) -> AccountSnapshot:
        pos = {
            t: Position(ticker=t, qty=D(q), avg_cost=D(p), market_price=D(p))
            for t, (q, p) in (positions or {}).items()
        }
        return AccountSnapshot(ts=NOW, settled_cash=D(cash), positions=pos)

    return _make
