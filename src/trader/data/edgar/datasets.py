"""Bulk history of insider transactions from the SEC "Insider Transactions Data Sets".

Quarterly ZIP files (``<YYYY>q<N>_form345.zip``) with the flattened XML of Forms 3/4/5.
Only original Form 4 filings are loaded (amendments 4/A are skipped to avoid double counting).

Point-in-time: the data sets carry the *filing date*, not the acceptance time. ``accepted_at`` is
set to 23:59:59 New York time of the filing date, which is never earlier than the real acceptance
(EDGAR assigns the next business day's filing date to submissions accepted after 17:30 ET).
Backtests therefore never see a filing before it was public; at worst they act one day late.
Filings already stored from the live EDGAR feed (exact acceptance time) are left untouched.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import insert, select

from trader.broker.base import NEW_YORK
from trader.data.edgar.client import WWW, EdgarClient
from trader.db import Database, FilingRow, InsiderTransactionRow

log = logging.getLogger(__name__)

DATASET_URL = WWW + "/files/structureddata/data/insider-transactions-data-sets/{quarter}_form345.zip"
_QUARTER = re.compile(r"^\d{4}q[1-4]$")
_TICKER = re.compile(r"^[A-Z]{1,6}(-[A-Z])?$")
_TRUE = {"1", "true"}

csv.field_size_limit(1 << 30)


def normalize_symbol(raw: str) -> str | None:
    """As-filed trading symbol -> SEC-style ticker, or None when unusable ('NONE', 'N/A', ...)."""
    s = raw.strip().upper()
    if ":" in s:  # e.g. "NASDAQ: ABCD"
        s = s.rsplit(":", 1)[1].strip()
    if s.replace(".", "").replace("/", "") in {"NONE", "NA", "", "-", "--"}:
        return None
    s = s.replace(".", "-").replace("/", "-").replace(" ", "")
    return s if _TICKER.match(s) else None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()  # noqa: DTZ007 - calendar date only
        except ValueError:
            continue
    return None


def _dec(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def end_of_filing_day(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=NEW_YORK).astimezone(UTC)


def download_quarter(client: EdgarClient, quarter: str, cache_dir: Path) -> Path:
    if not _QUARTER.match(quarter):
        raise ValueError(f"quarter must look like 2025q1, got {quarter!r}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{quarter}_form345.zip"
    if not target.exists():
        resp = client.get(DATASET_URL.format(quarter=quarter), allow_404=True)
        if resp is None:
            raise FileNotFoundError(f"SEC data set {quarter} not published yet")
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(target)
    return target


def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


@dataclass
class DatasetReport:
    quarter_file: str
    form4_filings: int = 0
    skipped_existing: int = 0
    skipped_no_ticker: int = 0
    filings_stored: int = 0
    transactions_stored: int = 0
    notes: list[str] = field(default_factory=list)


def load_quarter(db: Database, zip_path: Path, *, now: datetime | None = None) -> DatasetReport:
    now = now or datetime.now(UTC)
    report = DatasetReport(quarter_file=zip_path.name)
    with zipfile.ZipFile(zip_path) as zf:
        subs = {
            r["ACCESSION_NUMBER"]: r
            for r in _rows(zf, "SUBMISSION.tsv")
            if r.get("DOCUMENT_TYPE", "").strip() == "4"
        }
        report.form4_filings = len(subs)
        owners: dict[str, list[dict[str, str]]] = {}
        for r in _rows(zf, "REPORTINGOWNER.tsv"):
            if r["ACCESSION_NUMBER"] in subs:
                owners.setdefault(r["ACCESSION_NUMBER"], []).append(r)
        txs: dict[str, list[dict[str, str]]] = {}
        for r in _rows(zf, "NONDERIV_TRANS.tsv"):
            if r["ACCESSION_NUMBER"] in subs:
                txs.setdefault(r["ACCESSION_NUMBER"], []).append(r)

    existing: set[str] = set()
    accessions = list(subs)
    with db.session() as s:
        for i in range(0, len(accessions), 900):  # stay below SQLite's bound-parameter limit
            chunk = accessions[i : i + 900]
            existing.update(s.scalars(select(FilingRow.accession).where(FilingRow.accession.in_(chunk))))
    report.skipped_existing = len(existing)

    filing_rows: list[dict[str, object]] = []
    tx_rows: list[dict[str, object]] = []
    for acc, sub in subs.items():
        if acc in existing:
            continue
        filed = _parse_date(sub.get("FILING_DATE", ""))
        ticker = normalize_symbol(sub.get("ISSUERTRADINGSYMBOL", ""))
        cik_raw = sub.get("ISSUERCIK", "").strip()
        if filed is None or not cik_raw.isdigit():
            continue
        if ticker is None:
            report.skipped_no_ticker += 1
            continue
        accepted = end_of_filing_day(filed)
        cik = int(cik_raw)
        filing_rows.append(
            {
                "accession": acc,
                "cik": cik,
                "ticker": ticker,
                "company": sub.get("ISSUERNAME", "")[:200],
                "form": "4",
                "items": "",
                "filed_date": filed,
                "accepted_at": accepted,
                "ingested_at": now,
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}.txt",
                "analyzed": False,
            }
        )
        ows = owners.get(acc, [])
        # Joint filings: attribute the transaction once, preferring an officer/director.
        primary = next(
            (o for o in ows if {"Officer", "Director"} & set(o.get("RPTOWNER_RELATIONSHIP", "").split(","))),
            ows[0] if ows else None,
        )
        if primary is None:
            continue
        rel = set(primary.get("RPTOWNER_RELATIONSHIP", "").split(","))
        owner_cik = primary.get("RPTOWNERCIK", "").strip()
        plan = sub.get("AFF10B5ONE", "").strip().lower() in _TRUE
        for seq, t in enumerate(sorted(txs.get(acc, []), key=lambda x: int(x.get("NONDERIV_TRANS_SK") or 0))):
            shares = _dec(t.get("TRANS_SHARES", ""))
            tdate = _parse_date(t.get("TRANS_DATE", ""))
            if shares is None or tdate is None:
                continue
            tx_rows.append(
                {
                    "accession": acc,
                    "seq": seq,
                    "issuer_cik": cik,
                    "ticker": ticker,
                    "owner_cik": int(owner_cik) if owner_cik.isdigit() else 0,
                    "owner_name": primary.get("RPTOWNERNAME", "")[:200],
                    "is_director": "Director" in rel,
                    "is_officer": "Officer" in rel,
                    "is_ten_pct_owner": "TenPercentOwner" in rel,
                    "officer_title": primary.get("RPTOWNER_TITLE", "")[:200],
                    "transaction_date": tdate,
                    "code": t.get("TRANS_CODE", "").strip()[:4],
                    "acquired_disposed": t.get("TRANS_ACQUIRED_DISP_CD", "").strip()[:1],
                    "shares": shares,
                    "price": _dec(t.get("TRANS_PRICEPERSHARE", "")),
                    "shares_owned_after": _dec(t.get("SHRS_OWND_FOLWNG_TRANS", "")),
                    "direct": t.get("DIRECT_INDIRECT_OWNERSHIP", "").strip() != "I",
                    "is_10b5_1": plan,
                    "accepted_at": accepted,
                    "ingested_at": now,
                }
            )

    with db.session() as s:
        for i in range(0, len(filing_rows), 5000):
            s.execute(insert(FilingRow), filing_rows[i : i + 5000])
        for i in range(0, len(tx_rows), 5000):
            s.execute(insert(InsiderTransactionRow), tx_rows[i : i + 5000])
    report.filings_stored = len(filing_rows)
    report.transactions_stored = len(tx_rows)
    return report
