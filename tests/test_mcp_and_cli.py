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
        "audit_tail",
    }
    assert not any(w in n for n in names for w in ("order", "buy", "sell", "approve", "kill"))


def test_mcp_status_hides_restricted_list(tmp_path: Path) -> None:
    server = build_server(Lab(settings(tmp_path)))
    status = call(server, "get_status")
    assert status["risk_limits"]["restricted_count"] == 1
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
    assert again["queued"] is False
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
    assert blocked == {"queued": False, "error": "ticker not tradable"}
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
    exposed = json.dumps(
        [call(server, "list_signals"), call(server, "audit_tail", {"n": 200}), call(server, "get_status")]
    )
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
