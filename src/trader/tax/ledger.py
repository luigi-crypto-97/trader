"""Italian tax ledger for a foreign broker account (regime dichiarativo).

DRAFT - to be validated by a commercialista before filing. Implements:

* capital gains/losses on shares ("redditi diversi", art. 67 TUIR) computed in EUR;
* cost and proceeds converted at the ECB rate of the operation date, or the closest previous
  day (art. 9, c. 2, TUIR);
* weighted average cost per security ("costo medio ponderato", art. 68, c. 6, TUIR);
* commissions added to cost on purchases and deducted from proceeds on sales;
* an IVAFE estimate (2 per mille, pro rata by days held) and RW holding periods.

Not modelled: loss carry-forward across years (4 years), dividends (quadro RM), currency gains
on cash balances (relevant only above the art. 67, c. 1-ter threshold), corporate actions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from trader.broker.base import market_date
from trader.domain import Fill, Side

CAPITAL_GAINS_RATE = Decimal("0.26")
IVAFE_RATE = Decimal("0.002")
EUR_CENT = Decimal("0.01")

FxLookup = Callable[[date], Decimal]  # USD per 1 EUR on a given day


def eur(x: Decimal) -> Decimal:
    return x.quantize(EUR_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RealizedEvent:
    day: date
    ticker: str
    qty: Decimal
    proceeds_usd: Decimal
    fx_rate: Decimal
    proceeds_eur: Decimal
    cost_eur: Decimal

    @property
    def gain_eur(self) -> Decimal:
        return self.proceeds_eur - self.cost_eur


@dataclass
class HoldingPeriod:
    ticker: str
    opened: date
    closed: date | None = None
    final_value_eur: Decimal | None = None


@dataclass
class _Holding:
    qty: Decimal = Decimal("0")
    cost_eur: Decimal = Decimal("0")


@dataclass
class ItalianTaxLedger:
    fx: FxLookup
    holdings: dict[str, _Holding] = field(default_factory=dict)
    events: list[RealizedEvent] = field(default_factory=list)
    periods: list[HoldingPeriod] = field(default_factory=list)

    def process(self, fills: Iterable[Fill]) -> None:
        for f in sorted(fills, key=lambda x: (x.ts, x.fill_id)):
            self._apply(f)

    def _apply(self, f: Fill) -> None:
        day = market_date(f.ts)
        rate = self.fx(day)
        h = self.holdings.setdefault(f.ticker, _Holding())
        if f.side is Side.BUY:
            if h.qty == 0:
                self.periods.append(HoldingPeriod(f.ticker, opened=day))
            h.qty += f.qty
            h.cost_eur += (f.qty * f.price + f.commission) / rate
            return
        if f.qty > h.qty:
            raise ValueError(f"sale of {f.qty} {f.ticker} exceeds holding {h.qty} (short sales unsupported)")
        avg = h.cost_eur / h.qty
        cost = avg * f.qty
        proceeds_usd = f.qty * f.price - f.commission
        proceeds_eur = proceeds_usd / rate
        self.events.append(
            RealizedEvent(day=day, ticker=f.ticker, qty=f.qty, proceeds_usd=proceeds_usd, fx_rate=rate,
                          proceeds_eur=proceeds_eur, cost_eur=cost)
        )
        h.qty -= f.qty
        h.cost_eur -= cost
        if h.qty == 0:
            h.cost_eur = Decimal("0")
            period = next(p for p in reversed(self.periods) if p.ticker == f.ticker and p.closed is None)
            period.closed = day
            period.final_value_eur = proceeds_eur

    # --------------------------------------------------------------- reports
    def capital_gains(self, year: int) -> dict[str, object]:
        evs = [e for e in self.events if e.day.year == year]
        gains = sum((e.gain_eur for e in evs if e.gain_eur > 0), Decimal("0"))
        losses = sum((-e.gain_eur for e in evs if e.gain_eur < 0), Decimal("0"))
        net = gains - losses
        return {
            "year": year,
            "operations": len(evs),
            "plusvalenze_eur": str(eur(gains)),
            "minusvalenze_eur": str(eur(losses)),
            "netto_eur": str(eur(net)),
            "imposta_sostitutiva_stimata_eur": str(eur(max(net, Decimal("0")) * CAPITAL_GAINS_RATE)),
            "minusvalenze_riportabili_eur": str(eur(max(-net, Decimal("0")))),
            "detail": [
                {"date": e.day.isoformat(), "ticker": e.ticker, "qty": str(e.qty),
                 "fx_usd_per_eur": str(e.fx_rate), "proceeds_eur": str(eur(e.proceeds_eur)),
                 "cost_eur": str(eur(e.cost_eur)), "gain_eur": str(eur(e.gain_eur))}
                for e in evs
            ],
        }

    def ivafe_estimate(self, year: int, year_end_values_eur: dict[str, Decimal]) -> dict[str, object]:
        """IVAFE estimate: 2 per mille of the value at period end, pro rata by days held in the year.

        ``year_end_values_eur``: market value in EUR at 31/12 of positions still open at year end.
        Uses actual days in the year (365 or 366).
        """
        start, end = date(year, 1, 1), date(year, 12, 31)
        days_in_year = (end - start).days + 1
        rows = []
        total = Decimal("0")
        for p in self.periods:
            if p.opened > end or (p.closed is not None and p.closed < start):
                continue
            first = max(p.opened, start)
            last = min(p.closed, end) if p.closed is not None else end
            days = (last - first).days + 1
            if p.closed is not None and p.closed <= end:
                value = p.final_value_eur or Decimal("0")
            else:
                if p.ticker not in year_end_values_eur:
                    raise ValueError(f"missing year-end value for open position {p.ticker}")
                value = year_end_values_eur[p.ticker]
            tax = value * IVAFE_RATE * days / days_in_year
            total += tax
            rows.append({"ticker": p.ticker, "from": first.isoformat(), "to": last.isoformat(), "days": days,
                         "value_eur": str(eur(value)), "ivafe_eur": str(eur(tax))})
        return {"year": year, "ivafe_stimata_eur": str(eur(total)), "periodi_rw": rows}
