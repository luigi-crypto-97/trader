"""Core domain models shared by every layer.

Conventions:
- Money and quantities are ``Decimal``; never ``float``.
- Every ``datetime`` is timezone-aware and expressed in UTC.
- Prices are in the instrument currency (USD for US equities).
"""

from __future__ import annotations

import enum
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ZERO = Decimal("0")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime not allowed; use timezone-aware UTC datetimes")
    return value.astimezone(UTC)


_TICKER_SEP = re.compile(r"[\s./]+")


def normalize_ticker(ticker: str) -> str:
    """Canonical SEC-style ticker: 'BRK.B', 'BRK/B', 'BRK B' (TWS) and 'brk-b' all become 'BRK-B'."""
    return _TICKER_SEP.sub("-", ticker.strip().upper()).strip("-")


def new_id() -> str:
    return uuid.uuid4().hex


class Side(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(enum.StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(enum.StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}


class SignalStatus(enum.StrEnum):
    PROPOSED = "PROPOSED"
    REJECTED_RISK = "REJECTED_RISK"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED_HUMAN = "REJECTED_HUMAN"
    EXPIRED = "EXPIRED"
    ORDERED = "ORDERED"
    FAILED = "FAILED"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Quote(_Model):
    ticker: str
    ts: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    avg_daily_dollar_volume: Decimal | None = None

    v_ts_utc = field_validator("ts")(ensure_utc)

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask >= self.bid:
            return (self.bid + self.ask) / 2
        return None

    @property
    def ref_price(self) -> Decimal | None:
        """Best estimate of fair price: mid when a valid two-sided market exists, else last."""
        mid = self.mid
        if mid is not None:
            return mid
        return self.last if self.last is not None and self.last > 0 else None

    @property
    def spread_pct(self) -> Decimal | None:
        mid = self.mid
        if mid is None or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid


class Position(_Model):
    ticker: str
    qty: Decimal
    avg_cost: Decimal
    market_price: Decimal | None = None

    @property
    def market_value(self) -> Decimal:
        price = self.market_price if self.market_price is not None else self.avg_cost
        return self.qty * price


class AccountSnapshot(_Model):
    ts: datetime
    settled_cash: Decimal
    unsettled_cash: Decimal = ZERO
    positions: dict[str, Position] = Field(default_factory=dict)

    v_ts_utc = field_validator("ts")(ensure_utc)

    @property
    def positions_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions.values()), ZERO)

    @property
    def equity(self) -> Decimal:
        return self.settled_cash + self.unsettled_cash + self.positions_value

    @property
    def gross_exposure(self) -> Decimal:
        return sum((abs(p.market_value) for p in self.positions.values()), ZERO)

    def qty(self, ticker: str) -> Decimal:
        pos = self.positions.get(ticker)
        return pos.qty if pos else ZERO


class OrderRequest(_Model):
    client_order_id: str = Field(default_factory=new_id)
    ticker: str
    side: Side
    qty: Decimal
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None
    tif: str = "DAY"

    @field_validator("qty")
    @classmethod
    def _positive_qty(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("qty must be positive")
        return v

    @model_validator(mode="after")
    def _limit_needs_price(self) -> OrderRequest:
        if self.order_type is OrderType.LIMIT and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("LIMIT orders require a positive limit_price")
        return self

    @property
    def notional(self) -> Decimal:
        """Notional at the limit price (for market orders the caller must price it)."""
        if self.limit_price is None:
            raise ValueError("market order has no intrinsic notional")
        return self.qty * self.limit_price


class OrderResult(_Model):
    client_order_id: str
    broker_order_id: str | None
    status: OrderStatus
    filled_qty: Decimal = ZERO
    avg_fill_price: Decimal | None = None
    commission: Decimal = ZERO
    message: str = ""


class Fill(_Model):
    fill_id: str
    client_order_id: str
    ticker: str
    side: Side
    qty: Decimal
    price: Decimal
    commission: Decimal
    ts: datetime

    v_ts_utc = field_validator("ts")(ensure_utc)


class Signal(_Model):
    """A trade idea. It is only an idea: sizing, risk checks and approval come after."""

    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)
    source: str
    ticker: str
    side: Side = Side.BUY
    horizon_days: int
    conviction: Decimal
    stop_loss_pct: Decimal
    thesis: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    dedup_key: str

    v_created_utc = field_validator("created_at")(ensure_utc)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = normalize_ticker(v)
        if not v or len(v) > 10:
            raise ValueError("invalid ticker")
        return v

    @field_validator("conviction")
    @classmethod
    def _conviction_range(cls, v: Decimal) -> Decimal:
        if not ZERO <= v <= 1:
            raise ValueError("conviction must be in [0, 1]")
        return v

    @field_validator("stop_loss_pct")
    @classmethod
    def _stop_range(cls, v: Decimal) -> Decimal:
        if not ZERO < v < 1:
            raise ValueError("stop_loss_pct must be in (0, 1)")
        return v

    @field_validator("horizon_days")
    @classmethod
    def _horizon_range(cls, v: int) -> int:
        if not 1 <= v <= 365:
            raise ValueError("horizon_days must be in [1, 365]")
        return v
