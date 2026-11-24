"""Insider cluster buying: a deterministic, explainable baseline signal.

Rationale: open-market purchases by several officers/directors of the same company within a
short window are a well-documented positive predictor of future returns (opportunistic, not
routine, insider buying). Pre-scheduled Rule 10b5-1 plan trades carry little information and are
excluded by default. Only data accepted by the SEC *before* ``now`` is used (point-in-time).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from trader.db import Database, FilingRow, InsiderTransactionRow
from trader.domain import Signal

SOURCE = "insider_cluster"
_SENIOR = ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL", "PRESIDENT", "CHAIR")


@dataclass(frozen=True)
class InsiderClusterParams:
    lookback_days: int = 30
    min_insiders: int = 2
    min_total_value: Decimal = Decimal("100000")
    min_insider_value: Decimal = Decimal("10000")
    min_purchase_price: Decimal = Decimal("5")
    exclude_10b5_1: bool = True
    require_officer_or_director: bool = True
    horizon_days: int = 60
    stop_loss_pct: Decimal = Decimal("0.10")


@dataclass
class _Buyer:
    name: str
    title: str
    value: Decimal
    senior: bool


def _is_senior(title: str) -> bool:
    upper = title.upper()
    return any(s in upper for s in _SENIOR)


def find_insider_clusters(
    db: Database, now: datetime, params: InsiderClusterParams | None = None
) -> list[Signal]:
    p = params or InsiderClusterParams()
    start = now - timedelta(days=p.lookback_days)
    with db.session() as s:
        rows = list(
            s.scalars(
                select(InsiderTransactionRow).where(
                    InsiderTransactionRow.accepted_at > start,
                    InsiderTransactionRow.accepted_at <= now,
                    InsiderTransactionRow.code == "P",
                    InsiderTransactionRow.acquired_disposed == "A",
                    InsiderTransactionRow.ticker.is_not(None),
                )
            )
        )
        urls = {
            f.accession: f.url
            for f in s.scalars(select(FilingRow).where(FilingRow.accession.in_({r.accession for r in rows})))
        }

    by_ticker: dict[str, list[InsiderTransactionRow]] = defaultdict(list)
    for r in rows:
        if r.price is None or r.price < p.min_purchase_price:
            continue
        if p.exclude_10b5_1 and r.is_10b5_1:
            continue
        if p.require_officer_or_director and not (r.is_officer or r.is_director):
            continue
        assert r.ticker is not None
        by_ticker[r.ticker].append(r)

    signals: list[Signal] = []
    for ticker, txs in sorted(by_ticker.items()):
        buyers: dict[int, _Buyer] = {}
        for t in txs:
            assert t.price is not None
            b = buyers.setdefault(
                t.owner_cik, _Buyer(t.owner_name, t.officer_title, Decimal("0"), _is_senior(t.officer_title))
            )
            b.value += t.shares * t.price
        qualified = {k: b for k, b in buyers.items() if b.value >= p.min_insider_value}
        total = sum((b.value for b in qualified.values()), Decimal("0"))
        if len(qualified) < p.min_insiders or total < p.min_total_value:
            continue

        latest = max(t.accepted_at for t in txs)
        conviction = Decimal("0.55") + Decimal("0.10") * (len(qualified) - p.min_insiders)
        conviction += Decimal("0.10") * (total / p.min_total_value).log10()
        if any(b.senior for b in qualified.values()):
            conviction += Decimal("0.05")
        conviction = min(max(conviction, Decimal("0")), Decimal("0.95")).quantize(Decimal("0.01"))

        evidence: list[dict[str, Any]] = [
            {
                "owner": t.owner_name,
                "title": t.officer_title or ("Director" if t.is_director else ""),
                "transaction_date": t.transaction_date.isoformat(),
                "accepted_at": t.accepted_at.isoformat(),
                "shares": str(t.shares),
                "price": str(t.price),
                "value": str((t.shares * t.price).quantize(Decimal("0.01"))) if t.price else "0",
                "accession": t.accession,
                "url": urls.get(t.accession, ""),
            }
            for t in sorted(txs, key=lambda x: x.accepted_at)
            if t.owner_cik in qualified
        ]
        names = ", ".join(f"{b.name} ({b.title or 'Director'})" for b in qualified.values())
        iso = latest.isocalendar()
        signals.append(
            Signal(
                source=SOURCE,
                ticker=ticker,
                horizon_days=p.horizon_days,
                conviction=conviction,
                stop_loss_pct=p.stop_loss_pct,
                thesis=(
                    f"{len(qualified)} insider hanno acquistato azioni {ticker} sul mercato aperto negli "
                    f"ultimi {p.lookback_days} giorni per un totale di ${total:,.0f}: {names}. "
                    f"Acquisti discrezionali (non piani 10b5-1)."
                ),
                evidence=evidence,
                dedup_key=f"{SOURCE}:{ticker}:{iso.year}-W{iso.week:02d}",
                created_at=now,
            )
        )
    return signals
