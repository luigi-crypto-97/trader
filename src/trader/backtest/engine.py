"""Daily-bar event backtester that reuses the production sizing and risk engine.

Timing rules (conservative, no look-ahead):
* a signal is usable only if it was *available* (SEC acceptance time) before the session open of
  the entry day; entry happens at that day's open, plus adverse slippage;
* stop-loss: if a bar's low touches the stop, exit at the stop, or at the open if the bar gaps
  below it;
* time exit: at the open of the first session on/after ``entry + horizon_days``;
* equity is marked at the close; commissions use the same model as live trading.

Simplifications (documented, not hidden): no T+1 settlement (cash is reusable the same day),
no partial fills, no corporate actions beyond what the price file reflects (use adjusted data),
holidays come from the benchmark's calendar.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from trader.broker.base import NEW_YORK
from trader.broker.commissions import CommissionModel, IbkrTieredUS
from trader.config import RiskLimits
from trader.domain import AccountSnapshot, OrderRequest, Position, Quote, Side, Signal
from trader.risk.engine import RiskEngine, RiskState
from trader.risk.sizing import EntryPlan, plan_entry

BPS = Decimal("0.0001")
OPEN_TIME = time(9, 30)


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


PriceData = dict[str, dict[date, Bar]]


def load_csv_dir(directory: Path, tickers: Iterable[str] | None = None) -> PriceData:
    """Load ``<TICKER>.csv`` files with columns date,open,high,low,close,volume (adjusted prices)."""
    wanted = {t.upper() for t in tickers} if tickers else None
    data: PriceData = {}
    for path in sorted(directory.glob("*.csv")):
        ticker = path.stem.upper()
        if wanted is not None and ticker not in wanted:
            continue
        bars: dict[date, Bar] = {}
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                r = {k.strip().lower(): v for k, v in row.items()}
                d = date.fromisoformat(r["date"][:10])
                bars[d] = Bar(
                    d,
                    Decimal(r["open"]),
                    Decimal(r["high"]),
                    Decimal(r["low"]),
                    Decimal(r["close"]),
                    Decimal(r.get("volume") or "0"),
                )
        data[ticker] = bars
    return data


@dataclass(frozen=True)
class TimedSignal:
    signal: Signal
    available_at: datetime


@dataclass
class _Open:
    ticker: str
    qty: Decimal
    entry_day: date
    entry_price: Decimal
    entry_cost: Decimal
    stop: Decimal
    exit_after: date
    source: str


@dataclass(frozen=True)
class Trade:
    ticker: str
    source: str
    entry_day: date
    exit_day: date
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    reason: str

    @property
    def return_pct(self) -> Decimal:
        cost = self.qty * self.entry_price
        return self.pnl / cost if cost else Decimal("0")


@dataclass
class BacktestResult:
    equity_curve: list[tuple[date, Decimal]]
    trades: list[Trade]
    rejected: list[tuple[date, str, str]]
    benchmark_curve: list[tuple[date, Decimal]] = field(default_factory=list)

    def metrics(self) -> dict[str, float]:
        eq = [float(v) for _, v in self.equity_curve]
        if len(eq) < 2:
            return {}
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
        vol = math.sqrt(var) * math.sqrt(252)
        years = max((self.equity_curve[-1][0] - self.equity_curve[0][0]).days / 365.25, 1 / 365.25)
        total = eq[-1] / eq[0] - 1
        peak, mdd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        wins = [t for t in self.trades if t.pnl > 0]
        out = {
            "total_return": total,
            "cagr": (eq[-1] / eq[0]) ** (1 / years) - 1 if eq[0] > 0 and eq[-1] > 0 else -1.0,
            "volatility": vol,
            "sharpe": (mean * 252) / vol if vol > 0 else 0.0,
            "max_drawdown": mdd,
            "trades": float(len(self.trades)),
            "win_rate": len(wins) / len(self.trades) if self.trades else 0.0,
            "avg_trade_return": (sum(float(t.return_pct) for t in self.trades) / len(self.trades))
            if self.trades
            else 0.0,
        }
        if len(self.benchmark_curve) >= 2:
            b0, b1 = float(self.benchmark_curve[0][1]), float(self.benchmark_curve[-1][1])
            out["benchmark_return"] = b1 / b0 - 1
            out["excess_return"] = total - out["benchmark_return"]
        return out


class Backtester:
    def __init__(
        self,
        prices: PriceData,
        *,
        limits: RiskLimits | None = None,
        commissions: CommissionModel | None = None,
        initial_cash: Decimal = Decimal("1000"),
        slippage_bps: Decimal = Decimal("10"),
        benchmark: str = "SPY",
        adv_window: int = 20,
    ) -> None:
        self.prices = {t.upper(): bars for t, bars in prices.items()}
        self.limits = limits or RiskLimits()
        self.commissions = commissions or IbkrTieredUS()
        self.risk = RiskEngine(self.limits, self.commissions)
        self.initial_cash = initial_cash
        self.slip = slippage_bps * BPS
        self.benchmark = benchmark.upper()
        self.adv_window = adv_window

    def _calendar(self, start: date, end: date) -> list[date]:
        source = self.prices.get(self.benchmark)
        days = set(source) if source else {d for bars in self.prices.values() for d in bars}
        return sorted(d for d in days if start <= d <= end)

    def _adv(self, ticker: str, day: date) -> Decimal | None:
        bars = self.prices.get(ticker, {})
        past = sorted(d for d in bars if d < day)[-self.adv_window :]
        if len(past) < self.adv_window // 2:
            return None
        return sum((bars[d].close * bars[d].volume for d in past), Decimal("0")) / len(past)

    def run(self, signals: Iterable[TimedSignal], start: date, end: date) -> BacktestResult:
        calendar = self._calendar(start, end)
        queue = sorted(signals, key=lambda s: s.available_at)
        cash = self.initial_cash
        positions: dict[str, _Open] = {}
        trades: list[Trade] = []
        rejected: list[tuple[date, str, str]] = []
        curve: list[tuple[date, Decimal]] = []
        bench: list[tuple[date, Decimal]] = []
        peak = self.initial_cash
        used: set[str] = set()
        qi = 0

        def mark(day: date, field_name: str = "close") -> Decimal:
            total = cash
            for p in positions.values():
                bar = self.prices[p.ticker].get(day)
                px = getattr(bar, field_name) if bar else p.entry_price
                total += p.qty * px
            return total

        def close_position(p: _Open, day: date, price: Decimal, reason: str) -> None:
            nonlocal cash
            fill = price * (1 - self.slip)
            commission = self.commissions.estimate(p.qty, fill)
            proceeds = p.qty * fill - commission
            cash += proceeds
            trades.append(
                Trade(
                    p.ticker,
                    p.source,
                    p.entry_day,
                    day,
                    p.qty,
                    p.entry_price,
                    fill,
                    proceeds - p.entry_cost,
                    reason,
                )
            )
            del positions[p.ticker]

        for day in calendar:
            open_dt = datetime.combine(day, OPEN_TIME, tzinfo=NEW_YORK).astimezone(UTC)
            # 1) time exits at the open
            for p in list(positions.values()):
                bar = self.prices[p.ticker].get(day)
                if bar and day >= p.exit_after:
                    close_position(p, day, bar.open, "time")
            # 2) entries at the open for signals available before the open
            start_equity = mark(day, "open")
            while qi < len(queue) and queue[qi].available_at < open_dt:
                ts = queue[qi]
                qi += 1
                sig = ts.signal
                if sig.dedup_key in used:
                    continue
                used.add(sig.dedup_key)
                bar = self.prices.get(sig.ticker, {}).get(day)
                if bar is None:
                    rejected.append((day, sig.ticker, "no price data"))
                    continue
                if sig.ticker in positions:
                    rejected.append((day, sig.ticker, "already in position"))
                    continue
                quote = Quote(
                    ticker=sig.ticker,
                    ts=open_dt,
                    bid=bar.open,
                    ask=bar.open,
                    last=bar.open,
                    avg_daily_dollar_volume=self._adv(sig.ticker, day),
                )
                account = AccountSnapshot(
                    ts=open_dt,
                    settled_cash=cash,
                    positions={
                        t: Position(
                            ticker=t,
                            qty=p.qty,
                            avg_cost=p.entry_price,
                            market_price=self.prices[t][day].open if day in self.prices[t] else p.entry_price,
                        )
                        for t, p in positions.items()
                    },
                )
                plan = plan_entry(sig, quote, account, self.limits, self.commissions)
                if not isinstance(plan, EntryPlan):
                    rejected.append((day, sig.ticker, plan.reason))
                    continue
                order = OrderRequest(
                    ticker=sig.ticker, side=Side.BUY, qty=plan.qty, limit_price=plan.limit_price
                )
                state = RiskState(
                    kill_switch=False, orders_today=0, start_of_day_equity=start_equity, peak_equity=peak
                )
                decision = self.risk.evaluate(order, quote=quote, account=account, state=state, now=open_dt)
                if not decision.approved:
                    rejected.append((day, sig.ticker, decision.summary()))
                    continue
                fill = min(bar.open * (1 + self.slip), plan.limit_price)
                commission = self.commissions.estimate(plan.qty, fill)
                cost = plan.qty * fill + commission
                if cost > cash:
                    rejected.append((day, sig.ticker, "insufficient cash"))
                    continue
                cash -= cost
                positions[sig.ticker] = _Open(
                    sig.ticker,
                    plan.qty,
                    day,
                    fill,
                    cost,
                    fill * (1 - plan.stop_loss_pct),
                    day + timedelta(days=sig.horizon_days),
                    sig.source,
                )
            # 3) intraday stops (not on the entry bar's open, which was the entry price)
            for p in list(positions.values()):
                bar = self.prices[p.ticker].get(day)
                if bar and bar.low <= p.stop:
                    close_position(p, day, min(bar.open, p.stop) if day != p.entry_day else p.stop, "stop")
            equity = mark(day)
            peak = max(peak, equity)
            curve.append((day, equity))
            b = self.prices.get(self.benchmark, {}).get(day)
            if b:
                bench.append((day, b.close))
        if calendar:
            last = calendar[-1]
            for p in list(positions.values()):
                bar = self.prices[p.ticker].get(last)
                close_position(p, last, bar.close if bar else p.entry_price, "end")
        return BacktestResult(curve, trades, rejected, bench)


def replay_signals(generator: Callable[[datetime], list[Signal]], days: Iterable[date]) -> list[TimedSignal]:
    """Run a production signal generator as of each day's 16:00 New York close.

    The signal becomes available at that time; the backtester enters at the next session open.
    """
    out: list[TimedSignal] = []
    for d in days:
        as_of = datetime.combine(d, time(16, 0), tzinfo=NEW_YORK).astimezone(UTC)
        out.extend(TimedSignal(s, as_of) for s in generator(as_of))
    return out
