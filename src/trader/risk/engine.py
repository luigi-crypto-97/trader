"""Deterministic pre-trade risk engine.

This module is the last line of defence between any signal source (quant rules, LLMs, humans)
and the broker. It is pure: no I/O, no clock reads, no randomness. Every check is evaluated and
reported so that a rejection always carries its full explanation.

Entries (BUY) must pass every check. Exits (SELL that reduce an existing long position) are
deliberately allowed even when the kill switch or loss limits are active: reducing risk must
never be blocked. The account is long-only: a SELL can never exceed the quantity held.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trader.broker.commissions import CommissionModel
from trader.config import RiskLimits
from trader.domain import AccountSnapshot, OrderRequest, OrderType, Quote, Side


@dataclass(frozen=True)
class RiskState:
    """Account-level state the engine needs beyond the snapshot."""

    kill_switch: bool
    orders_today: int
    start_of_day_equity: Decimal | None
    peak_equity: Decimal | None


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    checks: tuple[Check, ...]
    trip_kill_switch: bool = False

    @property
    def violations(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        if self.approved:
            return "approved"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.violations)

    def as_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "trip_kill_switch": self.trip_kill_switch,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
        }


@dataclass
class _Collector:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(name, passed, detail))


def _pct(x: Decimal) -> str:
    return f"{(x * 100).quantize(Decimal('0.01'))}%"


class RiskEngine:
    def __init__(self, limits: RiskLimits, commissions: CommissionModel) -> None:
        self.limits = limits
        self.commissions = commissions

    def evaluate(
        self,
        order: OrderRequest,
        *,
        quote: Quote | None,
        account: AccountSnapshot,
        state: RiskState,
        now: datetime,
    ) -> RiskDecision:
        if order.side is Side.SELL:
            return self._evaluate_exit(order, quote=quote, account=account, now=now)
        return self._evaluate_entry(order, quote=quote, account=account, state=state, now=now)

    # ------------------------------------------------------------------ exits
    def _evaluate_exit(
        self, order: OrderRequest, *, quote: Quote | None, account: AccountSnapshot, now: datetime
    ) -> RiskDecision:
        c = _Collector()
        held = account.qty(order.ticker)
        c.add(
            "long_only_no_short",
            held > 0 and order.qty <= held,
            f"sell {order.qty} vs held {held}",
        )
        fresh = self._fresh_ref(quote, now)
        if order.order_type is OrderType.LIMIT and order.limit_price is not None and fresh is not None:
            deviation = abs(order.limit_price - fresh) / fresh
            c.add(
                "exit_price_sanity",
                deviation <= self.limits.max_price_deviation_pct * 2,
                f"limit {order.limit_price} vs ref {fresh} ({_pct(deviation)})",
            )
        else:
            c.add("exit_price_sanity", True, "no fresh reference price; exits are never blocked on data")
        return RiskDecision(approved=all(x.passed for x in c.checks), checks=tuple(c.checks))

    # ---------------------------------------------------------------- entries
    def _evaluate_entry(
        self,
        order: OrderRequest,
        *,
        quote: Quote | None,
        account: AccountSnapshot,
        state: RiskState,
        now: datetime,
    ) -> RiskDecision:
        lim = self.limits
        c = _Collector()
        trip = False
        equity = account.equity

        c.add("kill_switch_off", not state.kill_switch, "kill switch is ON" if state.kill_switch else "off")
        c.add("positive_equity", equity > 0, f"equity {equity}")

        if state.peak_equity and state.peak_equity > 0 and equity > 0:
            dd = equity / state.peak_equity - 1
            breached = dd <= -lim.max_drawdown_pct
            trip = breached
            c.add("max_drawdown", not breached, f"drawdown {_pct(dd)} (limit -{_pct(lim.max_drawdown_pct)})")
        else:
            c.add("max_drawdown", True, "no peak recorded yet")

        if state.start_of_day_equity and state.start_of_day_equity > 0 and equity > 0:
            day = equity / state.start_of_day_equity - 1
            c.add(
                "daily_loss_limit",
                day > -lim.daily_loss_limit_pct,
                f"today {_pct(day)} (limit -{_pct(lim.daily_loss_limit_pct)})",
            )
        else:
            c.add("daily_loss_limit", True, "no start-of-day equity yet")

        c.add("not_restricted", not lim.is_restricted(order.ticker), f"{order.ticker} restricted list check")
        c.add("in_universe", lim.in_universe(order.ticker), "whitelist check")
        c.add(
            "orders_per_day",
            state.orders_today < lim.max_orders_per_day,
            f"{state.orders_today} sent today (max {lim.max_orders_per_day})",
        )
        c.add("limit_order_only", order.order_type is OrderType.LIMIT, f"order type {order.order_type}")

        step = lim.qty_step if lim.allow_fractional else Decimal("1")
        c.add("qty_step", (order.qty % step) == 0, f"qty {order.qty} step {step}")

        if quote is None:
            c.add("quote_available", False, "no quote")
            return RiskDecision(False, tuple(c.checks), trip)

        age = (now - quote.ts).total_seconds()
        c.add("quote_fresh", 0 <= age <= lim.max_quote_age_seconds, f"quote age {int(age)}s")

        ref = quote.ref_price
        spread = quote.spread_pct
        c.add("two_sided_quote", spread is not None, "bid/ask required for entries")
        if spread is not None:
            c.add("max_spread", spread <= lim.max_spread_pct, f"spread {_pct(spread)}")
        if ref is None or order.limit_price is None:
            c.add("reference_price", False, "no reference price or limit price")
            return RiskDecision(False, tuple(c.checks), trip)

        c.add("min_price", ref >= lim.min_price, f"price {ref} (min {lim.min_price})")
        deviation = abs(order.limit_price - ref) / ref
        c.add(
            "price_deviation",
            deviation <= lim.max_price_deviation_pct,
            f"limit {order.limit_price} vs ref {ref} ({_pct(deviation)})",
        )

        notional = order.qty * order.limit_price
        commission = self.commissions.estimate(order.qty, order.limit_price)
        c.add("min_order_value", notional >= lim.min_order_value, f"notional {notional}")
        if notional > 0:
            c.add(
                "commission_drag",
                commission / notional <= lim.max_commission_pct,
                f"commission {commission} = {_pct(commission / notional)} of notional",
            )
        c.add(
            "settled_cash",
            notional + commission <= account.settled_cash,
            f"needs {notional + commission}, settled cash {account.settled_cash}",
        )

        if equity > 0:
            existing = account.positions.get(order.ticker)
            existing_value = existing.market_value if existing else Decimal("0")
            pos_pct = (existing_value + notional) / equity
            c.add("max_position", pos_pct <= lim.max_position_pct, f"position {_pct(pos_pct)} of equity")
            gross_pct = (account.gross_exposure + notional) / equity
            c.add("max_gross_exposure", gross_pct <= lim.max_gross_exposure_pct, f"gross {_pct(gross_pct)}")
            opened = len(account.positions) + (0 if existing else 1)
            c.add(
                "max_positions", opened <= lim.max_positions, f"{opened} positions (max {lim.max_positions})"
            )

        adv = quote.avg_daily_dollar_volume
        if adv is None or adv <= 0:
            c.add("liquidity_adv", not lim.require_adv, "average daily dollar volume unknown")
        else:
            c.add("liquidity_adv", notional / adv <= lim.max_adv_pct, f"{_pct(notional / adv)} of ADV")

        return RiskDecision(
            approved=all(x.passed for x in c.checks), checks=tuple(c.checks), trip_kill_switch=trip
        )

    def _fresh_ref(self, quote: Quote | None, now: datetime) -> Decimal | None:
        if quote is None:
            return None
        age = (now - quote.ts).total_seconds()
        if not 0 <= age <= self.limits.max_quote_age_seconds:
            return None
        return quote.ref_price
