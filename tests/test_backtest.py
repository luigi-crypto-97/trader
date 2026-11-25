from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from trader.backtest.engine import Backtester, Bar, PriceData, TimedSignal, load_csv_dir, replay_signals
from trader.broker.base import NEW_YORK
from trader.domain import Signal


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = business_days(date(2026, 1, 5), 60)


def series(prices: list[float], days: list[date] = DAYS, low_factor: float = 0.995) -> dict[date, Bar]:
    return {
        d: Bar(
            d,
            D(str(p)),
            D(str(round(p * 1.005, 4))),
            D(str(round(p * low_factor, 4))),
            D(str(p)),
            D("2000000"),
        )
        for d, p in zip(days, prices, strict=True)
    }


def sig(ticker: str = "ACME", horizon: int = 10, key: str = "k") -> Signal:
    return Signal(
        source="bt",
        ticker=ticker,
        horizon_days=horizon,
        conviction=D("0.9"),
        stop_loss_pct=D("0.08"),
        thesis="t",
        dedup_key=key,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


def prices_with(acme: list[float]) -> PriceData:
    return {"SPY": series([500.0 + i for i in range(60)]), "ACME": series(acme)}


def test_profitable_time_exit_and_no_lookahead() -> None:
    acme = [50.0] * 25 + [50.0 * (1.01**i) for i in range(35)]
    bt = Backtester(prices_with(acme), slippage_bps=D("0"))
    signal_day = DAYS[24]
    result = bt.run([TimedSignal(sig(), at(signal_day, 10))], DAYS[0], DAYS[-1])
    (trade,) = result.trades
    assert trade.entry_day == DAYS[25]  # announced intraday on day 24 -> next open, never same day
    assert trade.reason == "time" and trade.pnl > 0
    m = result.metrics()
    assert m["trades"] == 1 and m["win_rate"] == 1.0 and m["total_return"] > 0
    assert "benchmark_return" in m


def test_stop_loss_gap_down_exits_at_open() -> None:
    acme = [50.0] * 26 + [40.0] * 34
    bt = Backtester(prices_with(acme), slippage_bps=D("0"))
    result = bt.run([TimedSignal(sig(), at(DAYS[24], 17))], DAYS[0], DAYS[-1])
    (trade,) = result.trades
    assert trade.reason == "stop" and trade.exit_day == DAYS[26]
    assert trade.exit_price == D("40.0")  # gapped below the stop: filled at the open
    assert result.metrics()["max_drawdown"] < 0


def test_risk_engine_applies_in_backtest() -> None:
    thin = {d: Bar(d, D("50"), D("50"), D("50"), D("50"), D("10")) for d in DAYS}
    bt = Backtester({"SPY": series([500.0] * 60), "THIN": thin})
    result = bt.run([TimedSignal(sig("THIN"), at(DAYS[30], 9))], DAYS[0], DAYS[-1])
    assert not result.trades and "liquidity_adv" in result.rejected[0][2]


def test_replay_and_csv_loader(tmp_path: Path) -> None:
    (tmp_path / "acme.csv").write_text("date,open,high,low,close,volume\n2026-01-05,1,2,0.5,1.5,100\n")
    data = load_csv_dir(tmp_path)
    assert data["ACME"][date(2026, 1, 5)].close == D("1.5")
    seen: list[datetime] = []

    def gen(now: datetime) -> list[Signal]:
        seen.append(now)
        return [sig(key=f"k{len(seen)}")]

    timed = replay_signals(gen, DAYS[:2])
    assert [t.available_at for t in timed] == [at(DAYS[0], 16), at(DAYS[1], 16)]


def test_split_during_holding_is_applied_like_a_broker() -> None:
    from trader.marketdata import Split

    # Unadjusted prices: a 2-for-1 split on day 30 halves the price; economic value is unchanged.
    acme = [50.0] * 30 + [25.0] * 30
    bt = Backtester(
        prices_with(acme), slippage_bps=D("0"), splits={"ACME": [Split("ACME", DAYS[30], D("1"), D("2"))]}
    )
    result = bt.run([TimedSignal(sig(horizon=20), at(DAYS[24], 17))], DAYS[0], DAYS[-1])
    (trade,) = result.trades
    assert trade.reason == "time"  # no false stop-loss on the split day
    assert abs(trade.pnl) < D("1")  # only commissions, no fake -50%
    without = Backtester(prices_with(acme), slippage_bps=D("0"))
    (naive,) = without.run([TimedSignal(sig(horizon=20), at(DAYS[24], 17))], DAYS[0], DAYS[-1]).trades
    assert naive.reason == "stop"  # what goes wrong when splits are ignored
