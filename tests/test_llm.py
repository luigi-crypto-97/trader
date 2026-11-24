from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trader.db import Database, FilingRow, FilingTextRow
from trader.signals.llm import (
    AnthropicJsonCaller,
    FilingAnalyzer,
    LlmRefusal,
    analyze_recent_8k,
    verify_quotes,
)

NOW = datetime(2026, 11, 24, 15, tzinfo=UTC)
TEXT = (
    "=== 8-K ===\nACME Corp announced today that it was awarded a five-year contract valued at "
    "approximately $480 million by the U.S. Department of Energy.\nRevenue for fiscal 2025 was $610 million."
)


class FakeCaller:
    def __init__(self, triage: dict[str, Any], analysis: dict[str, Any] | None = None) -> None:
        self.responses = [triage] + ([analysis] if analysis else [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> tuple[dict[str, Any], dict[str, int]]:
        self.calls.append(kw)
        return self.responses.pop(0), {"input_tokens": 10, "output_tokens": 5}


def filing(ticker: str | None = "ACME", accession: str = "0000000001-26-000001") -> FilingRow:
    return FilingRow(
        accession=accession,
        cik=1,
        ticker=ticker,
        company="ACME Corp",
        form="8-K",
        items="Other Events",
        filed_date=date(2026, 11, 24),
        accepted_at=NOW,
        ingested_at=NOW,
        url="https://www.sec.gov/x",
        analyzed=False,
    )


BUY = {
    "action": "BUY",
    "horizon_days": 20,
    "conviction": 0.72,
    "stop_loss_pct": 0.09,
    "thesis": "Contratto pluriennale pari a ~79% del fatturato annuo.",
    "key_risks": ["Esecuzione del contratto"],
    "evidence_quotes": [
        "awarded a five-year contract valued at approximately $480 million",
        "Revenue for fiscal 2025 was $610 million",
    ],
}
RELEVANT = {"relevant": True, "event_type": "contract_win", "summary": "large DOE contract"}


def test_buy_idea_with_verified_quotes() -> None:
    caller = FakeCaller(RELEVANT, BUY)
    out = FilingAnalyzer(caller).analyze(filing(), TEXT, NOW)
    assert out.signal is not None and out.stage == "done"
    sig = out.signal
    assert sig.ticker == "ACME" and sig.source == "llm_8k"
    assert str(sig.conviction) == "0.72" and str(sig.stop_loss_pct) == "0.09"
    assert sig.dedup_key == "llm_8k:0000000001-26-000001"
    assert [e["quote"] for e in sig.evidence if "quote" in e] == BUY["evidence_quotes"]
    assert caller.calls[0]["model"] == "claude-haiku-5-5" and caller.calls[0]["effort"] == "low"
    assert caller.calls[1]["model"] == "claude-opus-5-5"
    assert "<filing>" in caller.calls[1]["user"] and TEXT in caller.calls[1]["user"]
    assert "untrusted" in caller.calls[1]["system"]


def test_hallucinated_quote_discards_idea() -> None:
    bad = dict(BUY, evidence_quotes=["awarded a ten-year contract valued at $4.8 billion"])
    out = FilingAnalyzer(FakeCaller(RELEVANT, bad)).analyze(filing(), TEXT, NOW)
    assert out.signal is None and out.stage == "verification"


@pytest.mark.parametrize(
    ("triage", "analysis", "stage"),
    [
        ({"relevant": False, "event_type": "other", "summary": "routine"}, None, "triage"),
        (RELEVANT, dict(BUY, action="NONE"), "analysis"),
        (RELEVANT, dict(BUY, conviction=0.3), "analysis"),
        (RELEVANT, dict(BUY, stop_loss_pct=0.9), "analysis"),  # schema range violation
    ],
)
def test_no_signal_paths(triage: dict[str, Any], analysis: dict[str, Any] | None, stage: str) -> None:
    out = FilingAnalyzer(FakeCaller(triage, analysis)).analyze(filing(), TEXT, NOW)
    assert out.signal is None and out.stage == stage


def test_too_long_filing_skipped_not_truncated() -> None:
    caller = FakeCaller(RELEVANT, BUY)
    out = FilingAnalyzer(caller, max_chars=50).analyze(filing(), TEXT, NOW)
    assert out.stage == "skip" and not caller.calls


def test_verify_quotes_normalizes_whitespace_and_case() -> None:
    ok, bad = verify_quotes(["REVENUE for   fiscal\n2025", "nope"], TEXT)
    assert ok == ["REVENUE for   fiscal\n2025"] and bad == ["nope"]


def test_analyze_recent_marks_filings(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'l.sqlite'}")
    with db.session() as s:
        s.add(filing())
        s.add(FilingTextRow(accession="0000000001-26-000001", text=TEXT))
        s.add(filing(ticker=None, accession="0000000002-26-000002"))
    outcomes: list[str] = []
    sigs = analyze_recent_8k(
        db, FilingAnalyzer(FakeCaller(RELEVANT, BUY)), NOW, on_outcome=lambda o: outcomes.append(o.stage)
    )
    assert [s.ticker for s in sigs] == ["ACME"] and outcomes == ["done"]
    assert analyze_recent_8k(db, FilingAnalyzer(FakeCaller(RELEVANT, BUY)), NOW) == []


# ----------------------------------------------------- Anthropic SDK adapter


class FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def fake_client(text: str, stop: str = "end_turn") -> SimpleNamespace:
    resp = SimpleNamespace(
        stop_reason=stop,
        stop_details=SimpleNamespace(category="cyber"),
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )
    return SimpleNamespace(messages=FakeMessages(resp), beta=SimpleNamespace(messages=FakeMessages(resp)))


def test_anthropic_caller_uses_fallbacks_for_opus_and_plain_for_haiku() -> None:
    client = fake_client('{"relevant": true}')
    caller = AnthropicJsonCaller(client)
    data, usage = caller(
        model="claude-opus-5-5",
        system="s",
        user="u",
        schema={"type": "object"},
        max_tokens=100,
        effort="high",
    )
    assert data == {"relevant": True} and usage == {"input_tokens": 100, "output_tokens": 20}
    sent = client.beta.messages.kwargs
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}},
        "effort": "high",
    }
    caller(model="claude-haiku-5-5", system="s", user="u", schema={}, max_tokens=100, effort="low")
    assert "fallbacks" not in client.messages.kwargs


def test_anthropic_caller_refusal_raises() -> None:
    caller = AnthropicJsonCaller(fake_client("", stop="refusal"))
    with pytest.raises(LlmRefusal):
        caller(model="claude-opus-5-5", system="s", user="u", schema={}, max_tokens=10, effort="low")
