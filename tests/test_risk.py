from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest

from trader.broker.commissions import IbkrTieredUS
from trader.config import RiskLimits, load_risk_limits
from trader.domain import OrderRequest, OrderType, Side, Signal
from trader.risk.engine import RiskEngine, RiskState
from trader.risk.sizing import EntryPlan, SizingRejection, plan_entry, round_price_down, round_price_up

CLEAN = RiskState(kill_switch=False, orders_today=0, start_of_day_equity=D("1000"), peak_equity=D("1000"))


def signal(**kw: object) -> Signal:
    base: dict[str, object] = {
        "source": "test",
        "ticker": "ACME",
        "horizon_days": 30,
        "conviction": D("0.8"),
        "stop_loss_pct": D("0.08"),
        "thesis": "t",
        "dedup_key": "k",
    }
    base.update(kw)
    return Signal.model_validate(base)


def engine(limits: RiskLimits) -> RiskEngine:
    return RiskEngine(limits, IbkrTieredUS())


def buy(qty: str = "2", limit: str = "50.15", ticker: str = "ACME") -> OrderRequest:
    return OrderRequest(ticker=ticker, side=Side.BUY, qty=D(qty), limit_price=D(limit))


def failed(decision: object) -> set[str]:
    return {c.name for c in decision.violations}  # type: ignore[attr-defined]


# ------------------------------------------------------------------ sizing


def test_price_rounding() -> None:
    assert round_price_up(D("50.0501")) == D("50.06")
    assert round_price_down(D("46.1399")) == D("46.13")
    assert round_price_up(D("0.50001")) == D("0.5001")


def test_plan_entry_respects_every_cap(limits, make_quote, make_account) -> None:  # type: ignore[no-untyped-def]
    plan = plan_entry(signal(), make_quote(), make_account(), limits, IbkrTieredUS())
    assert isinstance(plan, EntryPlan)
    assert plan.limit_price == D("50.16")  # ask 50.05 * 1.002 rounded up to the cent
    assert plan.stop_price < plan.limit_price
    assert plan.notional <= D("1000") * limits.max_position_pct
    assert plan.risk_amount <= D("1000") * limits.risk_per_trade_pct
    assert plan.qty == (plan.qty / limits.qty_step).to_integral_value() * limits.qty_step


def test_plan_entry_risk_budget_scales_with_conviction(limits, make_quote, make_account) -> None:  # type: ignore[no-untyped-def]
    wide_stop_low = plan_entry(
        signal(conviction=D("0.5"), stop_loss_pct=D("0.2")),
        make_quote(),
        make_account(cash="100000"),
        limits,
        IbkrTieredUS(),
    )
    wide_stop_high = plan_entry(
        signal(conviction=D("1"), stop_loss_pct=D("0.2")),
        make_quote(),
        make_account(cash="100000"),
        limits,
        IbkrTieredUS(),
    )
    assert isinstance(wide_stop_low, EntryPlan) and isinstance(wide_stop_high, EntryPlan)
    assert wide_stop_low.binding_constraint == "risk_budget"
    assert wide_stop_high.risk_amount > wide_stop_low.risk_amount


def test_plan_entry_rejects_low_conviction(limits, make_quote, make_account) -> None:  # type: ignore[no-untyped-def]
    result = plan_entry(signal(conviction=D("0.3")), make_quote(), make_account(), limits, IbkrTieredUS())
    assert isinstance(result, SizingRejection)


def test_plan_entry_whole_shares_when_fractional_disabled(make_quote, make_account) -> None:  # type: ignore[no-untyped-def]
    limits = RiskLimits(allow_fractional=False, qty_step=D("1"))
    result = plan_entry(signal(), make_quote(), make_account(), limits, IbkrTieredUS())
    assert isinstance(result, EntryPlan)
    assert result.qty == result.qty.to_integral_value()


def test_plan_entry_zero_when_no_cash(limits, make_quote, make_account) -> None:  # type: ignore[no-untyped-def]
    result = plan_entry(signal(), make_quote(), make_account(cash="5"), limits, IbkrTieredUS())
    assert isinstance(result, SizingRejection)


def test_sized_plan_passes_risk_engine(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    q = make_quote()
    acct = make_account()
    plan = plan_entry(signal(), q, acct, limits, IbkrTieredUS())
    assert isinstance(plan, EntryPlan)
    order = OrderRequest(ticker="ACME", side=Side.BUY, qty=plan.qty, limit_price=plan.limit_price)
    decision = engine(limits).evaluate(order, quote=q, account=acct, state=CLEAN, now=now)
    assert decision.approved, decision.summary()


# ----------------------------------------------------------------- entries


def test_clean_entry_approved(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    d = engine(limits).evaluate(buy(), quote=make_quote(), account=make_account(), state=CLEAN, now=now)
    assert d.approved, d.summary()
    assert not d.trip_kill_switch


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RiskState(True, 0, D("1000"), D("1000")), "kill_switch_off"),
        (RiskState(False, 20, D("1000"), D("1000")), "orders_per_day"),
        (RiskState(False, 0, D("1040"), D("1040")), "daily_loss_limit"),
    ],
)
def test_state_based_blocks(limits, make_quote, make_account, now, state, expected) -> None:  # type: ignore[no-untyped-def]
    d = engine(limits).evaluate(buy(), quote=make_quote(), account=make_account(), state=state, now=now)
    assert not d.approved
    assert expected in failed(d)


def test_drawdown_trips_kill_switch(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    state = RiskState(False, 0, D("1000"), D("1200"))  # equity 1000 vs peak 1200 = -16.7%
    d = engine(limits).evaluate(buy(), quote=make_quote(), account=make_account(), state=state, now=now)
    assert not d.approved
    assert d.trip_kill_switch
    assert "max_drawdown" in failed(d)


def test_restricted_and_whitelist(make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    lim = RiskLimits(restricted=["acme"], whitelist=["MSFT"])
    d = engine(lim).evaluate(buy(), quote=make_quote(), account=make_account(), state=CLEAN, now=now)
    assert {"not_restricted", "in_universe"} <= failed(d)


@pytest.mark.parametrize(
    ("quote_kwargs", "expected"),
    [
        ({"ts_shift": 3600}, "quote_fresh"),
        ({"bid": "49.00", "ask": "51.10"}, "max_spread"),
        ({"bid": "3.99", "ask": "4.00", "last": "4.00"}, "min_price"),
        ({"adv": None}, "liquidity_adv"),
        ({"adv": "1000"}, "liquidity_adv"),
    ],
)
def test_market_data_checks(limits, make_quote, make_account, now, quote_kwargs, expected) -> None:  # type: ignore[no-untyped-def]
    shift = quote_kwargs.pop("ts_shift", 0)
    q = make_quote(ts=now - timedelta(seconds=shift), **quote_kwargs)
    price = str(q.ask) if q.ask else "50.15"
    d = engine(limits).evaluate(
        buy(qty="1", limit=price), quote=q, account=make_account(), state=CLEAN, now=now
    )
    assert not d.approved
    assert expected in failed(d)


def test_missing_quote_rejected(limits, make_account, now) -> None:  # type: ignore[no-untyped-def]
    d = engine(limits).evaluate(buy(), quote=None, account=make_account(), state=CLEAN, now=now)
    assert not d.approved and "quote_available" in failed(d)


def test_fat_finger_price(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    d = engine(limits).evaluate(
        buy(qty="1", limit="60"), quote=make_quote(), account=make_account(), state=CLEAN, now=now
    )
    assert "price_deviation" in failed(d)


def test_position_cash_and_count_caps(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    eng = engine(limits)
    too_big = eng.evaluate(buy(qty="3"), quote=make_quote(), account=make_account(), state=CLEAN, now=now)
    assert "max_position" in failed(too_big)

    no_cash = eng.evaluate(
        buy(qty="1"), quote=make_quote(), account=make_account(cash="40"), state=CLEAN, now=now
    )
    assert "settled_cash" in failed(no_cash)

    full = {f"T{i}": ("1", "10") for i in range(10)}
    crowded = eng.evaluate(
        buy(qty="1"),
        quote=make_quote(),
        account=make_account(cash="1000", positions=full),
        state=CLEAN,
        now=now,
    )
    assert "max_positions" in failed(crowded)


def test_market_orders_and_bad_steps_rejected_for_entries(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    mkt = OrderRequest(ticker="ACME", side=Side.BUY, qty=D("1"), order_type=OrderType.MARKET)
    d = engine(limits).evaluate(mkt, quote=make_quote(), account=make_account(), state=CLEAN, now=now)
    assert "limit_order_only" in failed(d)
    odd = engine(limits).evaluate(
        buy(qty="1.00005"), quote=make_quote(), account=make_account(), state=CLEAN, now=now
    )
    assert "qty_step" in failed(odd)


def test_tiny_order_blocked_by_commission_and_min_value(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    d = engine(limits).evaluate(
        buy(qty="0.1"), quote=make_quote(), account=make_account(), state=CLEAN, now=now
    )
    assert {"min_order_value"} <= failed(d)


def test_no_adding_to_open_position_unless_enabled(make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    acct = make_account(positions={"ACME": ("1", "50")})
    blocked = engine(RiskLimits()).evaluate(
        buy(qty="0.5"), quote=make_quote(), account=acct, state=CLEAN, now=now
    )
    assert "no_add_to_position" in failed(blocked)
    lim = RiskLimits(allow_add_to_position=True)
    allowed = engine(lim).evaluate(buy(qty="0.5"), quote=make_quote(), account=acct, state=CLEAN, now=now)
    assert "no_add_to_position" not in failed(allowed)


# ------------------------------------------------------------------- exits


def test_exit_allowed_under_kill_switch_and_restriction(make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    lim = RiskLimits(restricted=["ACME"])
    acct = make_account(positions={"ACME": ("2", "50")})
    sell = OrderRequest(ticker="ACME", side=Side.SELL, qty=D("2"), order_type=OrderType.MARKET)
    state = RiskState(True, 99, D("2000"), D("5000"))
    d = engine(lim).evaluate(sell, quote=None, account=acct, state=state, now=now)
    assert d.approved, d.summary()


def test_no_short_selling(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    acct = make_account(positions={"ACME": ("1", "50")})
    sell = OrderRequest(ticker="ACME", side=Side.SELL, qty=D("1.5"), limit_price=D("49.9"))
    d = engine(limits).evaluate(sell, quote=make_quote(), account=acct, state=CLEAN, now=now)
    assert "long_only_no_short" in failed(d)
    naked = OrderRequest(ticker="ZZZ", side=Side.SELL, qty=D("1"), limit_price=D("10"))
    assert not engine(limits).evaluate(naked, quote=None, account=acct, state=CLEAN, now=now).approved


def test_exit_fat_finger_limit(limits, make_quote, make_account, now) -> None:  # type: ignore[no-untyped-def]
    acct = make_account(positions={"ACME": ("1", "50")})
    sell = OrderRequest(ticker="ACME", side=Side.SELL, qty=D("1"), limit_price=D("5"))
    d = engine(limits).evaluate(sell, quote=make_quote(), account=acct, state=CLEAN, now=now)
    assert "exit_price_sanity" in failed(d)


# ------------------------------------------------------------------ config


def test_limits_validation_rejects_leverage_and_bad_fractions() -> None:
    with pytest.raises(ValueError):
        RiskLimits(max_gross_exposure_pct=D("1.5"))
    with pytest.raises(ValueError):
        RiskLimits(max_position_pct=D("2"))
    with pytest.raises(ValueError):
        RiskLimits(allow_fractional=False, qty_step=D("0.01"))


def test_load_risk_limits_with_restricted_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = tmp_path / "risk.yaml"
    cfg.write_text("max_position_pct: 0.1\nrestricted: [aaa]\n")
    rl = tmp_path / "restricted.txt"
    rl.write_text("# private\nbbb  # comment\n\nccc\n")
    limits = load_risk_limits(cfg, rl)
    assert limits.max_position_pct == D("0.1")
    assert limits.restricted == ("AAA", "BBB", "CCC")


def test_commission_model() -> None:
    m = IbkrTieredUS()
    assert m.estimate(D("1"), D("50")) == D("0.3530")  # min 0.35 + 0.003 third-party
    assert m.estimate(D("0.1"), D("50")) == D("0.0503")  # 1% cap (0.05) below the minimum
    assert m.estimate(D("1000"), D("50")) == D("6.5000")
