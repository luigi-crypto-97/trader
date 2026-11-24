"""Interactive Brokers adapter (via ``ib_async``) for IB Gateway / TWS.

Ports: IB Gateway paper 4002, live 4001; TWS paper 7497, live 7496. Run the gateway on the same
host (or a private network) and keep the API in read-write mode only where orders are intended.

Notes verified against the ib_async data model; behaviour against a live gateway must be validated
on the PAPER account before any real-money use:
* Settled cash comes from the ``SettledCash`` account value (USD row, else BASE row if the base
  currency is USD); when absent the adapter falls back to the lower of TotalCashValue and
  AvailableFunds in USD (conservative).
* Executions are imported only once IBKR has sent their commission report, so the tax ledger
  never sees a fill with a missing commission; the pipeline re-polls with a look-back window.
* Share classes: SEC tickers use '-', IBKR uses a space (BRK-B <-> "BRK B").
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from trader.broker.base import BrokerError
from trader.domain import (
    AccountSnapshot,
    Fill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)

log = logging.getLogger(__name__)

UNSET_DOUBLE = 1.7976931348623157e308
_STATUS = {
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
}


def to_ib_symbol(ticker: str) -> str:
    return ticker.upper().replace("-", " ")


def from_ib_symbol(symbol: str) -> str:
    return symbol.upper().replace(" ", "-")


def _num(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f <= 0 or f >= UNSET_DOUBLE:
        return None
    return Decimal(str(x))


def _commission(x: Any) -> Decimal | None:
    """Commission may legitimately be 0; NaN / UNSET_DOUBLE mean 'not reported yet'."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or abs(f) >= UNSET_DOUBLE:
        return None
    return abs(Decimal(str(x)))


class IbkrBroker:
    name = "ibkr"

    def __init__(
        self,
        ib: Any,
        *,
        account: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        settle_wait: float = 1.0,
    ) -> None:
        self.ib = ib
        self.account_id = account or ""
        self.clock = clock
        self.settle_wait = settle_wait
        self._contracts: dict[str, Any] = {}
        self._adv: dict[tuple[str, date], Decimal | None] = {}

    @classmethod
    def connect(
        cls, host: str, port: int, client_id: int, account: str | None, market_data_type: int = 1
    ) -> IbkrBroker:
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BrokerError("ib_async not installed: uv sync --extra ibkr") from exc
        ib = IB()
        ib.connect(host, port, clientId=client_id, account=account or "", timeout=15)
        ib.reqMarketDataType(market_data_type)
        return cls(ib, account=account)

    def close(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    # ------------------------------------------------------------- contracts
    def _contract(self, ticker: str) -> Any:
        key = ticker.upper()
        if key not in self._contracts:
            from ib_async import Stock

            qualified = self.ib.qualifyContracts(Stock(to_ib_symbol(key), "SMART", "USD"))
            if not qualified or qualified[0] is None:
                raise BrokerError(f"cannot qualify contract for {key}")
            self._contracts[key] = qualified[0]
        return self._contracts[key]

    # --------------------------------------------------------------- account
    def _account_values(self) -> dict[tuple[str, str], Decimal]:
        out: dict[tuple[str, str], Decimal] = {}
        for v in self.ib.accountValues(self.account_id):
            try:
                out[(v.tag, v.currency)] = Decimal(str(v.value))
            except (ArithmeticError, ValueError):
                continue
        return out

    def account(self) -> AccountSnapshot:
        vals = self._account_values()
        total_usd = vals.get(("TotalCashValue", "USD"), Decimal("0"))
        settled = vals.get(("SettledCash", "USD"))
        if settled is None and vals.get(("TotalCashValue", "BASE")) == total_usd:
            settled = vals.get(("SettledCash", "BASE"))
        if settled is None:
            avail = vals.get(("AvailableFunds", "USD"))
            settled = min(total_usd, avail) if avail is not None else total_usd
        settled = max(settled, Decimal("0"))
        positions: dict[str, Position] = {}
        for item in self.ib.portfolio(self.account_id):
            c = item.contract
            if getattr(c, "secType", "STK") != "STK" or getattr(c, "currency", "USD") != "USD":
                continue
            qty = Decimal(str(item.position))
            if qty == 0:
                continue
            ticker = from_ib_symbol(c.symbol)
            positions[ticker] = Position(
                ticker=ticker,
                qty=qty,
                avg_cost=Decimal(str(item.averageCost)),
                market_price=_num(item.marketPrice),
            )
        return AccountSnapshot(
            ts=self.clock(),
            settled_cash=settled,
            unsettled_cash=max(total_usd - settled, Decimal("0")),
            positions=positions,
        )

    # ----------------------------------------------------------- market data
    def _average_dollar_volume(self, ticker: str, contract: Any) -> Decimal | None:
        key = (ticker, self.clock().date())
        if key not in self._adv:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="30 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
            )
            values = [
                Decimal(str(b.close)) * Decimal(str(b.volume)) for b in bars or [] if b.volume and b.close
            ]
            self._adv[key] = (sum(values, Decimal("0")) / len(values)) if values else None
        return self._adv[key]

    def quote(self, ticker: str) -> Quote | None:
        try:
            contract = self._contract(ticker)
        except BrokerError as exc:
            log.warning("%s", exc)
            return None
        tickers = self.ib.reqTickers(contract)
        if not tickers:
            return None
        t = tickers[0]
        ts = t.time if isinstance(t.time, datetime) and t.time.tzinfo else self.clock()
        bid, ask, last = _num(t.bid), _num(t.ask), _num(t.last) or _num(t.close)
        if bid is None and ask is None and last is None:
            return None
        return Quote(
            ticker=ticker.upper(),
            ts=ts.astimezone(UTC),
            bid=bid,
            ask=ask,
            last=last,
            avg_daily_dollar_volume=self._average_dollar_volume(ticker.upper(), contract),
        )

    # ---------------------------------------------------------------- orders
    def _find_trade(self, client_order_id: str) -> Any | None:
        for trade in self.ib.trades():
            if getattr(trade.order, "orderRef", "") == client_order_id:
                return trade
        return None

    def _result(self, client_order_id: str, trade: Any, message: str = "") -> OrderResult:
        st = trade.orderStatus
        filled = Decimal(str(st.filled or 0))
        status = _STATUS.get(st.status)
        if status is None:
            status = OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.OPEN
        commission = Decimal("0")
        for f in getattr(trade, "fills", []) or []:
            c = _commission(getattr(f.commissionReport, "commission", None))
            commission += c or Decimal("0")
        log_msg = trade.log[-1].message if getattr(trade, "log", None) else ""
        return OrderResult(
            client_order_id=client_order_id,
            broker_order_id=str(trade.order.permId or trade.order.orderId or ""),
            status=status,
            filled_qty=filled,
            avg_fill_price=_num(st.avgFillPrice),
            commission=commission,
            message=(message or log_msg or st.status)[:500],
        )

    def place(self, order: OrderRequest) -> OrderResult:
        existing = self._find_trade(order.client_order_id)
        if existing is not None:  # idempotent resubmission
            return self._result(order.client_order_id, existing, "already submitted")
        from ib_async import LimitOrder, MarketOrder

        contract = self._contract(order.ticker)
        action = "BUY" if order.side is Side.BUY else "SELL"
        ib_order: Any
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            ib_order = LimitOrder(action, float(order.qty), float(order.limit_price))
        else:
            ib_order = MarketOrder(action, float(order.qty))
        ib_order.orderRef = order.client_order_id
        ib_order.tif = order.tif
        ib_order.outsideRth = False
        if self.account_id:
            ib_order.account = self.account_id
        trade = self.ib.placeOrder(contract, ib_order)
        self.ib.sleep(self.settle_wait)
        return self._result(order.client_order_id, trade)

    def cancel(self, client_order_id: str) -> OrderResult | None:
        trade = self._find_trade(client_order_id)
        if trade is None:
            return None
        if trade.orderStatus.status not in _STATUS:
            self.ib.cancelOrder(trade.order)
            self.ib.sleep(self.settle_wait)
        return self._result(client_order_id, trade)

    def order_status(self, client_order_id: str) -> OrderResult | None:
        trade = self._find_trade(client_order_id)
        return self._result(client_order_id, trade) if trade is not None else None

    def fills_since(self, since: datetime | None) -> list[Fill]:
        out: list[Fill] = []
        for f in self.ib.fills():
            ex = f.execution
            ts = f.time if isinstance(f.time, datetime) else ex.time
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if since is not None and ts < since:
                continue
            commission = _commission(getattr(f.commissionReport, "commission", None))
            if commission is None:
                continue  # commission report not received yet: import on a later sync
            out.append(
                Fill(
                    fill_id=ex.execId,
                    client_order_id=ex.orderRef or f"ib-{ex.orderId}",
                    ticker=from_ib_symbol(f.contract.symbol),
                    side=Side.BUY if ex.side == "BOT" else Side.SELL,
                    qty=Decimal(str(ex.shares)),
                    price=Decimal(str(ex.price)),
                    commission=commission,
                    ts=ts.astimezone(UTC),
                )
            )
        return out
