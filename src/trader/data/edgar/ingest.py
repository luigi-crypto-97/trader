"""Ingest EDGAR filings (Form 4 insider trades, 8-K current reports) into the database.

Point-in-time discipline: every stored record carries the SEC *acceptance* timestamp, which is
the earliest moment the information was public. Signals and backtests must only use records
whose ``accepted_at`` precedes the decision time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select

from trader.data.edgar.client import EdgarClient
from trader.data.edgar.datasets import normalize_symbol
from trader.data.edgar.form4 import OwnershipParseError, parse_ownership_xml
from trader.data.edgar.index import IndexEntry
from trader.data.edgar.sgml import Submission, SubmissionParseError, html_to_text, parse_submission
from trader.db import Database, FilingRow, FilingTextRow, InsiderTransactionRow

log = logging.getLogger(__name__)

LISTED_EXCHANGES = frozenset({"NYSE", "Nasdaq"})
SUPPORTED_FORMS = frozenset({"4", "8-K"})
TICKER_CACHE_KEY = "sec_ticker_map"


@dataclass
class IngestReport:
    candidates: int = 0
    already_stored: int = 0
    fetched: int = 0
    stored: int = 0
    insider_rows: int = 0
    errors: list[str] = field(default_factory=list)


class EdgarIngestor:
    def __init__(
        self,
        client: EdgarClient,
        db: Database,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        exchanges: frozenset[str] = LISTED_EXCHANGES,
    ) -> None:
        self.client = client
        self.db = db
        self.clock = clock
        self.exchanges = exchanges

    # ---------------------------------------------------------------- tickers
    def ticker_map(self) -> dict[int, str]:
        """CIK -> ticker for companies listed on the allowed exchanges, refreshed daily."""
        today = self.clock().date().isoformat()
        cached = self.db.kv_get(TICKER_CACHE_KEY)
        if cached and cached.get("date") == today:
            return {int(k): v for k, v in cached["map"].items()}
        raw = self.client.ticker_exchange_map()
        mapping = {cik: t for cik, (t, exch) in raw.items() if exch in self.exchanges}
        self.db.kv_set(TICKER_CACHE_KEY, {"date": today, "map": {str(k): v for k, v in mapping.items()}})
        return mapping

    # ----------------------------------------------------------------- ingest
    def ingest_day(
        self, day: date, forms: Iterable[str] = SUPPORTED_FORMS, tickers: dict[int, str] | None = None
    ) -> IngestReport:
        return self.ingest_entries(self.client.master_index(day), forms, tickers=tickers)

    def ingest_latest(self, forms: Iterable[str] = SUPPORTED_FORMS) -> IngestReport:
        entries: list[IndexEntry] = []
        for form in forms:
            entries.extend(self.client.latest(form))
        return self.ingest_entries(entries, forms)

    def ingest_entries(
        self, entries: Iterable[IndexEntry], forms: Iterable[str], *, tickers: dict[int, str] | None = None
    ) -> IngestReport:
        """``tickers`` (CIK -> ticker) overrides today's listing map, e.g. for historical backfills
        that must include companies delisted since (no survivorship bias)."""
        wanted = set(forms) & SUPPORTED_FORMS
        tickers = tickers if tickers is not None else self.ticker_map()
        by_accession: dict[str, list[IndexEntry]] = {}
        for e in entries:
            if e.form in wanted:
                by_accession.setdefault(e.accession, []).append(e)
        # Keep only filings that involve a listed issuer (Form 4s are indexed under issuer and owner).
        relevant = {acc: es for acc, es in by_accession.items() if any(e.cik in tickers for e in es)}
        report = IngestReport(candidates=len(relevant))
        with self.db.session() as s:
            known = set(s.scalars(select(FilingRow.accession).where(FilingRow.accession.in_(list(relevant)))))
        report.already_stored = len(known)
        for acc, es in sorted(relevant.items()):
            if acc in known:
                continue
            entry = next((e for e in es if e.cik in tickers), es[0])
            try:
                text = self.client.submission_text(entry.path)
                report.fetched += 1
                sub = parse_submission(text)
                report.insider_rows += self.store(sub, entry, tickers)
                report.stored += 1
            except (SubmissionParseError, OwnershipParseError, ValueError, RuntimeError) as exc:
                msg = f"{acc}: {exc}"
                log.warning("ingest failed %s", msg)
                report.errors.append(msg)
        return report

    def store(self, sub: Submission, entry: IndexEntry, tickers: dict[int, str]) -> int:
        """Persist one parsed submission. Returns the number of insider transaction rows stored."""
        now = self.clock()
        url = f"https://www.sec.gov/Archives/{entry.path}"
        insider_rows = 0
        with self.db.session() as s:
            if s.get(FilingRow, sub.accession) is not None:
                return 0
            if sub.form == "4":
                doc = sub.first("4")
                xml = doc.xml if doc else None
                if not xml:
                    raise SubmissionParseError("Form 4 without XML document")
                own = parse_ownership_xml(xml)
                ticker = tickers.get(own.issuer_cik) or normalize_symbol(own.ticker)
                s.add(
                    FilingRow(
                        accession=sub.accession,
                        cik=own.issuer_cik,
                        ticker=ticker,
                        company=own.issuer_name,
                        form="4",
                        items="",
                        filed_date=sub.filed_date,
                        accepted_at=sub.accepted_at,
                        ingested_at=now,
                        url=url,
                    )
                )
                owner = own.primary_owner
                for tx in own.transactions:
                    s.add(
                        InsiderTransactionRow(
                            accession=sub.accession,
                            seq=tx.seq,
                            issuer_cik=own.issuer_cik,
                            ticker=ticker,
                            owner_cik=owner.cik,
                            owner_name=owner.name,
                            is_director=owner.is_director,
                            is_officer=owner.is_officer,
                            is_ten_pct_owner=owner.is_ten_pct_owner,
                            officer_title=owner.officer_title,
                            transaction_date=tx.transaction_date,
                            code=tx.code,
                            acquired_disposed=tx.acquired_disposed,
                            shares=tx.shares,
                            price=tx.price,
                            shares_owned_after=tx.shares_owned_after,
                            direct=tx.direct,
                            is_10b5_1=own.is_10b5_1,
                            accepted_at=sub.accepted_at,
                            ingested_at=now,
                        )
                    )
                    insider_rows += 1
            elif sub.form == "8-K":
                cik = next((c for c in sub.ciks if c in tickers), entry.cik)
                docs = [sub.first("8-K"), *sub.by_prefix("EX-99")]
                parts = [
                    f"=== {d.type} ({d.filename}) ===\n{html_to_text(d.text)}" for d in docs if d is not None
                ]
                s.add(
                    FilingRow(
                        accession=sub.accession,
                        cik=cik,
                        ticker=tickers.get(cik),
                        company=entry.company,
                        form="8-K",
                        items=" | ".join(sub.items),
                        filed_date=sub.filed_date,
                        accepted_at=sub.accepted_at,
                        ingested_at=now,
                        url=url,
                    )
                )
                s.add(FilingTextRow(accession=sub.accession, text="\n\n".join(parts)))
            else:
                raise SubmissionParseError(f"unsupported form {sub.form}")
        return insider_rows
