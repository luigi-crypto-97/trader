"""Daily US stock bars from Massive (formerly Polygon.io), with an on-disk cache.

The *grouped daily* endpoint returns one bar for every US-listed stock on a given date, including
companies that were later delisted, so a backtest built on it has no survivorship bias.
Free Basic plan: 5 requests/minute, ~2 years of history, data available after midnight ET.

Bars are stored **unadjusted** (as traded that day) and stock splits are kept separately.
Split-adjusted history would leak the future (a later reverse split makes a past 2 USD stock look
like 20 USD) and would change every time a new split happens; with raw bars plus the split list,
backtests apply each split on its execution date, exactly as a brokerage account does.

Authentication: the API key is sent as ``Authorization: Bearer <key>``. Set
``TRADER_MASSIVE_API_KEY``; in a cloud session whose proxy injects the key, leave it unset.

Tickers are stored in SEC style (class shares ``BRK-B``); Massive uses ``BRK.B``. Preferreds,
warrants and units are dropped.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from trader.marketdata import Bar, PriceData, Split, SplitMap

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

    def _get(self, url: str, params: dict[str, str] | None) -> dict[str, object]:
        """GET with throttling and retries. ``params=None`` keeps the URL's own query string
        (httpx replaces the query when given a params mapping, even an empty one)."""
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

    def grouped_daily(self, day: date, *, adjusted: bool = False) -> dict[str, Bar]:
        data = self._get(
            f"{BASE_URL}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            {"adjusted": "true" if adjusted else "false"},
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

    def splits(self, start: date, end: date) -> list[Split]:
        """All stock splits with execution date in [start, end] (paginated)."""
        out: list[Split] = []
        url: str | None = f"{BASE_URL}/v3/reference/splits"
        params: dict[str, str] | None = {
            "execution_date.gte": start.isoformat(),
            "execution_date.lte": end.isoformat(),
            "limit": "1000",
            "order": "asc",
            "sort": "execution_date",
        }
        while url:
            data = self._get(url, params)
            results = data.get("results") or []
            if not isinstance(results, list):
                raise PriceDataError("unexpected splits payload")
            for r in results:
                ticker = massive_to_sec(str(r.get("ticker", "")))
                frm, to = Decimal(str(r.get("split_from") or 0)), Decimal(str(r.get("split_to") or 0))
                if ticker is None or frm <= 0 or to <= 0:
                    continue
                out.append(Split(ticker, date.fromisoformat(str(r["execution_date"])), frm, to))
            nxt = data.get("next_url")
            if nxt and not str(nxt).startswith(BASE_URL + "/"):
                raise PriceDataError("unexpected pagination host: refusing to send the API key elsewhere")
            url, params = (str(nxt), None) if nxt else (None, None)  # next_url carries the cursor
        return out


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

    @property
    def splits_path(self) -> Path:
        return self.dir / "splits.json"

    def _load_split_file(self) -> tuple[list[Split], list[tuple[date, date]]]:
        if not self.splits_path.exists():
            return [], []
        raw = json.loads(self.splits_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):  # legacy file without coverage information
            raise PriceDataError("split file has no coverage information: download the splits again")
        splits = [
            Split(
                r["ticker"],
                date.fromisoformat(r["execution_date"]),
                Decimal(r["split_from"]),
                Decimal(r["split_to"]),
            )
            for r in raw["splits"]
        ]
        coverage = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in raw["coverage"]]
        return splits, coverage

    def write_splits(self, splits: list[Split], start: date, end: date) -> None:
        """Merge ``splits`` (the complete list for [start, end]) into the stored list.

        Stored splits inside [start, end] are replaced by the new list; splits outside it are kept.
        The covered date ranges are recorded so that :meth:`read_splits` can refuse an incomplete list."""
        try:
            existing, coverage = self._load_split_file()
        except PriceDataError:
            existing, coverage = [], []
        # The new list is complete for [start, end]: splits stored for that range but no longer
        # reported by the source are dropped; splits outside it are kept.
        merged = {(x.ticker, x.execution_date): x for x in existing if not start <= x.execution_date <= end}
        merged.update({(x.ticker, x.execution_date): x for x in splits})
        joined: list[tuple[date, date]] = []
        for a, b in sorted([*coverage, (start, end)]):  # union of covered date ranges
            if joined and a <= joined[-1][1] + timedelta(days=1):
                joined[-1] = (joined[-1][0], max(joined[-1][1], b))
            else:
                joined.append((a, b))
        payload = {
            "coverage": [[a.isoformat(), b.isoformat()] for a, b in joined],
            "splits": [
                {
                    "ticker": x.ticker,
                    "execution_date": x.execution_date.isoformat(),
                    "split_from": str(x.split_from),
                    "split_to": str(x.split_to),
                }
                for x in sorted(merged.values(), key=lambda x: (x.execution_date, x.ticker))
            ],
        }
        tmp = self.splits_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.splits_path)

    def read_splits(self, start: date, end: date) -> SplitMap:
        """Splits by ticker; fails unless the stored list is known to be complete for [start, end]."""
        splits, coverage = self._load_split_file()
        if not any(a <= start and end <= b for a, b in coverage):
            raise PriceDataError(f"split list does not cover {start}..{end}: download the splits again")
        out: SplitMap = {}
        for sp in splits:
            out.setdefault(sp.ticker, []).append(sp)
        for v in out.values():
            v.sort(key=lambda x: x.execution_date)
        return out

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
