from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from trader.backtest.engine import Bar, PriceData, TimedSignal
from trader.broker.base import NEW_YORK
from trader.data.edgar.datasets import end_of_filing_day, load_quarter, normalize_symbol
from trader.data.prices import MassiveClient, PriceCache, PriceDataError, backfill, massive_to_sec
from trader.db import Database, FilingRow, InsiderTransactionRow
from trader.domain import Signal
from trader.research.insider_study import (
    EventBuilder,
    StudyConfig,
    Variant,
    default_variants,
    render_markdown,
    run_study,
    summarize,
)

# ------------------------------------------------------------ SEC data sets

SUB = "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\tAFF10B5ONE\n"
OWN = "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\n"
TRX = (
    "ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\t"
    "TRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP\n"
)


def make_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "SUBMISSION.tsv",
            SUB
            + "0000000001-25-000001\t03-MAR-2025\t4\t0000000111\tACME CORP\tacme\t0\n"
            + "0000000002-25-000002\t03-MAR-2025\t4/A\t0000000111\tACME CORP\tACME\t0\n"
            + "0000000003-25-000003\t04-MAR-2025\t4\t0000000222\tNO TICKER INC\tNONE\t0\n"
            + "0000000004-25-000004\t04-MAR-2025\t4\t0000000333\tBERK\tNYSE: BRK.B\ttrue\n",
        )
        zf.writestr(
            "REPORTINGOWNER.tsv",
            OWN
            + "0000000001-25-000001\t0000009999\tBIG FUND LP\tTenPercentOwner\t\n"
            + "0000000001-25-000001\t0000000500\tDOE JANE\tDirector,Officer\tChief Executive Officer\n"
            + "0000000004-25-000004\t0000000600\tROE RICK\tDirector\t\n",
        )
        zf.writestr(
            "NONDERIV_TRANS.tsv",
            TRX
            + "0000000001-25-000001\t11\t28-FEB-2025\tP\t1000.0\t25.5\tA\t5000.0\tD\n"
            + "0000000001-25-000001\t10\t27-FEB-2025\tP\t500.0\t25.0\tA\t4000.0\tD\n"
            + "0000000004-25-000004\t12\t03-MAR-2025\tP\t10.0\t\tA\t\tI\n",
        )
    return path


def test_normalize_symbol() -> None:
    assert normalize_symbol(" acme ") == "ACME"
    assert normalize_symbol("NYSE: BRK.B") == "BRK-B"
    assert normalize_symbol("NONE") is None and normalize_symbol("N/A") is None
    assert normalize_symbol("") is None


def test_end_of_filing_day_is_conservative() -> None:
    eod = end_of_filing_day(date(2025, 3, 3))
    assert eod == datetime(2025, 3, 3, 23, 59, 59, tzinfo=NEW_YORK).astimezone(UTC)


def test_load_quarter(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'ds.sqlite'}")
    z = make_zip(tmp_path / "2025q1_form345.zip")
    report = load_quarter(db, z)
    assert report.form4_filings == 3  # the 4/A amendment is ignored
    assert report.skipped_no_ticker == 1
    assert report.filings_stored == 2 and report.transactions_stored == 3
    with db.session() as s:
        txs = list(s.scalars(select(InsiderTransactionRow).order_by(InsiderTransactionRow.id)))
        acme = [t for t in txs if t.ticker == "ACME"]
        assert [t.transaction_date for t in acme] == [date(2025, 2, 27), date(2025, 2, 28)]  # by SK order
        assert all(t.owner_name == "DOE JANE" and t.is_officer and t.is_director for t in acme)
        berk = next(t for t in txs if t.ticker == "BRK-B")
        assert berk.is_10b5_1 and berk.price is None and not berk.direct
        f = s.get(FilingRow, "0000000001-25-000001")
        assert f is not None and f.accepted_at == end_of_filing_day(date(2025, 3, 3))
    again = load_quarter(db, z)
    assert again.skipped_existing == 2 and again.filings_stored == 0


# ------------------------------------------------------------------ prices


def grouped_payload(day: str) -> dict[str, object]:
    return {
        "status": "OK",
        "resultsCount": 4,
        "results": [
            {"T": "SPY", "o": 500, "h": 505, "l": 495, "c": 502, "v": 1e8},
            {"T": "BRK.B", "o": 400.5, "h": 401, "l": 399, "c": 400, "v": 3e6},
            {"T": "GNLpD", "o": 20, "h": 20, "l": 20, "c": 20, "v": 1},
            {"T": "BAD", "o": 1},
        ],
    }


def test_massive_client_and_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Authorization", ""))
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=grouped_payload(request.url.path.rsplit("/", 1)[-1]))

    client = MassiveClient(
        api_key="k", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )
    cache = PriceCache(tmp_path / "px")
    n = backfill(client, cache, date(2025, 3, 7), date(2025, 3, 10))  # Fri + Mon (weekend skipped)
    assert n == 2 and calls[0] == "Bearer k"
    bars = cache.read(date(2025, 3, 7))
    assert set(bars) == {"SPY", "BRK-B"}
    assert bars["BRK-B"].open == D("400.5")
    assert backfill(client, cache, date(2025, 3, 7), date(2025, 3, 10)) == 0  # resumable
    data = cache.load(["brk-b"])
    assert list(data) == ["BRK-B"] and len(data["BRK-B"]) == 2


def test_massive_auth_error() -> None:
    client = MassiveClient(
        api_key="",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
        sleep=lambda s: None,
    )
    with pytest.raises(PriceDataError):
        client.grouped_daily(date(2025, 3, 7))


def test_massive_to_sec() -> None:
    assert massive_to_sec("BRK.B") == "BRK-B" and massive_to_sec("AAPL") == "AAPL"
    assert massive_to_sec("GNLpD") is None and massive_to_sec("IAUX.WS") is None


# ------------------------------------------------------------- event study


def bdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = bdays(date(2025, 1, 6), 120)


def flat(price: float, days: list[date] = DAYS, vol: float = 1e6) -> dict[date, Bar]:
    p = D(str(price))
    return {d: Bar(d, p, p, p, p, D(str(vol))) for d in days}


def at_close(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 16, tzinfo=NEW_YORK).astimezone(UTC)


def sig(ticker: str, key: str) -> Signal:
    return Signal(
        source="t",
        ticker=ticker,
        horizon_days=30,
        conviction=D("0.7"),
        stop_loss_pct=D("0.1"),
        thesis="t",
        dedup_key=key,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def prices_fixture() -> PriceData:
    up = {
        d: Bar(d, D(str(20 + i * 0.1)), D("99"), D("1"), D(str(20 + i * 0.1 + 0.05)), D("1000000"))
        for i, d in enumerate(DAYS)
    }
    delisted = {d: b for d, b in flat(30).items() if d <= DAYS[45]}
    return {"SPY": flat(100), "UP": up, "GONE": delisted, "PENNY": flat(2), "THIN": flat(50, vol=10)}


def test_event_builder_rules() -> None:
    cfg = StudyConfig(start=DAYS[0], end=DAYS[-1], split=DAYS[60], horizons=(5, 20))
    b = EventBuilder(prices_fixture(), cfg)
    timed = [
        TimedSignal(sig("UP", "1"), at_close(DAYS[30])),
        TimedSignal(sig("UP", "2"), at_close(DAYS[35])),  # within 30 days of the first: same event
        TimedSignal(sig("GONE", "3"), at_close(DAYS[40])),
        TimedSignal(sig("PENNY", "4"), at_close(DAYS[40])),
        TimedSignal(sig("THIN", "5"), at_close(DAYS[40])),
        TimedSignal(sig("NOPX", "6"), at_close(DAYS[40])),
    ]
    events, drops = b.build(timed)
    assert [e.ticker for e in events] == ["UP", "GONE"]
    assert drops == {"below_min_price": 1, "illiquid": 1, "no_price_at_entry": 1}
    up = events[0]
    assert up.entry_day == DAYS[31]  # signal at the close -> next session's open
    stock, bench = up.returns[5]
    exp = float(D(str(20 + 35 * 0.1 + 0.05)) / D(str(20 + 31 * 0.1)) - 1)
    assert stock == pytest.approx(exp) and bench == 0.0
    gone = events[1]
    assert 20 in gone.truncated and 5 not in gone.truncated  # delisted mid-window: last close used


def test_summarize() -> None:
    s = summarize([0.1, -0.1, 0.2, 0.0])
    assert s["n"] == 4 and s["mean"] == pytest.approx(0.05) and s["hit_rate"] == 0.5
    assert summarize([]) == {"n": 0}


def test_run_study_end_to_end_with_real_generator(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'st.sqlite'}")
    with db.session() as s:
        for owner, day in ((1, DAYS[20]), (2, DAYS[21]), (3, DAYS[80]), (4, DAYS[81])):
            s.add(
                InsiderTransactionRow(
                    accession=f"a{owner}",
                    seq=0,
                    issuer_cik=1,
                    ticker="UP",
                    owner_cik=owner,
                    owner_name=f"O{owner}",
                    is_director=True,
                    is_officer=True,
                    is_ten_pct_owner=False,
                    officer_title="CEO",
                    transaction_date=day,
                    code="P",
                    acquired_disposed="A",
                    shares=D("10000"),
                    price=D("25"),
                    shares_owned_after=None,
                    direct=True,
                    is_10b5_1=False,
                    accepted_at=at_close(day) - timedelta(hours=3),
                    ingested_at=at_close(day),
                )
            )
    cfg = StudyConfig(start=DAYS[0], end=DAYS[-1], split=DAYS[60], horizons=(5, 20))
    prices = prices_fixture()
    results = run_study(default_variants(db), DAYS, prices, cfg)
    a = results["variants"]["A"]
    assert a["in_sample"]["events"] == 1 and a["out_of_sample"]["events"] == 1
    assert a["in_sample"]["h5"]["excess"]["mean"] > 0
    assert results["chosen_variant"] in {"A", "B", "C", "D"}
    assert results["variants"]["B"]["in_sample"]["events"] == 0  # needs 3 insiders
    md = render_markdown(results)
    assert "Variante scelta" in md and "| A | IS | 1 |" in md


def test_variant_point_in_time(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'pit.sqlite'}")
    seen: list[datetime] = []

    def gen(now: datetime) -> list[Signal]:
        seen.append(now)
        return []

    cfg = StudyConfig(start=DAYS[0], end=DAYS[9], split=DAYS[5], horizons=(5,), primary_horizon=5)
    with pytest.raises(ValueError):
        StudyConfig(start=DAYS[0], end=DAYS[9], split=DAYS[5], horizons=(5,))
    run_study([Variant("X", "x", gen)], DAYS, prices_fixture(), cfg)
    assert seen == [at_close(d) for d in DAYS[:10]]
    assert db is not None


def test_event_returns_apply_splits_and_raw_price_filter() -> None:
    from trader.marketdata import Split

    raw_up = {d: Bar(d, D("40"), D("40"), D("40"), D("40"), D("1000000")) for d in DAYS[:50]}
    raw_up.update({d: Bar(d, D("20"), D("20"), D("20"), D("20"), D("2000000")) for d in DAYS[50:]})
    # A $2 stock that later does a 1-for-10 reverse split: adjusted data would show $20.
    penny = {d: Bar(d, D("2"), D("2"), D("2"), D("2"), D("5000000")) for d in DAYS[:60]}
    penny.update({d: Bar(d, D("20"), D("20"), D("20"), D("20"), D("500000")) for d in DAYS[60:]})
    prices = {"SPY": flat(100), "UP": raw_up, "PENNY": penny}
    splits = {
        "UP": [Split("UP", DAYS[50], D("1"), D("2"))],
        "PENNY": [Split("PENNY", DAYS[60], D("10"), D("1"))],
    }
    cfg = StudyConfig(start=DAYS[0], end=DAYS[-1], split=DAYS[90], horizons=(20,), primary_horizon=20)
    events, drops = EventBuilder(prices, cfg, splits).build(
        [TimedSignal(sig("UP", "1"), at_close(DAYS[40])), TimedSignal(sig("PENNY", "2"), at_close(DAYS[40]))]
    )
    assert [e.ticker for e in events] == ["UP"] and drops == {"below_min_price": 1}
    assert events[0].returns[20][0] == pytest.approx(0.0)  # 40 -> 2 x 20: no fake -50%


def test_in_sample_embargo_excludes_windows_crossing_split() -> None:
    from trader.research.insider_study import report_block

    cfg = StudyConfig(start=DAYS[0], end=DAYS[-1], split=DAYS[60], horizons=(5, 20), primary_horizon=20)
    b = EventBuilder(prices_fixture(), cfg)
    events, _ = b.build([TimedSignal(sig("UP", "1"), at_close(DAYS[50]))])  # 20-session exit after split
    block = report_block(events, cfg, embargo_before=cfg.split)
    assert block["h5"]["excess"]["n"] == 1  # exits on DAYS[55] < split
    assert block["h20"]["excess"] == {"n": 0} and block["h20"]["embargoed"] == 1


def test_massive_raw_bars_and_split_pagination(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "splits" in request.url.path:
            if "cursor" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "ticker": "BRK.B",
                                "execution_date": "2025-06-02",
                                "split_from": 1,
                                "split_to": 3,
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"ticker": "ACME", "execution_date": "2025-03-03", "split_from": 10, "split_to": 1},
                        {"ticker": "XYZpA", "execution_date": "2025-03-03", "split_from": 1, "split_to": 2},
                    ],
                    "next_url": "https://api.massive.com/v3/reference/splits?cursor=abc",
                },
            )
        return httpx.Response(200, json=grouped_payload("x"))

    client = MassiveClient(
        api_key="", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )
    client.grouped_daily(date(2025, 3, 7))
    assert "adjusted=false" in seen[0]
    splits = client.splits(date(2025, 1, 1), date(2025, 12, 31))
    assert [(s.ticker, s.ratio) for s in splits] == [("ACME", D("0.1")), ("BRK-B", D("3"))]
    cache = PriceCache(tmp_path / "px")
    cache.write_splits(splits)
    assert cache.read_splits()["BRK-B"][0].execution_date == date(2025, 6, 2)
