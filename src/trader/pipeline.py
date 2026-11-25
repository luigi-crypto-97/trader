"""Trading pipeline: signal -> sizing -> risk -> approval -> order -> fills -> exits.

State machine of a signal::

    PROPOSED --(sizing/risk fail)--> REJECTED_RISK
    PROPOSED --(ok, manual mode)--> PENDING_APPROVAL --(human yes)--> APPROVED --> ORDERED
                                                     --(human no)---> REJECTED_HUMAN
                                                     --(timeout)----> EXPIRED
    PROPOSED --(ok, auto mode)----> APPROVED --> ORDERED
    APPROVED --(risk re-check fails at execution)--> FAILED

What the human approves (ticker, quantity, limit price) is exactly what is sent to the broker;
the risk engine re-checks it against fresh market data right before submission.
Exits (stop-loss, time-based) reduce risk and are executed without approval.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import func, select

from trader.audit import AuditLog
from trader.broker.base import NEW_YORK, Broker, BrokerError, is_regular_session
from trader.broker.calendar import first_session_close_after
from trader.broker.commissions import CommissionModel
from trader.config import RiskLimits
from trader.db import (
    Database,
    EquitySnapshotRow,
    FillRow,
    OrderRow,
    PositionPlanRow,
    SignalRow,
    dumps,
)
from trader.domain import (
    AccountSnapshot,
    Fill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    Signal,
    SignalStatus,
)
from trader.risk.engine import PRIVATE_RULE, RiskEngine, RiskState
from trader.risk.sizing import EntryPlan, SizingRejection, plan_entry, round_price_down

log = logging.getLogger(__name__)

KILL_SWITCH_KEY = "kill_switch"
LAST_FILL_KEY = "last_fill_ts"
LAST_ACCOUNT_KEY = "last_account"
RECONCILE_GRACE = timedelta(minutes=30)


class Notifier(Protocol):
    def notify_pending(self, signal: dict[str, Any]) -> None: ...

    def notify(self, text: str) -> None: ...


class NullNotifier:
    def notify_pending(self, signal: dict[str, Any]) -> None:
        return None

    def notify(self, text: str) -> None:
        return None


@dataclass
class CycleReport:
    started_at: datetime
    in_session: bool
    reviewed: int = 0
    pending_approval: int = 0
    executed: int = 0
    exits: int = 0
    expired: int = 0
    new_fills: int = 0
    notes: list[str] = field(default_factory=list)


def submit_signal(db: Database, audit: AuditLog, signal: Signal, now: datetime) -> bool:
    """Store a PROPOSED signal unless its dedup key exists. Shared by the pipeline and MCP server."""
    with db.session() as s:
        if s.scalar(select(SignalRow.id).where(SignalRow.dedup_key == signal.dedup_key)):
            return False
        s.add(
            SignalRow(
                id=signal.id,
                created_at=signal.created_at,
                updated_at=now,
                source=signal.source,
                ticker=signal.ticker,
                side=signal.side.value,
                horizon_days=signal.horizon_days,
                conviction=signal.conviction,
                stop_loss_pct=signal.stop_loss_pct,
                thesis=signal.thesis,
                evidence=dumps(signal.evidence),
                dedup_key=signal.dedup_key,
                status=SignalStatus.PROPOSED.value,
            )
        )
    audit.record("signal.proposed", signal.model_dump(mode="json"), ts=now)
    return True


def signal_row_to_dict(row: SignalRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "source": row.source,
        "ticker": row.ticker,
        "side": row.side,
        "horizon_days": row.horizon_days,
        "conviction": str(row.conviction),
        "stop_loss_pct": str(row.stop_loss_pct),
        "thesis": row.thesis,
        "evidence": json.loads(row.evidence),
        "status": row.status,
        "status_reason": row.status_reason,
        "plan": json.loads(row.plan),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "decided_by": row.decided_by,
        "order_client_id": row.order_client_id,
    }


class TradingSystem:
    def __init__(
        self,
        *,
        db: Database,
        broker: Broker,
        limits: RiskLimits,
        commissions: CommissionModel,
        audit: AuditLog,
        approval_mode: Literal["manual", "auto"] = "manual",
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        session_check: Callable[[datetime], bool] = is_regular_session,
    ) -> None:
        self.db = db
        self.broker = broker
        self.limits = limits
        self.commissions = commissions
        self.risk = RiskEngine(limits, commissions)
        self.audit = audit
        self.approval_mode = approval_mode
        self.notifier: Notifier = notifier or NullNotifier()
        self.clock = clock
        self.session_check = session_check

    def _notify(self, text: str) -> None:
        """Notifications are best effort: a messaging failure must never stop sync, exits or entries."""
        try:
            self.notifier.notify(text)
        except Exception as exc:
            log.warning("notification failed: %s", exc)

    # ------------------------------------------------------------ kill switch
    def kill_switch_on(self) -> bool:
        state = self.db.kv_get(KILL_SWITCH_KEY, {"on": False})
        return bool(state.get("on", False))

    def set_kill_switch(self, on: bool, *, reason: str, actor: str) -> None:
        self.db.kv_set(
            KILL_SWITCH_KEY, {"on": on, "reason": reason, "actor": actor, "ts": self.clock().isoformat()}
        )
        self.audit.record("kill_switch", {"on": on, "reason": reason, "actor": actor}, ts=self.clock())
        self._notify(f"Kill switch {'ON' if on else 'OFF'} ({actor}): {reason}")

    # ------------------------------------------------------------- snapshots
    def snapshot(self) -> AccountSnapshot:
        account = self.broker.account()
        with self.db.session() as s:
            s.add(
                EquitySnapshotRow(
                    ts=self.clock(),
                    equity=account.equity,
                    settled_cash=account.settled_cash,
                    unsettled_cash=account.unsettled_cash,
                    positions_value=account.positions_value,
                )
            )
        self.db.kv_set(LAST_ACCOUNT_KEY, account.model_dump(mode="json"))
        return account

    def _day_start(self) -> datetime:
        local = self.clock().astimezone(NEW_YORK)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    def risk_state(self) -> RiskState:
        day_start = self._day_start()
        with self.db.session() as s:
            first_today = s.scalars(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.ts >= day_start)
                .order_by(EquitySnapshotRow.ts)
                .limit(1)
            ).first()
            peak_rows = s.scalars(select(EquitySnapshotRow.equity)).all()
            pending_buys: dict[str, Decimal] = {}
            for o in s.scalars(
                select(OrderRow).where(
                    OrderRow.side == Side.BUY.value,
                    OrderRow.status.in_([OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value]),
                )
            ):
                remaining = max(o.qty - o.filled_qty, Decimal("0"))
                # Entries are limit orders; a missing price can only be a market order: commit the worst
                # case of the whole remaining quantity at the last known fill price or the limit.
                price = o.limit_price or o.avg_fill_price or Decimal("0")
                committed = remaining * price + self.commissions.estimate(remaining, price)
                pending_buys[o.ticker] = pending_buys.get(o.ticker, Decimal("0")) + committed
            pending_sells: dict[str, Decimal] = {}
            for o in s.scalars(
                select(OrderRow).where(
                    OrderRow.side == Side.SELL.value,
                    OrderRow.status.in_([OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value]),
                )
            ):
                rest = max(o.qty - o.filled_qty, Decimal("0"))
                pending_sells[o.ticker] = pending_sells.get(o.ticker, Decimal("0")) + rest
            orders_today = s.scalar(
                select(func.count())
                .select_from(OrderRow)
                .where(OrderRow.created_at >= day_start, OrderRow.purpose == "ENTRY")
            )
        return RiskState(
            kill_switch=self.kill_switch_on(),
            orders_today=int(orders_today or 0),
            start_of_day_equity=first_today.equity if first_today else None,
            peak_equity=max(peak_rows) if peak_rows else None,
            pending_buys=pending_buys,
            pending_sells=pending_sells,
        )

    # --------------------------------------------------------------- signals
    def submit_signal(self, signal: Signal) -> bool:
        """Insert a new PROPOSED signal. Returns False if its dedup key was already seen or the ticker
        is on the private restricted list (dropped without storing the ticker anywhere)."""
        if self.limits.is_restricted(signal.ticker):
            self.audit.record(
                "signal.dropped", {"source": signal.source, "reason": PRIVATE_RULE}, ts=self.clock()
            )
            return False
        return submit_signal(self.db, self.audit, signal, self.clock())

    def _drop_private(self, signal_id: str) -> None:
        """Remove a queued signal whose ticker is on the private list, leaving no ticker-to-list link
        (the audit record carries neither the signal id nor the ticker)."""
        with self.db.session() as s:
            row = s.get(SignalRow, signal_id)
            if row is not None:
                s.delete(row)
        self.audit.record("signal.dropped", {"reason": PRIVATE_RULE}, ts=self.clock())

    def _set_status(self, signal_id: str, status: SignalStatus, reason: str = "", **fields: Any) -> None:
        with self.db.session() as s:
            row = s.get(SignalRow, signal_id)
            if row is None:
                raise KeyError(signal_id)
            row.status = status.value
            row.status_reason = reason
            row.updated_at = self.clock()
            for k, v in fields.items():
                setattr(row, k, v)
        self.audit.record(
            f"signal.{status.value.lower()}",
            {"signal_id": signal_id, "reason": reason, **{k: str(v) for k, v in fields.items()}},
            ts=self.clock(),
        )

    def list_signals(self, status: SignalStatus | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(SignalRow).order_by(SignalRow.created_at.desc()).limit(limit)
            if status is not None:
                q = q.where(SignalRow.status == status.value)
            return [signal_row_to_dict(r) for r in s.scalars(q)]

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(SignalRow, signal_id)
            return signal_row_to_dict(row) if row else None

    @staticmethod
    def _row_to_signal(row: SignalRow) -> Signal:
        return Signal(
            id=row.id,
            created_at=row.created_at,
            source=row.source,
            ticker=row.ticker,
            side=Side(row.side),
            horizon_days=row.horizon_days,
            conviction=row.conviction,
            stop_loss_pct=row.stop_loss_pct,
            thesis=row.thesis,
            evidence=json.loads(row.evidence),
            dedup_key=row.dedup_key,
        )

    def _rows(self, status: SignalStatus) -> list[SignalRow]:
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(SignalRow).where(SignalRow.status == status.value).order_by(SignalRow.created_at)
                )
            )

    # ---------------------------------------------------------------- review
    def expire_stale(self) -> int:
        now = self.clock()
        expired = 0
        max_age = timedelta(hours=self.limits.signal_max_age_hours)
        for row in self._rows(SignalStatus.PROPOSED):
            if now - row.created_at > max_age:
                self._set_status(row.id, SignalStatus.EXPIRED, "not reviewed within max age")
                expired += 1
        for status in (SignalStatus.PENDING_APPROVAL, SignalStatus.APPROVED):
            for row in self._rows(status):
                if row.expires_at is not None and now > row.expires_at:
                    self._set_status(row.id, SignalStatus.EXPIRED, f"{status.value} past expiry")
                    expired += 1
        return expired

    def review_proposed(self, account: AccountSnapshot) -> tuple[int, int]:
        """Size and risk-check every PROPOSED signal. Returns (reviewed, sent_for_approval)."""
        reviewed = pending = 0
        for row in self._rows(SignalStatus.PROPOSED):
            reviewed += 1
            signal = self._row_to_signal(row)
            if signal.side is not Side.BUY:
                self._set_status(row.id, SignalStatus.REJECTED_RISK, "only long entries are supported")
                continue
            if self.limits.is_restricted(signal.ticker):
                self._drop_private(row.id)
                continue
            quote = self.broker.quote(signal.ticker)
            if quote is None:
                self._set_status(row.id, SignalStatus.REJECTED_RISK, "no market data")
                continue
            # Size against cash not already committed to unfilled BUY orders.
            committed = self.risk_state().pending_total
            usable = min(committed, account.settled_cash)
            sizing_account = account.model_copy(
                update={
                    "settled_cash": account.settled_cash - usable,
                    "unsettled_cash": account.unsettled_cash + usable,  # equity unchanged
                }
            )
            plan = plan_entry(signal, quote, sizing_account, self.limits, self.commissions)
            if isinstance(plan, SizingRejection):
                self._set_status(row.id, SignalStatus.REJECTED_RISK, f"sizing: {plan.reason}")
                continue
            order = self._entry_order(signal, plan)
            decision = self.risk.evaluate(
                order, quote=quote, account=account, state=self.risk_state(), now=self.clock()
            )
            if decision.trip_kill_switch and not self.kill_switch_on():
                self.set_kill_switch(True, reason="max drawdown breached", actor="risk-engine")
            plan_json = dumps(
                {"entry": plan.as_dict(), "risk": decision.as_dict(), "quote": quote.model_dump(mode="json")}
            )
            if not decision.approved:
                self._set_status(row.id, SignalStatus.REJECTED_RISK, decision.summary(), plan=plan_json)
                continue
            if self.approval_mode == "auto":
                self._set_status(
                    row.id,
                    SignalStatus.APPROVED,
                    "auto-approved",
                    plan=plan_json,
                    decided_by="auto",
                    expires_at=self.clock() + timedelta(minutes=30),
                )
            else:
                expires = self.clock() + timedelta(minutes=self.limits.signal_ttl_minutes)
                self._set_status(
                    row.id,
                    SignalStatus.PENDING_APPROVAL,
                    "awaiting human approval",
                    plan=plan_json,
                    expires_at=expires,
                )
                pending += 1
                details = self.get_signal(row.id)
                if details is not None:
                    try:
                        self.notifier.notify_pending(details)
                    except Exception as exc:  # notification failure must not block the pipeline
                        log.warning("approval notification failed: %s", exc)
        return reviewed, pending

    @staticmethod
    def _entry_order(signal: Signal, plan: EntryPlan) -> OrderRequest:
        return OrderRequest(
            client_order_id=signal.id,
            ticker=signal.ticker,
            side=Side.BUY,
            qty=plan.qty,
            order_type=OrderType.LIMIT,
            limit_price=plan.limit_price,
        )

    # -------------------------------------------------------------- approval
    def decide(self, signal_id: str, *, approve: bool, actor: str, note: str = "") -> tuple[bool, str]:
        with self.db.session() as s:
            row = s.get(SignalRow, signal_id)
            if row is None:
                return False, "signal not found"
            status, expires = row.status, row.expires_at
        if status != SignalStatus.PENDING_APPROVAL.value:
            return False, f"signal is {status}, not pending approval"
        if expires is not None and self.clock() > expires:
            self._set_status(signal_id, SignalStatus.EXPIRED, "approval arrived after expiry")
            return False, "approval window expired"
        if approve:
            self._set_status(signal_id, SignalStatus.APPROVED, note or "approved", decided_by=actor)
            return True, "approved"
        self._set_status(signal_id, SignalStatus.REJECTED_HUMAN, note or "rejected", decided_by=actor)
        return True, "rejected"

    # ------------------------------------------------------------- execution
    def execute_approved(self, account: AccountSnapshot) -> int:
        executed = 0
        for row in self._rows(SignalStatus.APPROVED):
            if self.limits.is_restricted(row.ticker):
                self._drop_private(row.id)
                continue
            plan = json.loads(row.plan).get("entry")
            if not plan:
                self._set_status(row.id, SignalStatus.FAILED, "approved signal has no stored plan")
                continue
            order = OrderRequest(
                client_order_id=row.id,
                ticker=row.ticker,
                side=Side.BUY,
                qty=Decimal(plan["qty"]),
                order_type=OrderType.LIMIT,
                limit_price=Decimal(plan["limit_price"]),
            )
            quote = self.broker.quote(row.ticker)
            decision = self.risk.evaluate(
                order, quote=quote, account=account, state=self.risk_state(), now=self.clock()
            )
            if decision.trip_kill_switch and not self.kill_switch_on():
                self.set_kill_switch(True, reason="max drawdown breached", actor="risk-engine")
            if not decision.approved:
                self._set_status(row.id, SignalStatus.FAILED, f"pre-submit risk check: {decision.summary()}")
                continue
            result = self._place(order, purpose="ENTRY", signal_id=row.id)
            if result.status is OrderStatus.REJECTED:
                self._set_status(
                    row.id,
                    SignalStatus.FAILED,
                    f"broker rejected: {result.message}",
                    order_client_id=order.client_order_id,
                )
                continue
            self._set_status(
                row.id, SignalStatus.ORDERED, result.status.value, order_client_id=order.client_order_id
            )
            executed += 1
            account = self.broker.account()
        return executed

    def _place(self, order: OrderRequest, *, purpose: str, signal_id: str | None) -> OrderResult:
        now = self.clock()
        with self.db.session() as s:
            s.add(
                OrderRow(
                    client_order_id=order.client_order_id,
                    signal_id=signal_id,
                    purpose=purpose,
                    ticker=order.ticker,
                    side=order.side.value,
                    qty=order.qty,
                    order_type=order.order_type.value,
                    limit_price=order.limit_price,
                    status=OrderStatus.OPEN.value,
                    filled_qty=Decimal("0"),
                    commission=Decimal("0"),
                    created_at=now,
                    updated_at=now,
                )
            )
        self.audit.record("order.submit", {"purpose": purpose, **order.model_dump(mode="json")}, ts=now)
        try:
            result = self.broker.place(order)
        except BrokerError as exc:  # by contract: raised only when the order was certainly not sent
            result = OrderResult(
                client_order_id=order.client_order_id,
                broker_order_id=None,
                status=OrderStatus.REJECTED,
                message=f"broker error: {exc}",
            )
        except Exception as exc:
            # The order may have reached the broker: keep it OPEN (cash committed, no second SELL)
            # and let sync() verify it, reconciling only after its session has closed.
            log.error("order %s submission outcome unknown: %s", order.client_order_id, exc)
            result = OrderResult(
                client_order_id=order.client_order_id,
                broker_order_id=None,
                status=OrderStatus.OPEN,
                message=f"submission outcome unknown, to be verified: {exc}",
            )
        self._store_order_result(result)
        self.audit.record("order.result", result.model_dump(mode="json"), ts=self.clock())
        return result

    def _store_order_result(self, result: OrderResult) -> None:
        with self.db.session() as s:
            row = s.get(OrderRow, result.client_order_id)
            if row is None:
                return
            row.status = result.status.value
            row.broker_order_id = result.broker_order_id
            row.filled_qty = result.filled_qty
            row.avg_fill_price = result.avg_fill_price
            row.commission = result.commission
            row.message = result.message
            row.updated_at = self.clock()

    # ------------------------------------------------------------------ sync
    def sync(self) -> int:
        """Refresh order statuses and import new fills. Returns number of new fills."""
        with self.db.session() as s:
            open_ids = list(
                s.scalars(
                    select(OrderRow.client_order_id).where(
                        OrderRow.status.in_([OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value])
                    )
                )
            )
        now = self.clock()
        for coid in open_ids:
            # A broker that cannot tell (disconnected, error) raises: nothing is reconciled then.
            result = self.broker.order_status(coid)
            if result is not None:
                self._store_order_result(result)
                continue
            with self.db.session() as s:
                row = s.get(OrderRow, coid)
                # All orders are DAY orders. Only once the first NYSE session in which the order could
                # trade has closed (plus a margin) does "unknown to the broker" mean "gone".
                stale = row is not None and now > first_session_close_after(row.created_at) + RECONCILE_GRACE
                if stale and row is not None:
                    # Keeping a dead order OPEN would lock cash and, for a SELL, block the exit.
                    row.status = OrderStatus.CANCELLED.value
                    row.message = "unknown to broker after its session; reconciled as cancelled"
                    row.updated_at = self.clock()
                    ticker, side = row.ticker, row.side
            if stale:
                self.audit.record(
                    "order.reconciled", {"client_order_id": coid, "status": "CANCELLED"}, ts=self.clock()
                )
                self._notify(f"ATTENZIONE: ordine {side} {ticker} non più noto al broker, chiuso d'ufficio")

        last = self.db.kv_get(LAST_FILL_KEY)
        since = datetime.fromisoformat(last) if last else None
        # Brokers can report executions late or with equal timestamps: look back one day and
        # rely on fill_id de-duplication rather than on a strict timestamp cursor.
        lookback = since - timedelta(days=1) if since else None
        new = 0
        newest = since
        for fill in sorted(self.broker.fills_since(lookback), key=lambda f: f.ts):
            if self._record_fill(fill):
                new += 1
            newest = fill.ts if newest is None or fill.ts > newest else newest
        if newest is not None and newest != since:
            self.db.kv_set(LAST_FILL_KEY, newest.isoformat())
        return new

    def _record_fill(self, fill: Fill) -> bool:
        with self.db.session() as s:
            if s.get(FillRow, fill.fill_id) is not None:
                return False
            s.add(
                FillRow(
                    fill_id=fill.fill_id,
                    client_order_id=fill.client_order_id,
                    ticker=fill.ticker,
                    side=fill.side.value,
                    qty=fill.qty,
                    price=fill.price,
                    commission=fill.commission,
                    ts=fill.ts,
                )
            )
            order = s.get(OrderRow, fill.client_order_id)
            if fill.side is Side.BUY and order is not None and order.signal_id:
                sig = s.get(SignalRow, order.signal_id)
                if sig is not None:
                    stop_pct = Decimal(
                        json.loads(sig.plan).get("entry", {}).get("stop_loss_pct", str(sig.stop_loss_pct))
                    )
                    plan = s.get(PositionPlanRow, fill.ticker)
                    stop = round_price_down(fill.price * (1 - stop_pct))
                    if plan is None:
                        s.add(
                            PositionPlanRow(
                                ticker=fill.ticker,
                                signal_id=sig.id,
                                stop_price=stop,
                                exit_after=fill.ts + timedelta(days=sig.horizon_days),
                                opened_at=fill.ts,
                            )
                        )
                    else:  # adding to a position: keep the tighter (higher) stop
                        plan.stop_price = max(plan.stop_price, stop)
        self.audit.record("fill", fill.model_dump(mode="json"), ts=self.clock())
        self._notify(
            f"Eseguito {fill.side.value} {fill.qty} {fill.ticker} @ {fill.price} (comm. {fill.commission})"
        )
        return True

    # ----------------------------------------------------------------- exits
    def manage_exits(self, account: AccountSnapshot) -> int:
        now = self.clock()
        exits = 0
        with self.db.session() as s:
            plans = {p.ticker: (p.stop_price, p.exit_after) for p in s.scalars(select(PositionPlanRow))}
            open_sells = set(
                s.scalars(
                    select(OrderRow.ticker).where(
                        OrderRow.side == Side.SELL.value,
                        OrderRow.status.in_([OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value]),
                    )
                )
            )
        for ticker, position in account.positions.items():
            if ticker not in plans or ticker in open_sells or position.qty <= 0:
                continue
            stop_price, exit_after = plans[ticker]
            quote = self.broker.quote(ticker)
            price = quote.ref_price if quote else None
            reason = None
            if price is not None and price <= stop_price:
                reason = "EXIT_STOP"
            elif now >= exit_after:
                reason = "EXIT_TIME"
            if reason is None:
                continue
            if quote is not None and quote.bid is not None and quote.bid > 0:
                limit = round_price_down(quote.bid * (1 - 2 * self.limits.limit_offset_pct))
                order = OrderRequest(ticker=ticker, side=Side.SELL, qty=position.qty, limit_price=limit)
            else:
                order = OrderRequest(
                    ticker=ticker, side=Side.SELL, qty=position.qty, order_type=OrderType.MARKET
                )
            decision = self.risk.evaluate(
                order, quote=quote, account=account, state=self.risk_state(), now=now
            )
            if not decision.approved:
                self.audit.record("exit.blocked", {"ticker": ticker, "reason": decision.summary()}, ts=now)
                self._notify(f"ATTENZIONE: uscita {ticker} bloccata: {decision.summary()}")
                continue
            result = self._place(order, purpose=reason, signal_id=None)
            if result.status is not OrderStatus.REJECTED:
                exits += 1
            self._notify(f"Uscita {reason} {ticker}: {result.status.value} {result.message}")
        self._drop_closed_plans()
        return exits

    def _drop_closed_plans(self) -> None:
        held = {t for t, p in self.broker.account().positions.items() if p.qty > 0}
        with self.db.session() as s:
            for plan in s.scalars(select(PositionPlanRow)):
                if plan.ticker not in held:
                    s.delete(plan)

    # ----------------------------------------------------------------- cycle
    def run_cycle(self) -> CycleReport:
        now = self.clock()
        report = CycleReport(started_at=now, in_session=self.session_check(now))
        report.new_fills += self.sync()
        account = self.snapshot()
        state = self.risk_state()
        if state.peak_equity and account.equity > 0:
            dd = account.equity / state.peak_equity - 1
            if dd <= -self.limits.max_drawdown_pct and not self.kill_switch_on():
                self.set_kill_switch(True, reason=f"drawdown {dd:.2%}", actor="risk-engine")
        report.expired = self.expire_stale()
        if report.in_session:
            report.exits = self.manage_exits(account)
            account = self.broker.account()
            report.reviewed, report.pending_approval = self.review_proposed(account)
            report.executed = self.execute_approved(self.broker.account())
            report.new_fills += self.sync()
            self._drop_closed_plans()
        else:
            report.notes.append("outside regular session: no reviews, entries or exits")
        self.audit.record("cycle", {k: str(v) for k, v in report.__dict__.items()}, ts=self.clock())
        return report

    # --------------------------------------------------------------- reports
    def status(self) -> dict[str, Any]:
        account = self.broker.account()
        state = self.risk_state()
        with self.db.session() as s:
            plans = {
                p.ticker: {"stop_price": str(p.stop_price), "exit_after": p.exit_after.isoformat()}
                for p in s.scalars(select(PositionPlanRow))
            }
        kill = self.db.kv_get(KILL_SWITCH_KEY, {"on": False})
        return {
            "ts": self.clock().isoformat(),
            "broker": self.broker.name,
            "approval_mode": self.approval_mode,
            "kill_switch": kill,
            "equity": str(account.equity),
            "settled_cash": str(account.settled_cash),
            "unsettled_cash": str(account.unsettled_cash),
            "positions": {
                t: {
                    "qty": str(p.qty),
                    "avg_cost": str(p.avg_cost),
                    "market_price": str(p.market_price),
                    "market_value": str(p.market_value),
                    "exit_plan": plans.get(t),
                }
                for t, p in account.positions.items()
            },
            "risk_state": {
                "orders_today": state.orders_today,
                "start_of_day_equity": str(state.start_of_day_equity),
                "peak_equity": str(state.peak_equity),
            },
        }
