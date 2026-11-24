"""Position sizing: how much to buy, given a signal, a quote and the account.

The size is the minimum of every constraint (risk budget, position cap, settled cash, gross
exposure), rounded *down* to the quantity step. Sizing never relaxes a limit; the risk engine
re-checks the result independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trader.broker.commissions import CommissionModel
from trader.config import RiskLimits
from trader.domain import AccountSnapshot, Quote, Signal

CENT = Decimal("0.01")
SUBPENNY = Decimal("0.0001")


def round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def tick_size(price: Decimal) -> Decimal:
    """Reg NMS minimum price increment: $0.01 at or above $1, $0.0001 below."""
    return CENT if price >= 1 else SUBPENNY


def round_price_up(price: Decimal) -> Decimal:
    tick = tick_size(price)
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def round_price_down(price: Decimal) -> Decimal:
    tick = tick_size(price)
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


@dataclass(frozen=True)
class EntryPlan:
    qty: Decimal
    limit_price: Decimal
    stop_price: Decimal
    stop_loss_pct: Decimal
    notional: Decimal
    est_commission: Decimal
    risk_amount: Decimal
    binding_constraint: str

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class SizingRejection:
    reason: str


def plan_entry(
    signal: Signal,
    quote: Quote,
    account: AccountSnapshot,
    limits: RiskLimits,
    commissions: CommissionModel,
) -> EntryPlan | SizingRejection:
    if signal.conviction < limits.min_conviction:
        return SizingRejection(f"conviction {signal.conviction} below minimum {limits.min_conviction}")

    reference = quote.ask if quote.ask is not None and quote.ask > 0 else quote.ref_price
    if reference is None:
        return SizingRejection("no usable price in quote")

    limit_price = round_price_up(reference * (1 + limits.limit_offset_pct))
    stop_pct = min(max(signal.stop_loss_pct, limits.min_stop_loss_pct), limits.max_stop_loss_pct)
    stop_price = round_price_down(limit_price * (1 - stop_pct))
    per_share_risk = limit_price - stop_price
    if per_share_risk <= 0:
        return SizingRejection("stop price not below entry price")

    equity = account.equity
    if equity <= 0:
        return SizingRejection("non-positive equity")

    span = Decimal("1") - limits.min_conviction
    scaled = (signal.conviction - limits.min_conviction) / span if span > 0 else Decimal("1")
    conviction_factor = Decimal("0.5") + Decimal("0.5") * min(max(scaled, Decimal("0")), Decimal("1"))
    risk_budget = equity * limits.risk_per_trade_pct * conviction_factor

    existing = account.positions.get(signal.ticker)
    existing_value = existing.market_value if existing is not None else Decimal("0")
    # Reserve the worst-case commission (minimum ticket or the 1% cap) out of settled cash.
    commission_reserve = max(commissions.estimate(Decimal("1"), limit_price), account.settled_cash * CENT)

    candidates = {
        "risk_budget": risk_budget / per_share_risk,
        "max_position": (equity * limits.max_position_pct - existing_value) / limit_price,
        "settled_cash": (account.settled_cash - commission_reserve) / limit_price,
        "gross_exposure": (equity * limits.max_gross_exposure_pct - account.gross_exposure) / limit_price,
    }
    binding = min(candidates, key=lambda k: candidates[k])
    step = limits.qty_step if limits.allow_fractional else Decimal("1")
    qty = round_down(max(candidates[binding], Decimal("0")), step)
    if qty <= 0:
        return SizingRejection(f"size rounds to zero (binding constraint: {binding})")

    notional = qty * limit_price
    if notional < limits.min_order_value:
        return SizingRejection(
            f"size {qty} (notional {notional}) below minimum order value {limits.min_order_value} "
            f"(binding constraint: {binding})"
        )
    return EntryPlan(
        qty=qty,
        limit_price=limit_price,
        stop_price=stop_price,
        stop_loss_pct=stop_pct,
        notional=notional,
        est_commission=commissions.estimate(qty, limit_price),
        risk_amount=qty * per_share_risk,
        binding_constraint=binding,
    )
