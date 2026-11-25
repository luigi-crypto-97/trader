"""Daily US stock bars from Massive (formerly Polygon.io), with an on-disk cache.

The *grouped daily* endpoint returns one bar for every US-listed stock on a given date, including
companies that were later delisted, so a backtest built on it has no survivorship bias.
Free Basic plan: 5 requests/minute, ~2 years of history, data available after midnight ET.

Authentication: the API key is sent as ``Authorization: Bearer <key>``. Set
``TRADER_MASSIVE_API_KEY``; in a cloud session whose proxy injects the key, leave it unset.

Tickers are stored in SEC style (class shares ``BRK-B``); Massive uses ``BRK.B``. Preferreds,
warrants and units are dropped.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from trader.backtest.engine import Bar, PriceData

log = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"
_COMMON = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")


class PriceDataError(RuntimeError):
    pass


def massive_to_sec(ticker: str) -> str | None:
    """'BRK.B' -> 'BRK-B'; None for preferreds/warrants/units and other non-common symbols."""
    if not _COMMON.match(ticker):
        return None
    return ticker.replace(".", "-")


class MassiveClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        http: httpx.Client | None = None,
        min_interval: float = 12.5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("TRADER_MASSIVE_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = http or httpx.Client(timeout=60.0, headers=headers)
        if http is not None and headers:
            self.http.headers.update(headers)
        self.min_interval = min_interval
        self.sleep = sleep
        self.monotonic = monotonic
        self._last = -1e9

    def _get(self, url: str, params: dict[str, str]) -> dict[str, object]:
        for attempt in range(6):
            wait = self._last + self.min_interval - self.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = self.monotonic()
            try:
                resp = self.http.get(url, params=params)
            except httpx.TransportError as exc:
                log.warning("massive transport error (%s), retrying", type(exc).__name__)
                self.sleep(min(60.0, 5.0 * (attempt + 1)))
                continue
            if resp.status_code == 429:
                self.sleep(60.0)
                continue
            if resp.status_code >= 500:
                self.sleep(min(60.0, 5.0 * (attempt + 1)))
                continue
            if resp.status_code in (401, 403):
                raise PriceDataError(
                    f"Massive rejected the request (HTTP {resp.status_code}): check the API key/plan"
                )
            if resp.status_code != 200:
                raise PriceDataError(f"Massive HTTP {resp.status_code}: {resp.text[:200]}")
            data: dict[str, object] = resp.json()
            return data
        raise PriceDataError(f"Massive request failed repeatedly: {url}")

    def grouped_daily(self, day: date) -> dict[str, Bar]:
        data = self._get(
            f"{BASE_URL}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}", {"adjusted": "true"}
        )
        results = data.get("results") or []
        assert isinstance(results, list)
        bars: dict[str, Bar] = {}
        for r in results:
            ticker = massive_to_sec(str(r.get("T", "")))
            if ticker is None or not all(k in r for k in ("o", "h", "l", "c", "v")):
                continue
            bars[ticker] = Bar(
                day,
                Decimal(str(r["o"])),
                Decimal(str(r["h"])),
                Decimal(str(r["l"])),
                Decimal(str(r["c"])),
                Decimal(str(r["v"])),
            )
        return bars


class PriceCache:
    """One gzip text file per trading day: ``TICKER,open,high,low,close,volume`` lines.
    An empty file marks a non-trading day (so it is never re-downloaded)."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.csv.gz"

    def has(self, day: date) -> bool:
        return self.path(day).exists()

    def write(self, day: date, bars: dict[str, Bar]) -> None:
        tmp = self.path(day).with_suffix(".tmp")
        lines = [f"{t},{b.open},{b.high},{b.low},{b.close},{b.volume}" for t, b in sorted(bars.items())]
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        tmp.replace(self.path(day))

    def read(self, day: date, tickers: set[str] | None = None) -> dict[str, Bar]:
        bars: dict[str, Bar] = {}
        with gzip.open(self.path(day), "rt", encoding="utf-8") as fh:
            for line in fh:
                t, o, h, low, c, v = line.rstrip("\n").split(",")
                if tickers is None or t in tickers:
                    bars[t] = Bar(day, Decimal(o), Decimal(h), Decimal(low), Decimal(c), Decimal(v))
        return bars

    def days(self) -> list[date]:
        return sorted(date.fromisoformat(p.name.split(".")[0]) for p in self.dir.glob("*.csv.gz"))

    def load(
        self, tickers: Iterable[str] | None = None, start: date | None = None, end: date | None = None
    ) -> PriceData:
        wanted = {t.upper() for t in tickers} if tickers is not None else None
        data: PriceData = {}
        for d in self.days():
            if (start and d < start) or (end and d > end):
                continue
            for t, bar in self.read(d, wanted).items():
                data.setdefault(t, {})[d] = bar
        return data


def backfill(
    client: MassiveClient,
    cache: PriceCache,
    start: date,
    end: date,
    *,
    on_progress: Callable[[date, int], None] | None = None,
) -> int:
    """Download every missing weekday in [start, end]. Resumable. Returns days fetched."""
    fetched = 0
    d = start
    while d <= end:
        if d.weekday() < 5 and not cache.has(d):
            bars = client.grouped_daily(d)
            cache.write(d, bars)
            fetched += 1
            if on_progress:
                on_progress(d, len(bars))
        d += timedelta(days=1)
    return fetched
