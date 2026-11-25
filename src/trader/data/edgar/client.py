"""Polite HTTP client for SEC EDGAR.

SEC fair-access rules: declare a User-Agent identifying you (company/name and contact) and stay
below 10 requests/second. Set ``TRADER_SEC_USER_AGENT``; this client never invents one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date

import httpx

from trader.data.edgar.index import IndexEntry, parse_latest_atom, parse_master_index

WWW = "https://www.sec.gov"
REQUEST_TIMEOUT = 30.0
DATA = "https://data.sec.gov"


_BLOCKED_MARKERS = ("Request Rate Threshold Exceeded", "Undeclared Automated Tool")


class EdgarError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        """Worth retrying later: network problems, rate limits, server errors."""
        return self.status is None or self.status == 429 or self.status >= 500


class EdgarDeferred(EdgarError):
    """The caller's deadline passed: the request was not (re)tried."""


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        *,
        http: httpx.Client | None = None,
        max_rps: float = 5.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise EdgarError("SEC requires a declared User-Agent: set TRADER_SEC_USER_AGENT")
        if not 0 < max_rps <= 10:
            raise EdgarError("max_rps must be in (0, 10] per SEC fair-access policy")
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=True)
        self.headers = {"User-Agent": user_agent.strip(), "Accept-Encoding": "gzip, deflate"}
        self.min_interval = 1.0 / max_rps
        self.retries = retries
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()

    def get(
        self, url: str, *, allow_404: bool = False, deadline: float | None = None
    ) -> httpx.Response | None:
        """``deadline`` (time.monotonic) bounds the whole call: no attempt starts after it and each
        request's timeout is capped to the time left."""
        delay = 1.0
        for attempt in range(self.retries + 1):
            if deadline is not None and deadline - time.monotonic() <= 0:
                raise EdgarDeferred(f"deadline reached before GET {url}")
            self._throttle()
            left = None if deadline is None else deadline - time.monotonic()  # after throttling
            if left is not None and left <= 0:
                raise EdgarDeferred(f"deadline reached before GET {url}")
            total = REQUEST_TIMEOUT if left is None else max(1.0, min(REQUEST_TIMEOUT, left))
            timeout = httpx.Timeout(total, connect=min(5.0, total))
            try:
                resp = self.http.get(url, headers=self.headers, timeout=timeout)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise EdgarError(f"GET {url} failed: {exc}") from exc
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise EdgarDeferred(f"no time left to retry GET {url}") from exc
                self.sleep(delay)
                delay *= 2
                continue
            if allow_404 and resp.status_code == 404:
                return None
            if resp.status_code == 403:
                body = resp.text[:5000]
                if any(m in body for m in _BLOCKED_MARKERS):
                    raise EdgarError(f"SEC blocked the request (rate limit or User-Agent policy): {url}", 429)
                if allow_404:  # EDGAR archives answer 403 for files that do not exist (e.g. holidays)
                    return None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise EdgarDeferred(f"no time left to retry GET {url}")
                self.sleep(delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                raise EdgarError(f"GET {url} -> HTTP {resp.status_code}", resp.status_code)
            return resp
        raise EdgarError(f"GET {url} exhausted retries")

    # ------------------------------------------------------------- endpoints
    def ticker_exchange_map(self, deadline: float | None = None) -> dict[int, tuple[str, str]]:
        """CIK -> (ticker, exchange) for SEC-registered listed companies."""
        resp = self.get(f"{WWW}/files/company_tickers_exchange.json", deadline=deadline)
        assert resp is not None
        payload = resp.json()
        fields = payload["fields"]
        i_cik, i_ticker, i_exch = fields.index("cik"), fields.index("ticker"), fields.index("exchange")
        out: dict[int, tuple[str, str]] = {}
        for row in payload["data"]:
            cik, ticker, exch = int(row[i_cik]), str(row[i_ticker] or ""), str(row[i_exch] or "")
            if ticker and cik not in out:  # first listed class wins (SEC orders by relevance)
                out[cik] = (ticker.upper(), exch)
        return out

    def master_index(self, day: date, deadline: float | None = None) -> list[IndexEntry]:
        quarter = (day.month - 1) // 3 + 1
        url = f"{WWW}/Archives/edgar/daily-index/{day.year}/QTR{quarter}/master.{day:%Y%m%d}.idx"
        resp = self.get(url, allow_404=True, deadline=deadline)  # missing on weekends/holidays
        return parse_master_index(resp.text) if resp is not None else []

    def latest(self, form: str, count: int = 100, deadline: float | None = None) -> list[IndexEntry]:
        url = (
            f"{WWW}/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb="
            f"&owner=include&start=0&count={count}&output=atom"
        )
        resp = self.get(url, deadline=deadline)
        assert resp is not None
        return parse_latest_atom(resp.text, form)

    def submission_text(self, path: str, deadline: float | None = None) -> str:
        resp = self.get(f"{WWW}/Archives/{path.lstrip('/')}", deadline=deadline)
        assert resp is not None
        return resp.text
