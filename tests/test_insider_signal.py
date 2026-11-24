from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from trader.db import Database, InsiderTransactionRow
from trader.signals.insider import InsiderClusterParams, find_insider_clusters

NOW = datetime(2026, 11, 24, 15, tzinfo=UTC)


def add_tx(db: Database, *, acc: str, ticker: str, owner: int, shares: str, price: str,
           accepted: datetime, code: str = "P", officer: bool = True, title: str = "CFO",
           plan: bool = False, director: bool = False) -> None:
    with db.session() as s:
        s.add(InsiderTransactionRow(
            accession=acc, seq=0, issuer_cik=1, ticker=ticker, owner_cik=owner, owner_name=f"Owner {owner}",
            is_director=director, is_officer=officer, is_ten_pct_owner=False, officer_title=title,
            transaction_date=accepted.date() - timedelta(days=1), code=code, acquired_disposed="A",
            shares=D(shares), price=D(price), shares_owned_after=None, direct=True, is_10b5_1=plan,
            accepted_at=accepted, ingested_at=accepted,
        ))


def db_at(tmp_path: Path) -> Database:
    return Database(f"sqlite:///{tmp_path / 'i.sqlite'}")


def test_cluster_detected_with_evidence(tmp_path: Path) -> None:
    db = db_at(tmp_path)
    add_tx(db, acc="a1", ticker="ACME", owner=1, shares="2000", price="30", accepted=NOW - timedelta(days=3),
           title="Chief Executive Officer")
    add_tx(db, acc="a2", ticker="ACME", owner=2, shares="1500", price="31", accepted=NOW - timedelta(days=2))
    (sig,) = find_insider_clusters(db, NOW)
    assert sig.ticker == "ACME" and sig.source == "insider_cluster"
    assert D("0.55") <= sig.conviction <= D("0.95")
    assert len(sig.evidence) == 2 and sig.evidence[0]["accession"] == "a1"
    assert "2 insider" in sig.thesis
    assert sig.dedup_key.startswith("insider_cluster:ACME:2026-W")


def test_single_insider_or_small_value_ignored(tmp_path: Path) -> None:
    db = db_at(tmp_path)
    add_tx(db, acc="b1", ticker="ONE", owner=1, shares="10000", price="50", accepted=NOW - timedelta(days=1))
    add_tx(db, acc="c1", ticker="TINY", owner=1, shares="100", price="20", accepted=NOW - timedelta(days=1))
    add_tx(db, acc="c2", ticker="TINY", owner=2, shares="100", price="20", accepted=NOW - timedelta(days=1))
    assert find_insider_clusters(db, NOW) == []


def test_filters_plans_sales_penny_and_non_insiders(tmp_path: Path) -> None:
    db = db_at(tmp_path)
    for i, kw in enumerate([
        {"plan": True}, {"code": "S"}, {"price": "2"}, {"officer": False, "title": ""},
    ]):
        base = {"acc": f"x{i}", "ticker": "FILT", "owner": 10 + i, "shares": "10000", "price": "40",
                "accepted": NOW - timedelta(days=1)}
        base.update(kw)
        add_tx(db, **base)  # type: ignore[arg-type]
    add_tx(db, acc="ok", ticker="FILT", owner=99, shares="10000", price="40", accepted=NOW - timedelta(days=1))
    assert find_insider_clusters(db, NOW) == []
    loose = InsiderClusterParams(exclude_10b5_1=False)
    assert [s.ticker for s in find_insider_clusters(db, NOW, loose)] == ["FILT"]


def test_point_in_time_and_lookback(tmp_path: Path) -> None:
    db = db_at(tmp_path)
    add_tx(db, acc="f1", ticker="FUT", owner=1, shares="5000", price="30", accepted=NOW - timedelta(days=1))
    add_tx(db, acc="f2", ticker="FUT", owner=2, shares="5000", price="30", accepted=NOW + timedelta(minutes=1))
    add_tx(db, acc="o1", ticker="OLD", owner=1, shares="5000", price="30", accepted=NOW - timedelta(days=40))
    add_tx(db, acc="o2", ticker="OLD", owner=2, shares="5000", price="30", accepted=NOW - timedelta(days=1))
    assert find_insider_clusters(db, NOW) == []
    later = NOW + timedelta(minutes=2)
    assert [s.ticker for s in find_insider_clusters(db, later)] == ["FUT"]
    assert date(2026, 1, 1) < later.date()
