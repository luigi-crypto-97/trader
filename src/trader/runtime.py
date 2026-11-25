"""Wiring: build every component from ``Settings``."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from trader.approval.telegram import TelegramBot
from trader.audit import AuditLog
from trader.broker.base import Broker
from trader.broker.commissions import IbkrTieredUS
from trader.broker.simulated import SimulatedBroker
from trader.config import RiskLimits, Settings
from trader.db import Database
from trader.domain import Signal
from trader.pipeline import TradingSystem
from trader.signals.llm import AnalysisOutcome

log = logging.getLogger(__name__)

SIM_STATE_KEY = "sim_broker"
DAILY_INGEST_KEY = "edgar_daily_ingest_done"


@dataclass
class Runtime:
    settings: Settings
    db: Database
    audit: AuditLog
    limits: RiskLimits
    broker: Broker
    system: TradingSystem
    telegram: TelegramBot | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def save(self) -> None:
        if isinstance(self.broker, SimulatedBroker):
            self.db.kv_set(SIM_STATE_KEY, self.broker.to_state())

    def close(self) -> None:
        self.save()
        close = getattr(self.broker, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------- data
    def edgar(self) -> object:
        from trader.data.edgar.client import EdgarClient
        from trader.data.edgar.ingest import EdgarIngestor

        if not self.settings.sec_user_agent:
            raise RuntimeError("Set TRADER_SEC_USER_AGENT (e.g. 'Nome Cognome contatto@dominio') in .env")
        return EdgarIngestor(EdgarClient(self.settings.sec_user_agent), self.db, clock=self.clock)

    def ingest(self, *, latest: bool = True, day: date | None = None) -> dict[str, object]:
        from trader.data.edgar.ingest import EdgarIngestor

        ing = self.edgar()
        assert isinstance(ing, EdgarIngestor)
        report = ing.ingest_day(day) if day else ing.ingest_latest() if latest else None
        if report is None:
            return {}
        self.audit.record(
            "ingest",
            {"day": str(day), "latest": latest, "stored": report.stored, "errors": len(report.errors)},
            ts=self.clock(),
        )
        return report.__dict__

    def ingest_previous_day_once(self) -> dict[str, object] | None:
        """Complete yesterday's filings from the master index once per day (catches what the
        intraday feed missed)."""
        today = self.clock().date()
        if self.db.kv_get(DAILY_INGEST_KEY) == today.isoformat():
            return None
        prev = today - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        result = self.ingest(day=prev)
        # An empty or missing index (EDGAR answers 403 for files it does not have) is retried on the
        # next cycle instead of silently marking the day as complete.
        # Filings that failed to download are retried too: the day is complete only without errors.
        found = (
            int(str(result.get("candidates", 0) or 0)) > 0
            or int(str(result.get("already_stored", 0) or 0)) > 0
        )
        if found and not result.get("errors"):
            self.db.kv_set(DAILY_INGEST_KEY, today.isoformat())
        return result

    # ---------------------------------------------------------- signals
    def _record_llm_outcome(self, outcome: AnalysisOutcome) -> None:
        self.audit.record(
            "llm.analysis",
            {
                "accession": outcome.accession,
                "stage": outcome.stage,
                "note": outcome.note[:2000],
                "usage": outcome.usage,
            },
            ts=self.clock(),
        )

    def generate_signals(self, *, use_llm: bool | None = None) -> list[tuple[Signal, bool]]:
        from trader.signals.insider import find_insider_clusters

        now = self.clock()
        produced: list[Signal] = list(find_insider_clusters(self.db, now))
        llm = self.settings.llm_enabled if use_llm is None else use_llm
        if llm:
            from trader.signals.llm import AnthropicJsonCaller, FilingAnalyzer, analyze_recent_8k

            analyzer = FilingAnalyzer(
                AnthropicJsonCaller(),
                triage_model=self.settings.llm_triage_model,
                analysis_model=self.settings.llm_analysis_model,
                analysis_effort=self.settings.llm_analysis_effort,
                max_chars=self.settings.llm_max_filing_chars,
                min_conviction=float(self.limits.min_conviction),
            )
            produced += analyze_recent_8k(
                self.db,
                analyzer,
                now,
                on_outcome=self._record_llm_outcome,
                exclude_tickers=self.limits.restricted,  # private list: never analysed, never logged
            )
        tradable = [s for s in produced if not self.limits.is_restricted(s.ticker)]  # private list
        return [(s, self.system.submit_signal(s)) for s in tradable]


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or Settings()
    db = Database(settings.db_url)
    audit = AuditLog(db)
    limits = settings.risk_limits()
    commissions = IbkrTieredUS()
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731
    broker: Broker
    if settings.broker == "sim":
        state = db.kv_get(SIM_STATE_KEY)
        if state:
            broker = SimulatedBroker.from_state(
                state, clock=clock, commissions=commissions, slippage_bps=settings.sim_slippage_bps
            )
        else:
            broker = SimulatedBroker(
                settled_cash=settings.sim_initial_cash,
                clock=clock,
                commissions=commissions,
                slippage_bps=settings.sim_slippage_bps,
            )
    else:
        from trader.broker.ibkr import IbkrBroker

        broker = IbkrBroker.connect(
            settings.ibkr_host,
            settings.ibkr_port,
            settings.ibkr_client_id,
            settings.ibkr_account,
            settings.ibkr_market_data_type,
        )
    telegram = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        telegram = TelegramBot(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, db)
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=limits,
        commissions=commissions,
        audit=audit,
        approval_mode=settings.approval_mode,
        notifier=telegram,
        clock=clock,
    )
    if settings.llm_enabled and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("LLM enabled but ANTHROPIC_API_KEY not set; the SDK will try other credential sources")
    return Runtime(
        settings=settings,
        db=db,
        audit=audit,
        limits=limits,
        broker=broker,
        system=system,
        telegram=telegram,
        clock=clock,
    )
