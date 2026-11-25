from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trader.config import Settings
from trader.db import Database, InsiderTransactionRow
from trader.mcp_server import Lab, build_server


def settings(tmp_path: Path, **kw: Any) -> Settings:
    risk = tmp_path / "risk.yaml"
    risk.write_text("max_position_pct: 0.12\n")
    restricted = tmp_path / "restricted.txt"
    restricted.write_text("SECRT\n")
    return Settings(
        db_url=f"sqlite:///{tmp_path / 'lab.sqlite'}",
        risk_config_path=risk,
        restricted_list_path=restricted,
        _env_file=None,
        **kw,
    )  # type: ignore[call-arg]


def call(server: Any, name: str, args: dict[str, Any] | None = None) -> Any:
    result = asyncio.run(server.call_tool(name, args or {}))
    assert not result.is_error, result
    if isinstance(result.structured_content, dict) and set(result.structured_content) == {"result"}:
        return result.structured_content["result"]  # list results
    return json.loads(result.content[0].text)


def test_mcp_tool_surface_has_no_order_tools(tmp_path: Path) -> None:
    server = build_server(Lab(settings(tmp_path)))
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "get_status",
        "list_signals",
        "get_signal",
        "insider_activity",
        "recent_filings",
        "filing_text",
        "insider_clusters",
        "propose_signal",
    }
    assert not any(w in n for n in names for w in ("order", "buy", "sell", "approve", "kill"))


def test_mcp_status_hides_restricted_list(tmp_path: Path) -> None:
    server = build_server(Lab(settings(tmp_path)))
    status = call(server, "get_status")
    assert status["risk_limits"]["restricted_count"] == 1
    assert status["audit_ok"] is True
    assert "SECRT" not in json.dumps(status)


def test_mcp_propose_signal_queues_only(tmp_path: Path) -> None:
    lab = Lab(settings(tmp_path))
    server = build_server(lab)
    out = call(
        server,
        "propose_signal",
        {
            "ticker": "acme",
            "thesis": "t",
            "horizon_days": 20,
            "conviction": 0.7,
            "stop_loss_pct": 0.08,
            "evidence": ["https://www.sec.gov/Archives/x"],
        },
    )
    assert out["queued"] is True
    again = call(
        server,
        "propose_signal",
        {
            "ticker": "ACME",
            "thesis": "t",
            "horizon_days": 20,
            "conviction": 0.7,
            "stop_loss_pct": 0.08,
            "evidence": ["x"],
        },
    )
    assert again == {
        "queued": False,
        "signal_id": None,
        "note": "not queued: already proposed today or not tradable",
    }
    (sig,) = call(server, "list_signals", {"status": "PROPOSED"})
    assert sig["ticker"] == "ACME" and sig["source"] == "mcp_research"
    blocked = call(
        server,
        "propose_signal",
        {
            "ticker": "SECRT",
            "thesis": "t",
            "horizon_days": 20,
            "conviction": 0.7,
            "stop_loss_pct": 0.08,
            "evidence": ["x"],
        },
    )
    assert blocked == again  # restricted and duplicate are indistinguishable
    bad = call(
        server,
        "propose_signal",
        {
            "ticker": "ACME2",
            "thesis": "t",
            "horizon_days": 20,
            "conviction": 1.7,
            "stop_loss_pct": 0.08,
            "evidence": ["x"],
        },
    )
    assert bad["queued"] is False
    assert lab.audit.verify().ok


def test_mcp_insider_activity(tmp_path: Path) -> None:
    lab = Lab(settings(tmp_path))
    now = datetime.now(UTC)
    with lab.db.session() as s:
        s.add(
            InsiderTransactionRow(
                accession="a",
                seq=0,
                issuer_cik=1,
                ticker="ACME",
                owner_cik=2,
                owner_name="Jane",
                is_director=True,
                is_officer=False,
                is_ten_pct_owner=False,
                officer_title="",
                transaction_date=now.date(),
                code="P",
                acquired_disposed="A",
                shares=D("10"),
                price=D("20"),
                shares_owned_after=None,
                direct=True,
                is_10b5_1=False,
                accepted_at=now - timedelta(days=1),
                ingested_at=now,
            )
        )
    rows = call(build_server(lab), "insider_activity", {"ticker": "acme"})
    assert rows[0]["owner"] == "Jane" and rows[0]["price"] == "20"


# --------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    s = settings(tmp_path)
    monkeypatch.setenv("TRADER_DB_URL", s.db_url)
    monkeypatch.setenv("TRADER_RISK_CONFIG_PATH", str(s.risk_config_path))
    monkeypatch.setenv("TRADER_RESTRICTED_LIST_PATH", str(s.restricted_list_path))
    monkeypatch.setenv("TRADER_BROKER", "sim")
    monkeypatch.chdir(tmp_path)  # no stray .env
    return tmp_path


def test_cli_status_kill_and_audit(cli_env: Path) -> None:
    from trader.cli import app

    runner = CliRunner()
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["equity"] == "1000"
    assert runner.invoke(app, ["kill", "on", "--reason", "test"]).exit_code == 0
    assert json.loads(runner.invoke(app, ["status"]).output)["kill_switch"]["on"] is True
    res = runner.invoke(app, ["audit", "verify"])
    assert res.exit_code == 0 and json.loads(res.output)["ok"] is True
    doctor = json.loads(runner.invoke(app, ["doctor"]).output)
    assert "restricted list: 1" in doctor["risk_config"]


def test_cli_sim_quote_persists(cli_env: Path) -> None:
    from trader.cli import app

    runner = CliRunner()
    assert runner.invoke(app, ["sim", "quote", "ACME", "50"]).exit_code == 0
    db = Database(f"sqlite:///{cli_env / 'lab.sqlite'}")
    assert "ACME" in db.kv_get("sim_broker")["quotes"]


def test_restricted_never_visible_through_mcp(tmp_path: Path) -> None:
    from trader.audit import AuditLog
    from trader.broker.commissions import IbkrTieredUS
    from trader.broker.simulated import SimulatedBroker
    from trader.domain import Signal
    from trader.pipeline import TradingSystem

    st = settings(tmp_path)
    lab = Lab(st)
    server = build_server(lab)
    system = TradingSystem(
        db=lab.db,
        broker=SimulatedBroker(settled_cash=D("1000")),
        limits=st.risk_limits(),
        commissions=IbkrTieredUS(),
        audit=AuditLog(lab.db),
    )
    quant = Signal(
        source="insider_cluster",
        ticker="SECRT",
        horizon_days=30,
        conviction=D("0.8"),
        stop_loss_pct=D("0.1"),
        thesis="t",
        dedup_key="q1",
    )
    system.submit_signal(quant)
    system.run_cycle()
    call(
        server,
        "propose_signal",
        {
            "ticker": "secrt",
            "thesis": "t",
            "horizon_days": 20,
            "conviction": 0.7,
            "stop_loss_pct": 0.08,
            "evidence": ["x"],
        },
    )
    exposed = json.dumps([call(server, "list_signals"), call(server, "get_status")])
    assert "SECRT" not in exposed.upper()


def test_mcp_daily_proposal_cap(tmp_path: Path) -> None:
    from trader.mcp_server import MAX_PROPOSALS_PER_DAY

    server = build_server(Lab(settings(tmp_path)))
    for i in range(MAX_PROPOSALS_PER_DAY):
        call(
            server,
            "propose_signal",
            {
                "ticker": f"T{i}",
                "thesis": "t",
                "horizon_days": 5,
                "conviction": 0.6,
                "stop_loss_pct": 0.05,
                "evidence": ["x"],
            },
        )
    over = call(
        server,
        "propose_signal",
        {
            "ticker": "LAST",
            "thesis": "t",
            "horizon_days": 5,
            "conviction": 0.6,
            "stop_loss_pct": 0.05,
            "evidence": ["x"],
        },
    )
    assert over == {"queued": False, "error": "daily proposal limit reached"}


def test_cli_public_rejections_hide_private_rule() -> None:
    from datetime import date

    from trader.cli import public_rejections

    rej = [
        (date(2025, 1, 2), "AAA", "private rule; min_price: x"),
        (date(2025, 1, 2), "BBB", "liquidity_adv: x"),
    ]
    assert public_rejections(rej) == [rej[1]]


def test_domain_normalizes_tickers() -> None:
    from trader.domain import Signal, normalize_ticker

    assert normalize_ticker(" brk b ") == normalize_ticker("BRK.B") == normalize_ticker("BRK/B") == "BRK-B"
    s = Signal(
        source="t",
        ticker="brk.b",
        horizon_days=5,
        conviction=D("0.5"),
        stop_loss_pct=D("0.1"),
        thesis="t",
        dedup_key="d",
    )
    assert s.ticker == "BRK-B"


def test_mcp_view_identical_for_duplicate_and_restricted(tmp_path: Path) -> None:
    server = build_server(Lab(settings(tmp_path)))
    args = {"thesis": "t", "horizon_days": 20, "conviction": 0.7, "stop_loss_pct": 0.08, "evidence": ["x"]}
    call(server, "propose_signal", {"ticker": "DUPL", **args})
    before = [call(server, "list_signals"), call(server, "get_status")]
    dup = call(server, "propose_signal", {"ticker": "DUPL", **args})
    after_dup = [call(server, "list_signals"), call(server, "get_status")]
    restricted = call(server, "propose_signal", {"ticker": "SECRT", **args})
    after_restricted = [call(server, "list_signals"), call(server, "get_status")]
    assert dup == restricted
    assert before == after_dup == after_restricted


def test_mcp_hides_records_of_signals_dropped_at_execution(tmp_path: Path) -> None:
    from trader.audit import AuditLog
    from trader.broker.commissions import IbkrTieredUS
    from trader.broker.simulated import SimulatedBroker
    from trader.config import RiskLimits
    from trader.domain import Quote, Signal
    from trader.pipeline import TradingSystem
    from trader.risk.engine import RiskEngine

    lab = Lab(settings(tmp_path))
    now = datetime(2026, 11, 24, 15, tzinfo=UTC)
    broker = SimulatedBroker(settled_cash=D("1000"), clock=lambda: now)
    broker.set_quote(
        Quote(
            ticker="LATER",
            ts=now,
            bid=D("49.99"),
            ask=D("50.01"),
            last=D("50"),
            avg_daily_dollar_volume=D("100000000"),
        )
    )
    system = TradingSystem(
        db=lab.db,
        broker=broker,
        limits=RiskLimits(),
        commissions=IbkrTieredUS(),
        audit=AuditLog(lab.db),
        clock=lambda: now,
    )
    sig = Signal(
        source="insider_cluster",
        ticker="LATER",
        horizon_days=30,
        conviction=D("0.8"),
        stop_loss_pct=D("0.1"),
        thesis="t",
        dedup_key="late1",
        created_at=now,
    )
    system.submit_signal(sig)
    system.run_cycle()
    system.decide(sig.id, approve=True, actor="t")
    system.limits = RiskLimits(restricted=["LATER"])
    system.risk = RiskEngine(system.limits, system.commissions)
    system.run_cycle()
    server = build_server(lab)
    view = json.dumps([call(server, "list_signals"), call(server, "get_status")])
    assert sig.id not in view and "LATER" not in view


def test_failure_alerts_notify_and_reconnect() -> None:
    from types import SimpleNamespace

    from trader.broker.base import BrokerError
    from trader.cli import FailureAlerts

    sent: list[str] = []
    events: list[str] = []
    tg = SimpleNamespace(notify=sent.append)
    audit = SimpleNamespace(record=lambda event, payload, ts=None: events.append(event))
    old = SimpleNamespace(telegram=tg, close=lambda: events.append("old closed"))
    new = SimpleNamespace(telegram=tg, close=lambda: None, audit=audit, clock=lambda: None)
    t = [0.0]
    alerts = FailureAlerts(every=1800, clock=lambda: t[0])
    assert alerts.failed(old, RuntimeError("boom"), rebuild=lambda: new) is old  # type: ignore[arg-type]
    rebuilt = alerts.failed(old, BrokerError("gateway down"), rebuild=lambda: events.append("rebuilt") or new)  # type: ignore[arg-type]
    assert rebuilt is new and events[:2] == ["old closed", "rebuilt"]  # old connection released first
    assert "broker.reconnected" in events
    assert sum("ciclo fallito" in m for m in sent) == 1  # throttled
    assert any("ripristinata" in m for m in sent)
    alerts.recovered(new)  # type: ignore[arg-type]
    assert sent[-1] == "Ciclo di nuovo regolare."


def test_cycle_still_exits_when_optional_phases_fail(cli_env: Path) -> None:
    from decimal import Decimal

    from trader.cli import _one_cycle
    from trader.domain import Quote, Signal
    from trader.runtime import build_runtime

    rt = build_runtime(Settings(_env_file=None))  # type: ignore[call-arg]
    clock = [datetime(2026, 11, 24, 15, tzinfo=UTC)]
    rt.system.clock = lambda: clock[0]
    rt.broker.clock = lambda: clock[0]  # type: ignore[attr-defined]

    def q(price: str) -> Quote:
        p = Decimal(price)
        return Quote(
            ticker="ACME",
            ts=clock[0],
            bid=p - Decimal("0.01"),
            ask=p + Decimal("0.01"),
            last=p,
            avg_daily_dollar_volume=Decimal("100000000"),
        )

    rt.broker.set_quote(q("50"))  # type: ignore[attr-defined]
    sig = Signal(
        source="t",
        ticker="ACME",
        horizon_days=30,
        conviction=D("0.8"),
        stop_loss_pct=D("0.08"),
        thesis="t",
        dedup_key="oc1",
        created_at=clock[0],
    )
    rt.system.submit_signal(sig)
    rt.system.run_cycle()
    rt.system.decide(sig.id, approve=True, actor="t")
    rt.system.run_cycle()
    stop = D(rt.system.status()["positions"]["ACME"]["exit_plan"]["stop_price"])

    class Broken:
        def process_updates(self, system: object) -> int:
            raise ConnectionError("telegram unreachable")

    seen_sells: list[int] = []

    def llm_down(**_: object) -> list[object]:
        # Record what had happened when the slow optional phase started (asserted outside, because
        # exceptions raised here are swallowed by the isolation wrapper).
        seen_sells.append(
            sum(1 for o in rt.broker.orders.values() if o.request.side.value == "SELL")  # type: ignore[attr-defined]
        )
        raise RuntimeError("anthropic 529 overloaded")

    rt.telegram = Broken()  # type: ignore[assignment]
    rt.generate_signals = llm_down  # type: ignore[method-assign,assignment]
    clock[0] = clock[0].replace(minute=10)
    rt.broker.set_quote(q(str(stop - 1)))  # type: ignore[attr-defined]
    out = _one_cycle(rt, ingest=False, generate=True)
    assert "telegram_updates_error" in out and out["new_signals_error"].startswith("RuntimeError")
    assert out["cycle"]["exits"] == 1
    assert seen_sells == [1]  # the stop exit was already sent when the slow phase began


def test_mcp_insider_screen_hides_private_tickers(tmp_path: Path) -> None:
    lab = Lab(settings(tmp_path))
    now = datetime.now(UTC)
    with lab.db.session() as s:
        for owner in (1, 2):
            s.add(
                InsiderTransactionRow(
                    accession=f"p{owner}",
                    seq=0,
                    issuer_cik=1,
                    ticker="SECRT",
                    owner_cik=owner,
                    owner_name=f"O{owner}",
                    is_director=True,
                    is_officer=True,
                    is_ten_pct_owner=False,
                    officer_title="CEO",
                    transaction_date=now.date(),
                    code="P",
                    acquired_disposed="A",
                    shares=D("10000"),
                    price=D("30"),
                    shares_owned_after=None,
                    direct=True,
                    is_10b5_1=False,
                    accepted_at=now - timedelta(days=1),
                    ingested_at=now,
                )
            )
    assert call(build_server(lab), "insider_clusters") == []


def test_mcp_cannot_tell_private_8k_from_others(tmp_path: Path) -> None:
    from trader.audit import AuditLog
    from trader.db import FilingRow, FilingTextRow
    from trader.signals.llm import FilingAnalyzer, analyze_recent_8k

    lab = Lab(settings(tmp_path))
    now = datetime.now(UTC)
    with lab.db.session() as s:
        for acc, ticker in (("0000000001-26-000001", "ACME"), ("0000000002-26-000002", "SECRT")):
            s.add(
                FilingRow(
                    accession=acc,
                    cik=1,
                    ticker=ticker,
                    company=ticker,
                    form="8-K",
                    items="x",
                    filed_date=now.date(),
                    accepted_at=now - timedelta(hours=1),
                    ingested_at=now,
                    url="u",
                    analyzed=False,
                )
            )
            s.add(FilingTextRow(accession=acc, text="text"))

    def caller(**kw: object) -> tuple[dict[str, object], dict[str, int]]:
        return {"relevant": False, "event_type": "other", "summary": "routine"}, {}

    audit = AuditLog(lab.db)
    limits = lab.settings.risk_limits()
    analyze_recent_8k(
        lab.db,
        FilingAnalyzer(caller),
        now,
        is_excluded=limits.is_restricted,
        on_outcome=lambda o: audit.record("llm.analysis", {"accession": o.accession}),
    )
    server = build_server(lab)
    filings = call(server, "recent_filings", {"form": "8-K"})
    assert {tuple(sorted(f)) for f in filings} == {tuple(sorted(filings[0]))}  # identical shape
    assert all("analyzed" not in json.dumps(f) for f in filings)
