"""Parser for SEC ownership documents (Forms 3/4/5 XML, schema X0609 and earlier)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


class OwnershipParseError(ValueError):
    pass


@dataclass(frozen=True)
class Owner:
    cik: int
    name: str
    is_director: bool
    is_officer: bool
    is_ten_pct_owner: bool
    officer_title: str


@dataclass(frozen=True)
class InsiderTransaction:
    seq: int
    security_title: str
    transaction_date: date
    code: str
    acquired_disposed: str
    shares: Decimal
    price: Decimal | None
    shares_owned_after: Decimal | None
    direct: bool

    @property
    def value(self) -> Decimal:
        return self.shares * self.price if self.price is not None else Decimal("0")

    @property
    def is_open_market_purchase(self) -> bool:
        return self.code == "P" and self.acquired_disposed == "A" and self.price is not None and self.price > 0


@dataclass(frozen=True)
class OwnershipDocument:
    document_type: str
    issuer_cik: int
    issuer_name: str
    ticker: str
    owners: tuple[Owner, ...]
    transactions: tuple[InsiderTransaction, ...]
    is_10b5_1: bool

    @property
    def primary_owner(self) -> Owner:
        """Joint filings report one economic transaction for several owners: attribute it once,
        preferring an officer/director over entities."""
        insiders = [o for o in self.owners if o.is_officer or o.is_director]
        return insiders[0] if insiders else self.owners[0]


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _flag(node: ET.Element | None, path: str) -> bool:
    return _text(node, path).lower() in {"1", "true"}


def _decimal(raw: str) -> Decimal | None:
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def parse_ownership_xml(xml: str) -> OwnershipDocument:
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError as exc:
        raise OwnershipParseError(f"invalid XML: {exc}") from exc
    if root.tag != "ownershipDocument":
        raise OwnershipParseError(f"unexpected root element {root.tag}")

    issuer = root.find("issuer")
    issuer_cik_raw = _text(issuer, "issuerCik")
    if not issuer_cik_raw.isdigit():
        raise OwnershipParseError("missing issuer CIK")

    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        cik_raw = _text(ro, "reportingOwnerId/rptOwnerCik")
        owners.append(
            Owner(
                cik=int(cik_raw) if cik_raw.isdigit() else 0,
                name=_text(ro, "reportingOwnerId/rptOwnerName"),
                is_director=_flag(rel, "isDirector"),
                is_officer=_flag(rel, "isOfficer"),
                is_ten_pct_owner=_flag(rel, "isTenPercentOwner"),
                officer_title=_text(rel, "officerTitle"),
            )
        )
    if not owners:
        raise OwnershipParseError("no reporting owner")

    transactions = []
    for seq, tx in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        shares = _decimal(_text(tx, "transactionAmounts/transactionShares/value"))
        tx_date = _text(tx, "transactionDate/value")[:10]
        if shares is None or not tx_date:
            continue
        transactions.append(
            InsiderTransaction(
                seq=seq,
                security_title=_text(tx, "securityTitle/value"),
                transaction_date=date.fromisoformat(tx_date),
                code=_text(tx, "transactionCoding/transactionCode"),
                acquired_disposed=_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                shares=shares,
                price=_decimal(_text(tx, "transactionAmounts/transactionPricePerShare/value")),
                shares_owned_after=_decimal(
                    _text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
                ),
                direct=_text(tx, "ownershipNature/directOrIndirectOwnership/value") != "I",
            )
        )

    return OwnershipDocument(
        document_type=_text(root, "documentType"),
        issuer_cik=int(issuer_cik_raw),
        issuer_name=_text(issuer, "issuerName"),
        ticker=_text(issuer, "issuerTradingSymbol").upper(),
        owners=tuple(owners),
        transactions=tuple(transactions),
        is_10b5_1=_flag(root, "aff10b5One"),
    )
