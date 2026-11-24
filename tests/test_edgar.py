from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from trader.data.edgar.client import EdgarClient, EdgarError
from trader.data.edgar.form4 import parse_ownership_xml
from trader.data.edgar.index import parse_latest_atom, parse_master_index
from trader.data.edgar.ingest import EdgarIngestor
from trader.data.edgar.sgml import html_to_text, parse_submission
from trader.db import Database, FilingRow, FilingTextRow, InsiderTransactionRow

FIX = Path(__file__).parent / "fixtures" / "edgar"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_form4_submission_point_in_time() -> None:
    sub = parse_submission(read("form4_bkyi_purchase.txt"))
    assert sub.accession == "0001437749-26-037980"
    assert sub.form == "4"
    # 2026-11-23 08:09:40 US Eastern (EST, UTC-5) -> 13:09:40 UTC
    assert sub.accepted_at == datetime(2026, 11, 23, 13, 9, 40, tzinfo=UTC)
    assert sub.filed_date == date(2026, 11, 23)
    assert set(sub.ciks) == {1246138, 1019034}
    doc = sub.first("4")
    assert doc is not None and doc.xml is not None


def test_parse_ownership_open_market_purchase() -> None:
    sub = parse_submission(read("form4_bkyi_purchase.txt"))
    own = parse_ownership_xml(sub.first("4").xml)  # type: ignore[union-attr,arg-type]
    assert own.ticker == "BKYI" and own.issuer_cik == 1019034
    owner = own.primary_owner
    assert owner.name == "DEPASQUALE MICHAEL W"
    assert owner.is_director and owner.is_officer and owner.officer_title == "Chief Executive Officer"
    assert not own.is_10b5_1
    (tx,) = own.transactions
    assert tx.code == "P" and tx.acquired_disposed == "A"
    assert tx.shares == D("1000") and tx.price == D("1.98") and tx.value == D("1980.00")
    assert tx.shares_owned_after == D("13764") and tx.direct
    assert tx.is_open_market_purchase


def test_parse_ownership_10b5_1_indirect() -> None:
    sub = parse_submission(read("form4_eltk_10b5_1.txt"))
    own = parse_ownership_xml(sub.first("4").xml)  # type: ignore[union-attr,arg-type]
    assert own.ticker == "ELTK" and own.is_10b5_1
    assert [t.price for t in own.transactions] == [D("4.975"), D("5.3634")]
    assert all(not t.direct for t in own.transactions)


def test_joint_filing_attributed_to_insider() -> None:
    xml = parse_submission(read("form4_bkyi_purchase.txt")).first("4").xml  # type: ignore[union-attr]
    assert xml is not None
    fund = (
        "<reportingOwner><reportingOwnerId><rptOwnerCik>0009999999</rptOwnerCik>"
        "<rptOwnerName>SOME FUND LP</rptOwnerName></reportingOwnerId><reportingOwnerRelationship>"
        "<isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship></reportingOwner>"
    )
    joint = xml.replace("<reportingOwner>", fund + "<reportingOwner>", 1)
    own = parse_ownership_xml(joint)
    assert len(own.owners) == 2
    assert own.primary_owner.name == "DEPASQUALE MICHAEL W"


def test_parse_8k_items_and_text() -> None:
    sub = parse_submission(read("8k_mgld.txt"))
    assert sub.form == "8-K"
    assert sub.items == (
        "Completion of Acquisition or Disposition of Assets",
        "Other Events",
        "Financial Statements and Exhibits",
    )
    assert [d.type for d in sub.documents] == ["8-K", "EX-99.1"]
    text = html_to_text(sub.first("EX-99.1").text)  # type: ignore[union-attr]
    assert len(text) > 500 and "<" not in text[:2000]


def test_master_index_and_atom() -> None:
    entries = parse_master_index(read("master_sample.idx"))
    assert len(entries) == 67
    forms = {e.form for e in entries}
    assert {"4", "8-K", "424B2"} <= forms
    bkyi = [e for e in entries if e.accession == "0001437749-26-037980"]
    assert {e.cik for e in bkyi} == {1019034, 1246138}
    atom = parse_latest_atom(read("atom_form4.xml"), "4")
    assert len(atom) == 6 and all(e.form == "4" for e in atom)
    assert all(e.path.endswith(".txt") and e.path.startswith("edgar/data/") for e in atom)


# -------------------------------------------------------------------- client


def mock_client(handler: httpx.MockTransport, **kw: object) -> EdgarClient:
    return EdgarClient("test-suite research", http=httpx.Client(transport=handler), max_rps=10,
                       sleep=lambda _s: None, **kw)  # type: ignore[arg-type]


def test_client_requires_user_agent_and_sane_rate() -> None:
    with pytest.raises(EdgarError):
        EdgarClient("  ")
    with pytest.raises(EdgarError):
        EdgarClient("x", max_rps=50)


def test_client_retries_and_404() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["User-Agent"])
        if "master.20261128" in request.url.path:
            return httpx.Response(404)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text=read("master_sample.idx"))

    client = mock_client(httpx.MockTransport(handler))
    assert len(client.master_index(date(2026, 11, 23))) == 67
    assert client.master_index(date(2026, 11, 28)) == []
    assert calls[0] == "test-suite research"


# ------------------------------------------------------------------- ingestor


def fake_sec() -> httpx.MockTransport:
    tickers = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [1019034, "BIO KEY INTERNATIONAL INC", "BKYI", "Nasdaq"],
            [1024672, "ELTEK LTD", "ELTK", "Nasdaq"],
            [1005101, "Marygold Companies, Inc.", "MGLD", "NYSE"],
            [999, "OTC CO", "OTCX", "OTC"],
        ],
    }
    files = {
        "0001437749-26-037980.txt": "form4_bkyi_purchase.txt",
        "0001178913-26-005312.txt": "form4_eltk_10b5_1.txt",
        "0001493152-26-053689.txt": "8k_mgld.txt",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("company_tickers_exchange.json"):
            return httpx.Response(200, text=json.dumps(tickers))
        if "daily-index" in path:
            return httpx.Response(200, text=read("master_sample.idx"))
        name = path.rsplit("/", 1)[-1]
        if name in files:
            return httpx.Response(200, text=read(files[name]))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_ingest_day_end_to_end(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'e.sqlite'}")
    now = datetime(2026, 11, 24, 12, tzinfo=UTC)
    ing = EdgarIngestor(mock_client(fake_sec()), db, clock=lambda: now)
    report = ing.ingest_day(date(2026, 11, 23))
    assert report.candidates == 3 and report.stored == 3 and not report.errors
    assert report.insider_rows == 3

    with db.session() as s:
        filings = {f.accession: f for f in s.scalars(select(FilingRow))}
        assert filings["0001437749-26-037980"].ticker == "BKYI"
        assert filings["0001493152-26-053689"].ticker == "MGLD"
        assert "Other Events" in filings["0001493152-26-053689"].items
        txs = list(s.scalars(select(InsiderTransactionRow).order_by(InsiderTransactionRow.accession)))
        assert {t.ticker for t in txs} == {"BKYI", "ELTK"}
        assert all(t.accepted_at.tzinfo is UTC for t in txs)
        text = s.get(FilingTextRow, "0001493152-26-053689")
        assert text is not None and "=== EX-99.1" in text.text

    again = ing.ingest_day(date(2026, 11, 23))
    assert again.already_stored == 3 and again.fetched == 0


@pytest.mark.live
def test_live_sec_master_index() -> None:
    import os

    client = EdgarClient(os.environ.get("TRADER_SEC_USER_AGENT", "trader-test-suite"))
    entries = client.master_index(date(2026, 11, 23))
    assert any(e.form == "4" for e in entries)
