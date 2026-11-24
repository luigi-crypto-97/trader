"""Commission models used for pre-trade estimates, simulation and backtests."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class CommissionModel(Protocol):
    def estimate(self, qty: Decimal, price: Decimal) -> Decimal: ...


@dataclass(frozen=True)
class IbkrTieredUS:
    """Approximation of IBKR Pro *Tiered* pricing for US stocks (lowest-volume tier).

    Commission: USD 0.0035/share, minimum USD 0.35/order, capped at 1% of trade value
    (when the cap is below the minimum, IBKR charges the cap). Third-party fees (exchange,
    clearing, regulatory) vary per venue; a conservative flat per-share estimate is added.
    Verify against the live IBKR schedule before relying on exact figures.
    """

    per_share: Decimal = Decimal("0.0035")
    min_per_order: Decimal = Decimal("0.35")
    max_pct_of_value: Decimal = Decimal("0.01")
    third_party_per_share: Decimal = Decimal("0.0030")

    def estimate(self, qty: Decimal, price: Decimal) -> Decimal:
        if qty <= 0 or price <= 0:
            return Decimal("0")
        value = qty * price
        commission = min(max(qty * self.per_share, self.min_per_order), value * self.max_pct_of_value)
        return (commission + qty * self.third_party_per_share).quantize(Decimal("0.0001"))


@dataclass(frozen=True)
class FlatCommission:
    per_order: Decimal = Decimal("0")

    def estimate(self, qty: Decimal, price: Decimal) -> Decimal:
        return self.per_order if qty > 0 else Decimal("0")
