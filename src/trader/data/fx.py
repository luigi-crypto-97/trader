"""ECB euro reference exchange rates, cached in the database.

ECB convention: the rate is units of foreign currency per 1 EUR (e.g. USD 1.0939 = 1 EUR).
For Italian tax purposes amounts in foreign currency are converted at the rate of the day of the
operation or, if none was published, of the closest previous day (art. 9, c. 2, TUIR).
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select

from trader.db import Database, FxRateRow

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{currency}.EUR.SP00.A"
MAX_LOOKBACK_DAYS = 10


class FxError(RuntimeError):
    pass


class EcbClient:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=30.0)

    def fetch(self, currency: str, start: date, end: date) -> dict[date, Decimal]:
        resp = self.http.get(
            ECB_URL.format(currency=currency.upper()),
            params={"startPeriod": start.isoformat(), "endPeriod": end.isoformat(), "format": "csvdata"},
        )
        if resp.status_code == 404:  # no observations in range
            return {}
        if resp.status_code != 200:
            raise FxError(f"ECB API HTTP {resp.status_code}")
        rates: dict[date, Decimal] = {}
        for row in csv.DictReader(io.StringIO(resp.text)):
            value = (row.get("OBS_VALUE") or "").strip()
            if value:
                rates[date.fromisoformat(row["TIME_PERIOD"])] = Decimal(value)
        return rates


class FxRates:
    def __init__(self, db: Database, client: EcbClient | None = None) -> None:
        self.db = db
        self.client = client or EcbClient()

    def sync(self, currency: str, start: date, end: date) -> int:
        rates = self.client.fetch(currency, start, end)
        with self.db.session() as s:
            for day, rate in rates.items():
                row = s.get(FxRateRow, (currency.upper(), day))
                if row is None:
                    s.add(FxRateRow(currency=currency.upper(), day=day, rate=rate))
                else:
                    row.rate = rate
        return len(rates)

    def _cached(self, currency: str, day: date) -> Decimal | None:
        with self.db.session() as s:
            row = s.scalars(
                select(FxRateRow)
                .where(
                    FxRateRow.currency == currency.upper(),
                    FxRateRow.day <= day,
                    FxRateRow.day >= day - timedelta(days=MAX_LOOKBACK_DAYS),
                )
                .order_by(FxRateRow.day.desc())
                .limit(1)
            ).first()
            return row.rate if row else None

    def rate(self, currency: str, day: date) -> Decimal:
        """Units of ``currency`` per 1 EUR valid for ``day`` (same day or closest previous)."""
        if currency.upper() == "EUR":
            return Decimal("1")
        cached = self._cached(currency, day)
        if cached is not None:
            with self.db.session() as s:
                exact = s.get(FxRateRow, (currency.upper(), day))
            if exact is not None or day.weekday() >= 5:
                return cached
        self.sync(currency, day - timedelta(days=MAX_LOOKBACK_DAYS), day)
        found = self._cached(currency, day)
        if found is None:
            raise FxError(f"no ECB {currency} rate within {MAX_LOOKBACK_DAYS} days before {day}")
        return found

    def to_eur(self, amount: Decimal, currency: str, day: date) -> Decimal:
        return amount / self.rate(currency, day)
