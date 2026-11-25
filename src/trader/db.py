"""Persistence layer (SQLAlchemy 2). SQLite by default; any SQLAlchemy URL works (e.g. Postgres)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Engine,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class DecimalText(TypeDecorator[Decimal]):
    """Exact decimals stored as text (SQLite has no native decimal type)."""

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"expected Decimal, got {type(value).__name__}")
        return format(value, "f")

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator[datetime]):
    """Aware UTC datetimes in, aware UTC datetimes out, on every backend."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be stored; use UTC-aware datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Base(DeclarativeBase):
    pass


class FilingRow(Base):
    __tablename__ = "filings"

    accession: Mapped[str] = mapped_column(String(25), primary_key=True)
    cik: Mapped[int] = mapped_column(Integer, index=True)
    ticker: Mapped[str | None] = mapped_column(String(10), index=True)
    company: Mapped[str] = mapped_column(String(200))
    form: Mapped[str] = mapped_column(String(20), index=True)
    items: Mapped[str] = mapped_column(String(200), default="")
    filed_date: Mapped[date] = mapped_column(Date)
    accepted_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    url: Mapped[str] = mapped_column(String(300))
    analyzed: Mapped[bool] = mapped_column(Boolean, default=False)


class InsiderTransactionRow(Base):
    __tablename__ = "insider_transactions"
    __table_args__ = (UniqueConstraint("accession", "seq", name="uq_insider_accession_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    accession: Mapped[str] = mapped_column(String(25), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    issuer_cik: Mapped[int] = mapped_column(Integer, index=True)
    ticker: Mapped[str | None] = mapped_column(String(10), index=True)
    owner_cik: Mapped[int] = mapped_column(Integer)
    owner_name: Mapped[str] = mapped_column(String(200))
    is_director: Mapped[bool] = mapped_column(Boolean)
    is_officer: Mapped[bool] = mapped_column(Boolean)
    is_ten_pct_owner: Mapped[bool] = mapped_column(Boolean)
    officer_title: Mapped[str] = mapped_column(String(200), default="")
    transaction_date: Mapped[date] = mapped_column(Date)
    code: Mapped[str] = mapped_column(String(4))
    acquired_disposed: Mapped[str] = mapped_column(String(1))
    shares: Mapped[Decimal] = mapped_column(DecimalText)
    price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    shares_owned_after: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    direct: Mapped[bool] = mapped_column(Boolean, default=True)
    is_10b5_1: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FilingTextRow(Base):
    """Plain text of the documents relevant for analysis (e.g. 8-K body and EX-99 exhibits)."""

    __tablename__ = "filing_texts"

    accession: Mapped[str] = mapped_column(String(25), primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(50))
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    side: Mapped[str] = mapped_column(String(4))
    horizon_days: Mapped[int] = mapped_column(Integer)
    conviction: Mapped[Decimal] = mapped_column(DecimalText)
    stop_loss_pct: Mapped[Decimal] = mapped_column(DecimalText)
    thesis: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    order_client_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class OrderRow(Base):
    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    signal_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    purpose: Mapped[str] = mapped_column(String(20))
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    order_type: Mapped[str] = mapped_column(String(10))
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_qty: Mapped[Decimal] = mapped_column(DecimalText)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    commission: Mapped[Decimal] = mapped_column(DecimalText)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FillRow(Base):
    __tablename__ = "fills"

    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(32), index=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    price: Mapped[Decimal] = mapped_column(DecimalText)
    commission: Mapped[Decimal] = mapped_column(DecimalText)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class PositionPlanRow(Base):
    """Exit plan attached to an open position (stop price and time-based exit)."""

    __tablename__ = "position_plans"

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stop_price: Mapped[Decimal] = mapped_column(DecimalText)
    exit_after: Mapped[datetime] = mapped_column(UTCDateTime)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    equity: Mapped[Decimal] = mapped_column(DecimalText)
    settled_cash: Mapped[Decimal] = mapped_column(DecimalText)
    unsettled_cash: Mapped[Decimal] = mapped_column(DecimalText)
    positions_value: Mapped[Decimal] = mapped_column(DecimalText)


class AuditRow(Base):
    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class KVRow(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FxRateRow(Base):
    __tablename__ = "fx_rates"

    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    rate: Mapped[Decimal] = mapped_column(DecimalText)  # units of currency per 1 EUR (ECB convention)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    Base.metadata.create_all(engine)
    return engine


class Database:
    def __init__(self, url: str) -> None:
        self.engine = make_engine(url)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # Small key/value store for durable process state (kill switch, offsets, simulator state).
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(KVRow, key)
            return default if row is None else json.loads(row.value)

    def kv_set(self, key: str, value: Any) -> None:
        now = datetime.now(UTC)
        with self.session() as s:
            row = s.get(KVRow, key)
            if row is None:
                s.add(KVRow(key=key, value=dumps(value), updated_at=now))
            else:
                row.value = dumps(value)
                row.updated_at = now

    def kv_delete(self, key: str) -> None:
        with self.session() as s:
            row = s.get(KVRow, key)
            if row is not None:
                s.delete(row)

    def latest_equity(self) -> EquitySnapshotRow | None:
        with self.session() as s:
            return s.scalars(select(EquitySnapshotRow).order_by(EquitySnapshotRow.ts.desc()).limit(1)).first()
