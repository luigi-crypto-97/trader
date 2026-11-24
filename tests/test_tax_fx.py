from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from trader.data.fx import EcbClient, FxError, FxRates
from trader.db import Database
from trader.domain import Fill, Side
from trader.tax.ledger import ItalianTaxLedger

CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-11-19,1.0992,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-11-20,1.099,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-11-23,1.0939,A\n"
)


def fill(fid: str, side: Side, qty: str, price: str, comm: str, day: int) -> Fill:
    return Fill(fill_id=fid, client_order_id="c", ticker="ACME", side=side, qty=D(qty), price=D(price),
                commission=D(comm), ts=datetime(2026, 3, day, 15, tzinfo=UTC))


FX = {date(2026, 3, 2): D("1.10"), date(2026, 3, 3): D("1.00"), date(2026, 3, 4): D("1.25")}


def test_weighted_average_cost_in_eur_with_daily_fx() -> None:
    ledger = ItalianTaxLedger(fx=lambda d: FX[d])
    ledger.process([
        fill("1", Side.BUY, "10", "11", "1", 2),
        fill("2", Side.BUY, "10", "10", "1", 3),
        fill("3", Side.SELL, "5", "12.5", "1", 4),
        fill("4", Side.SELL, "15", "12.5", "1", 4),
    ])
    e1, e2 = ledger.events
    assert e1.proceeds_eur == D("49.2")
    assert round(e1.cost_eur, 6) == D("50.477273")
    assert round(e2.cost_eur, 6) == D("151.431818")
    report = ledger.capital_gains(2026)
    assert report["plusvalenze_eur"] == "0.00"
    assert report["minusvalenze_eur"] == "3.51"
    assert report["netto_eur"] == "-3.51"
    assert report["imposta_sostitutiva_stimata_eur"] == "0.00"
    assert report["minusvalenze_riportabili_eur"] == "3.51"
    assert ledger.holdings["ACME"].qty == 0


def test_gain_taxed_at_26_percent() -> None:
    ledger = ItalianTaxLedger(fx=lambda d: D("1"))
    ledger.process([fill("1", Side.BUY, "10", "10", "0", 2), fill("2", Side.SELL, "10", "20", "0", 4)])
    report = ledger.capital_gains(2026)
    assert report["plusvalenze_eur"] == "100.00"
    assert report["imposta_sostitutiva_stimata_eur"] == "26.00"


def test_oversell_rejected() -> None:
    ledger = ItalianTaxLedger(fx=lambda d: D("1"))
    with pytest.raises(ValueError):
        ledger.process([fill("1", Side.SELL, "1", "10", "0", 2)])


def test_ivafe_estimate_pro_rata() -> None:
    ledger = ItalianTaxLedger(fx=lambda d: D("1"))
    ledger.process([
        fill("1", Side.BUY, "10", "100", "0", 2),
        fill("2", Side.SELL, "10", "100", "0", 4),  # closed: value 1000, 3 days
        fill("3", Side.BUY, "1", "100", "0", 4),     # open at year end
    ])
    with pytest.raises(ValueError):
        ledger.ivafe_estimate(2026, {})
    out = ledger.ivafe_estimate(2026, {"ACME": D("365")})
    periods = out["periodi_rw"]
    assert isinstance(periods, list) and len(periods) == 2
    assert periods[0]["days"] == 3 and periods[0]["ivafe_eur"] == "0.02"  # 1000*0.002*3/365
    assert periods[1]["to"] == "2026-12-31"


# ------------------------------------------------------------------- FX


def ecb_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=CSV)

    return httpx.MockTransport(handler)


def test_fx_previous_business_day_and_cache(tmp_path: Path) -> None:
    calls: list[str] = []
    fx = FxRates(Database(f"sqlite:///{tmp_path / 'fx.sqlite'}"),
                 EcbClient(httpx.Client(transport=ecb_transport(calls))))
    assert fx.rate("USD", date(2026, 11, 23)) == D("1.0939")
    assert fx.rate("USD", date(2026, 11, 22)) == D("1.099")  # Sunday -> Friday
    assert fx.rate("USD", date(2026, 11, 21)) == D("1.099")  # Saturday -> Friday
    assert len(calls) == 1  # everything else served from cache
    assert fx.to_eur(D("1099"), "USD", date(2026, 11, 20)) == D("1000")
    assert fx.rate("EUR", date(2026, 1, 1)) == D("1")


def test_fx_missing_rate_raises(tmp_path: Path) -> None:
    empty = httpx.MockTransport(lambda r: httpx.Response(404))
    fx = FxRates(Database(f"sqlite:///{tmp_path / 'fx2.sqlite'}"), EcbClient(httpx.Client(transport=empty)))
    with pytest.raises(FxError):
        fx.rate("USD", date(2026, 11, 23))


@pytest.mark.live
def test_live_ecb(tmp_path: Path) -> None:
    fx = FxRates(Database(f"sqlite:///{tmp_path / 'fx3.sqlite'}"))
    assert D("0.5") < fx.rate("USD", date(2026, 11, 23)) < D("2")
