"""Command line interface: ``uv run trader --help``."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import typer

from trader.config import Settings
from trader.domain import Quote, SignalStatus
from trader.runtime import Runtime, build_runtime

app = typer.Typer(
    help="Trader: segnali SEC, rischio deterministico, approvazione umana.", no_args_is_help=True
)
signals_app = typer.Typer(help="Segnali: genera, elenca, approva.", no_args_is_help=True)
ingest_app = typer.Typer(help="Acquisizione filing SEC EDGAR.", no_args_is_help=True)
audit_app = typer.Typer(help="Registro di audit.", no_args_is_help=True)
sim_app = typer.Typer(help="Strumenti per il broker simulato.", no_args_is_help=True)
data_app = typer.Typer(help="Dati storici: prezzi (Massive) e archivi insider SEC.", no_args_is_help=True)
research_app = typer.Typer(help="Studi di ricerca (fase 2).", no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")
app.add_typer(signals_app, name="signals")
app.add_typer(ingest_app, name="ingest")
app.add_typer(audit_app, name="audit")
app.add_typer(sim_app, name="sim")

log = logging.getLogger("trader")


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _out(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def public_rejections(rejected: list[tuple[date, str, str]]) -> list[tuple[date, str, str]]:
    """Backtest rejections minus those caused by the private restricted list."""
    from trader.risk.engine import PRIVATE_RULE

    return [r for r in rejected if PRIVATE_RULE not in r[2]]


class FailureAlerts:
    """Makes failing cycles visible: Telegram alert (at most every 30 minutes) and, when the broker
    connection is lost, a fresh connection. A silent loop would also skip stop-loss exits."""

    def __init__(self, every: float = 1800.0, clock: Any = time.monotonic) -> None:
        self.every = every
        self.clock = clock
        self.last_alert: float | None = None
        self.failing = False

    def _send(self, rt: Runtime, text: str) -> None:
        if rt.telegram is None:
            return
        try:
            rt.telegram.notify(text)
        except Exception as exc:
            log.warning("alert not delivered: %s", exc)

    def failed(self, rt: Runtime, exc: Exception, rebuild: Any) -> Runtime:
        from trader.broker.base import BrokerError

        self.failing = True
        now = self.clock()
        if self.last_alert is None or now - self.last_alert >= self.every:
            self.last_alert = now
            self._send(
                rt,
                f"ATTENZIONE: ciclo fallito ({type(exc).__name__}: {str(exc)[:200]}). "
                "Uscite e ingressi sospesi finché il problema persiste.",
            )
        if isinstance(exc, BrokerError):
            # Release the old connection (and save simulator state) before opening a new one, so
            # two sessions never share the same IB client id.
            try:
                rt.close()
            except Exception as close_exc:
                log.warning("closing the old connection failed: %s", close_exc)
            try:
                fresh: Runtime = rebuild()
            except Exception as again:
                log.warning("reconnection failed: %s", again)
                return rt  # still broken: the next cycle raises BrokerError and retries
            try:
                fresh.audit.record("broker.reconnected", {"after": type(exc).__name__}, ts=fresh.clock())
            except Exception as audit_exc:
                log.warning("could not audit the reconnection: %s", audit_exc)
            self._send(fresh, "Connessione al broker ripristinata.")
            return fresh
        return rt

    def recovered(self, rt: Runtime) -> None:
        if self.failing:
            self.failing = False
            self.last_alert = None
            self._send(rt, "Ciclo di nuovo regolare.")


def _rt() -> Runtime:
    return build_runtime(Settings())


# --------------------------------------------------------------------- status
@app.command()
def status() -> None:
    """Stato del conto, posizioni, piani di uscita, kill switch."""
    rt = _rt()
    try:
        _out(rt.system.status())
    finally:
        rt.close()


@app.command()
def doctor() -> None:
    """Controlla configurazione e dipendenze senza inviare ordini."""
    s = Settings()
    checks: dict[str, Any] = {}
    try:
        limits = s.risk_limits()
        checks["risk_config"] = f"ok ({s.risk_config_path}, restricted list: {len(limits.restricted)} ticker)"
    except Exception as exc:
        checks["risk_config"] = f"ERRORE: {exc}"
    checks["db_url"] = s.db_url
    checks["broker"] = (
        s.broker if s.broker == "sim" else f"ibkr {s.ibkr_host}:{s.ibkr_port} (paper=4002/live=4001)"
    )
    checks["approval_mode"] = s.approval_mode
    checks["sec_user_agent"] = "ok" if s.sec_user_agent else "MANCANTE: imposta TRADER_SEC_USER_AGENT"
    checks["telegram"] = (
        "ok" if s.telegram_bot_token and s.telegram_chat_id else "non configurato (approva da CLI)"
    )
    checks["llm"] = (
        f"attivo ({s.llm_triage_model} -> {s.llm_analysis_model})" if s.llm_enabled else "disattivo"
    )
    if s.broker == "ibkr":
        try:
            rt = build_runtime(s)
            acct = rt.broker.account()
            checks["ibkr_connection"] = f"ok, equity {acct.equity} USD"
            rt.close()
        except Exception as exc:
            checks["ibkr_connection"] = f"ERRORE: {exc}"
    _out(checks)


@app.command()
def kill(
    state: Annotated[str, typer.Argument(help="on | off")],
    reason: Annotated[str, typer.Option(help="Motivo (registrato nell'audit)")] = "manual",
) -> None:
    """Attiva o disattiva il kill switch (blocca nuove entrate; le uscite restano consentite)."""
    if state not in {"on", "off"}:
        raise typer.BadParameter("usa 'on' oppure 'off'")
    rt = _rt()
    try:
        rt.system.set_kill_switch(state == "on", reason=reason, actor="cli")
        typer.echo(f"kill switch {state.upper()}")
    finally:
        rt.close()


# -------------------------------------------------------------------- ingest
@ingest_app.command("latest")
def ingest_latest() -> None:
    """Ultimi Form 4 e 8-K dal feed EDGAR (intraday)."""
    _setup_logging()
    rt = _rt()
    try:
        _out(rt.ingest(latest=True))
    finally:
        rt.close()


@ingest_app.command("day")
def ingest_day(day: Annotated[str, typer.Argument(help="YYYY-MM-DD")]) -> None:
    """Tutti i Form 4 e 8-K di una giornata (indice master EDGAR)."""
    _setup_logging()
    rt = _rt()
    try:
        _out(rt.ingest(day=date.fromisoformat(day)))
    finally:
        rt.close()


# ------------------------------------------------------------------- signals
@signals_app.command("generate")
def signals_generate(llm: Annotated[bool | None, typer.Option("--llm/--no-llm")] = None) -> None:
    """Genera segnali (cluster insider; analisi LLM degli 8-K se abilitata) e li mette in coda."""
    _setup_logging()
    rt = _rt()
    try:
        results = rt.generate_signals(use_llm=llm)
        _out(
            [
                {
                    "ticker": s.ticker,
                    "source": s.source,
                    "conviction": str(s.conviction),
                    "new": new,
                    "id": s.id,
                }
                for s, new in results
            ]
        )
    finally:
        rt.close()


@signals_app.command("list")
def signals_list(
    status: Annotated[str | None, typer.Option(help="es. PENDING_APPROVAL")] = None, limit: int = 20
) -> None:
    rt = _rt()
    try:
        st = SignalStatus(status.upper()) if status else None
        _out(
            [
                {
                    k: v
                    for k, v in s.items()
                    if k
                    in {
                        "id",
                        "created_at",
                        "ticker",
                        "source",
                        "status",
                        "conviction",
                        "status_reason",
                        "expires_at",
                    }
                }
                for s in rt.system.list_signals(st, limit)
            ]
        )
    finally:
        rt.close()


@signals_app.command("show")
def signals_show(signal_id: str) -> None:
    rt = _rt()
    try:
        _out(rt.system.get_signal(signal_id) or {"error": "not found"})
    finally:
        rt.close()


@app.command()
def approve(signal_id: str, note: str = "") -> None:
    """Approva un segnale in attesa (verrà rieseguito il controllo rischio prima dell'invio)."""
    rt = _rt()
    try:
        ok, msg = rt.system.decide(signal_id, approve=True, actor="cli", note=note)
        typer.echo(msg)
        raise typer.Exit(0 if ok else 1)
    finally:
        rt.close()


@app.command()
def reject(signal_id: str, note: str = "") -> None:
    rt = _rt()
    try:
        ok, msg = rt.system.decide(signal_id, approve=False, actor="cli", note=note)
        typer.echo(msg)
        raise typer.Exit(0 if ok else 1)
    finally:
        rt.close()


# --------------------------------------------------------------------- cycle
TELEGRAM_BUDGET = 30.0  # seconds of Telegram command handling before the trading cycle


def _one_cycle(rt: Runtime, *, ingest: bool, generate: bool, deadline: float | None = None) -> dict[str, Any]:
    """One cycle. Optional phases are isolated (their errors never stop the trading cycle) and
    time-bounded: Telegram gets TELEGRAM_BUDGET before trading; ingestion and signal generation run
    after trading and stop at ``deadline`` (time.monotonic), so the next cycle starts on time.
    Work left over is picked up by the next cycle."""
    out: dict[str, Any] = {}
    if deadline is None:
        deadline = time.monotonic() + 300.0

    def optional(name: str, fn: Any) -> None:
        try:
            out[name] = fn()
        except Exception as exc:
            log.warning("%s failed: %s", name, exc)
            out[f"{name}_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    # 1) human commands (approvals, /kill), bounded; 2) the trading cycle - sync, stop-loss and
    # time exits, approved orders; 3) slow optional work, bounded by the deadline. New signals are
    # reviewed on the next cycle.
    if rt.telegram is not None:
        telegram = rt.telegram
        tg_deadline = min(deadline, time.monotonic() + TELEGRAM_BUDGET)
        optional("telegram_updates", lambda: telegram.process_updates(rt.system, deadline=tg_deadline))
    out["cycle"] = rt.system.run_cycle().__dict__
    rt.save()
    if ingest and rt.settings.sec_user_agent:
        optional("daily_ingest", lambda: rt.ingest_previous_day_once(deadline=deadline))
        optional(
            "ingest",
            lambda: {k: v for k, v in rt.ingest(latest=True, deadline=deadline).items() if k != "errors"},
        )
    if generate:
        optional("new_signals", lambda: sum(1 for _, new in rt.generate_signals(deadline=deadline) if new))
    return out


@app.command()
def cycle(ingest: bool = True, generate: bool = True) -> None:
    """Un ciclo completo: Telegram, ingest, segnali, revisione, esecuzione, uscite."""
    _setup_logging()
    rt = _rt()
    try:
        _out(_one_cycle(rt, ingest=ingest, generate=generate))
    finally:
        rt.close()


@app.command()
def run(
    interval: Annotated[int, typer.Option(help="Secondi tra un ciclo e l'altro")] = 300,
    poll: Annotated[int, typer.Option(help="Secondi tra i controlli Telegram")] = 10,
) -> None:
    """Esecuzione continua. Ctrl+C per fermare (lo stato viene salvato)."""
    _setup_logging()
    rt = _rt()
    typer.echo(f"Avvio: broker={rt.broker.name}, approvazione={rt.settings.approval_mode}, ciclo={interval}s")
    next_cycle = 0.0
    alerts = FailureAlerts()
    # Reconnections reuse the start environment settings; risk.yaml and the restricted list are
    # re-read (a reconnection is audited).
    start_settings = rt.settings
    try:
        while True:
            now = time.monotonic()
            if now >= next_cycle:
                next_cycle = now + interval
                try:
                    # Optional work must end before the next trading cycle is due (10 s margin).
                    report = _one_cycle(rt, ingest=True, generate=True, deadline=next_cycle - 10)
                    log.info("cycle: %s", json.dumps(report, default=str))
                    alerts.recovered(rt)
                except Exception as exc:
                    log.exception("cycle failed; continuing")
                    rt = alerts.failed(rt, exc, rebuild=partial(build_runtime, start_settings))
                    next_cycle = now + min(interval, 60)  # retry the trading cycle soon
            elif rt.telegram is not None:
                try:  # between cycles: a Telegram outage only affects Telegram
                    rt.telegram.process_updates(rt.system)
                except Exception as exc:
                    log.warning("telegram polling failed: %s", exc)
            time.sleep(poll)
    except KeyboardInterrupt:
        typer.echo("Arresto richiesto.")
    finally:
        rt.close()


# --------------------------------------------------------------------- audit
@audit_app.command("verify")
def audit_verify() -> None:
    rt = _rt()
    try:
        v = rt.audit.verify()
        _out(v.__dict__)
        raise typer.Exit(0 if v.ok else 2)
    finally:
        rt.close()


@audit_app.command("tail")
def audit_tail(n: int = 20) -> None:
    rt = _rt()
    try:
        _out(rt.audit.tail(n))
    finally:
        rt.close()


# ----------------------------------------------------------------------- tax
@app.command()
def tax(year: int) -> None:
    """Bozza fiscale italiana (quadro RT, stima IVAFE) dai fill registrati."""
    from sqlalchemy import select

    from trader.data.fx import FxRates
    from trader.db import FillRow
    from trader.domain import Fill, Side
    from trader.tax.ledger import ItalianTaxLedger

    rt = _rt()
    try:
        fx = FxRates(rt.db)
        with rt.db.session() as s:
            fills = [
                Fill(
                    fill_id=f.fill_id,
                    client_order_id=f.client_order_id,
                    ticker=f.ticker,
                    side=Side(f.side),
                    qty=f.qty,
                    price=f.price,
                    commission=f.commission,
                    ts=f.ts,
                )
                for f in s.scalars(select(FillRow))
            ]
        ledger = ItalianTaxLedger(fx=lambda d: fx.rate("USD", d))
        ledger.process(f for f in fills if f.ts.year <= year)
        year_end = date(year, 12, 31)
        valuation_day = min(year_end, datetime.now(UTC).date())
        account = rt.broker.account()
        values = {t: p.market_value / fx.rate("USD", valuation_day) for t, p in account.positions.items()}
        _out(
            {
                "avvertenza": "Bozza da verificare con il commercialista.",
                "plusvalenze": ledger.capital_gains(year),
                "ivafe_rw": ledger.ivafe_estimate(year, values),
                "valori_fine_anno_al": valuation_day.isoformat(),
            }
        )
    finally:
        rt.close()


# ---------------------------------------------------------------- backtest
@app.command()
def backtest(
    prices: Annotated[Path, typer.Option(help="Cartella con <TICKER>.csv (date,open,high,low,close,volume)")],
    start: str,
    end: str,
    cash: str = "1000",
    benchmark: str = "SPY",
) -> None:
    """Backtest del segnale cluster insider sui dati già acquisiti, prezzi da CSV."""
    from datetime import timedelta

    from trader.backtest.engine import Backtester, load_csv_dir, replay_signals
    from trader.signals.insider import find_insider_clusters

    rt = _rt()
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        days = [
            s + timedelta(days=i) for i in range((e - s).days + 1) if (s + timedelta(days=i)).weekday() < 5
        ]
        timed = replay_signals(lambda now: find_insider_clusters(rt.db, now), days)
        bt = Backtester(
            load_csv_dir(prices), limits=rt.limits, initial_cash=Decimal(cash), benchmark=benchmark
        )
        result = bt.run(timed, s, e)
        _out(
            {
                "metrics": result.metrics(),
                "signals": len(timed),
                "trades": len(result.trades),
                "rejected_sample": public_rejections(result.rejected)[:20],
            }
        )
    finally:
        rt.close()


# ---------------------------------------------------------------------- data
@data_app.command("prices")
def data_prices(start: str, end: str) -> None:
    """Scarica le barre giornaliere di tutte le azioni USA (Massive grouped daily), riprendibile.

    Chiave: TRADER_MASSIVE_API_KEY (piano gratuito: 5 richieste/minuto, ~2 anni di storico)."""
    from trader.data.prices import MassiveClient, PriceCache, backfill

    _setup_logging()
    s = Settings()
    cache = PriceCache(s.massive_cache_dir)
    client = MassiveClient()
    # Splits are fetched for everything already cached too, so the stored list stays complete.
    first = min([date.fromisoformat(start), *cache.days()[:1]])
    today = datetime.now(UTC).date()
    cache.write_splits(client.splits(first, today), first, today)
    n = backfill(
        client,
        cache,
        date.fromisoformat(start),
        date.fromisoformat(end),
        on_progress=lambda d, k: typer.echo(f"{d}: {k} titoli"),
    )
    typer.echo(f"scaricati {n} giorni; in cache {len(cache.days())}")


@data_app.command("insider-history")
def data_insider_history(quarters: Annotated[list[str], typer.Argument(help="es. 2025q1 2025q2")]) -> None:
    """Carica gli archivi trimestrali SEC dei Form 4 nel database di ricerca."""
    from trader.data.edgar.client import EdgarClient
    from trader.data.edgar.datasets import download_quarter, load_quarter
    from trader.db import Database

    _setup_logging()
    s = Settings()
    if not s.sec_user_agent:
        raise typer.BadParameter("imposta TRADER_SEC_USER_AGENT")
    client = EdgarClient(s.sec_user_agent)
    db = Database(s.research_db_url)
    for q in quarters:
        report = load_quarter(db, download_quarter(client, q.lower(), s.form345_cache_dir))
        _out(report.__dict__)


# ------------------------------------------------------------------ research
@research_app.command("insider")
def research_insider(
    start: str,
    end: str,
    split: Annotated[str, typer.Option(help="Inizio del periodo out-of-sample (YYYY-MM-DD)")],
    out: Path = Path("docs/research/fase2-insider.md"),
    json_out: Path = Path("var/research/fase2-insider.json"),
) -> None:
    """Studio point-in-time del segnale insider: eventi, extra-rendimento vs SPY, portafoglio da 1.000 USD."""
    from trader.backtest.engine import replay_signals
    from trader.data.prices import PriceCache
    from trader.db import Database
    from trader.research.insider_study import StudyConfig, default_variants, render_markdown, run_study

    _setup_logging()
    s = Settings()
    db = Database(s.research_db_url)
    cfg = StudyConfig(
        start=date.fromisoformat(start), end=date.fromisoformat(end), split=date.fromisoformat(split)
    )
    variants = default_variants(db)
    cache = PriceCache(s.massive_cache_dir)
    # Trading calendar from the benchmark, then replay every variant once; only the tickers that
    # actually produce signals are loaded (keeps memory small).
    trading_days = sorted(cache.load({cfg.benchmark})[cfg.benchmark])
    replay_days = [d for d in trading_days if cfg.start <= d <= cfg.end]
    timed = {v.name: replay_signals(v.generator, replay_days) for v in variants}
    tickers = {t.signal.ticker for signals in timed.values() for t in signals}
    typer.echo(f"segnali: { {k: len(v) for k, v in timed.items()} }, titoli da caricare: {len(tickers)}")
    prices = cache.load(tickers | {cfg.benchmark})
    results = run_study(
        variants,
        trading_days,
        prices,
        cfg,
        limits=s.risk_limits(),
        splits=cache.read_splits(trading_days[0], trading_days[-1]),
        timed_by_variant=timed,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    out.write_text(render_markdown(results), encoding="utf-8")
    typer.echo(f"report: {out}  dati: {json_out}  variante scelta: {results['chosen_variant']}")


# ----------------------------------------------------------------------- sim
@sim_app.command("quote")
def sim_quote(ticker: str, price: str, spread: str = "0.02", adv: str = "50000000") -> None:
    """Imposta una quotazione nel broker simulato (solo per prove)."""
    from trader.broker.simulated import SimulatedBroker

    rt = _rt()
    try:
        if not isinstance(rt.broker, SimulatedBroker):
            raise typer.BadParameter("disponibile solo con TRADER_BROKER=sim")
        p, half = Decimal(price), Decimal(spread) / 2
        rt.broker.set_quote(
            Quote(
                ticker=ticker.upper(),
                ts=datetime.now(UTC),
                bid=p - half,
                ask=p + half,
                last=p,
                avg_daily_dollar_volume=Decimal(adv),
            )
        )
        typer.echo(f"{ticker.upper()} {p - half}/{p + half}")
    finally:
        rt.close()


if __name__ == "__main__":  # pragma: no cover
    app()
