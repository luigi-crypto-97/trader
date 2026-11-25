"""Phase 2 research: does insider cluster buying predict excess returns?

Protocol (fixed before looking at results):
* Signals are produced by the *production* generator (``find_insider_clusters``) replayed as of
  each trading day's 16:00 New York close, so only data public at that time is used.
* Events: first signal per ticker, then no new event for the same ticker for ``min_gap_days``.
* Entry: open of the first session after the signal became available. Exit: close after N
  sessions (N in ``horizons``); if the stock stops trading (delisting) the last close is used.
* Tradability filters at entry: price >= ``min_price`` and 20-session average dollar volume
  >= ``min_adv`` (computed from bars *before* the entry day).
* Excess return = stock return - SPY return over the same window.
* Statistics: per-event mean/median/hit rate, and a month-clustered t-statistic (events in the
  same month are not independent). In-sample selects the variant; out-of-sample only reports.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from trader.backtest.engine import Backtester, PriceData, TimedSignal, replay_signals
from trader.broker.base import NEW_YORK
from trader.config import RiskLimits
from trader.db import Database
from trader.domain import Signal
from trader.signals.insider import InsiderClusterParams, find_insider_clusters


@dataclass(frozen=True)
class StudyConfig:
    start: date
    end: date
    split: date  # events available before this date are in-sample
    horizons: tuple[int, ...] = (5, 20, 60)
    primary_horizon: int = 20
    min_gap_days: int = 30
    min_price: Decimal = Decimal("5")
    min_adv: Decimal = Decimal("1000000")
    adv_window: int = 20
    benchmark: str = "SPY"

    def __post_init__(self) -> None:
        if self.primary_horizon not in self.horizons:
            raise ValueError("primary_horizon must be one of horizons")
        if not self.start <= self.split <= self.end:
            raise ValueError("split must lie between start and end")


@dataclass
class Event:
    ticker: str
    available_at: datetime
    entry_day: date
    entry_price: Decimal
    adv: Decimal
    conviction: Decimal
    returns: dict[int, tuple[float, float]] = field(default_factory=dict)  # h -> (stock, bench)
    truncated: set[int] = field(default_factory=set)

    def excess(self, h: int) -> float | None:
        r = self.returns.get(h)
        return r[0] - r[1] if r else None


def summarize(values: Sequence[float]) -> dict[str, float]:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    out = {
        "n": float(n),
        "mean": mean,
        "median": statistics.median(values),
        "hit_rate": sum(1 for v in values if v > 0) / n,
    }
    if n > 1:
        sd = statistics.stdev(values)
        out["stdev"] = sd
        out["t_stat"] = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return out


def clustered_by_month(events: Sequence[Event], h: int) -> dict[str, float]:
    """Average excess per calendar month of entry, then statistics across months."""
    by_month: dict[tuple[int, int], list[float]] = defaultdict(list)
    for e in events:
        x = e.excess(h)
        if x is not None:
            by_month[(e.entry_day.year, e.entry_day.month)].append(x)
    monthly = [sum(v) / len(v) for _, v in sorted(by_month.items())]
    s = summarize(monthly)
    return {f"months_{k}": v for k, v in s.items()}


class EventBuilder:
    def __init__(self, prices: PriceData, cfg: StudyConfig) -> None:
        self.prices = prices
        self.cfg = cfg
        bench = prices.get(cfg.benchmark)
        if not bench:
            raise ValueError(f"benchmark {cfg.benchmark} missing from price data")
        self.calendar = sorted(bench)
        self.index = {d: i for i, d in enumerate(self.calendar)}

    def _open_dt(self, d: date) -> datetime:
        return datetime.combine(d, time(9, 30), tzinfo=NEW_YORK).astimezone(UTC)

    def _entry_day(self, available_at: datetime) -> date | None:
        lo, hi = 0, len(self.calendar)
        while lo < hi:  # first session whose open is strictly after availability
            mid = (lo + hi) // 2
            if self._open_dt(self.calendar[mid]) > available_at:
                hi = mid
            else:
                lo = mid + 1
        return self.calendar[lo] if lo < len(self.calendar) else None

    def _adv(self, ticker: str, entry_idx: int) -> Decimal | None:
        bars = self.prices.get(ticker, {})
        window = [self.calendar[i] for i in range(max(0, entry_idx - self.cfg.adv_window), entry_idx)]
        vals = [bars[d].close * bars[d].volume for d in window if d in bars]
        if len(vals) < self.cfg.adv_window // 2:
            return None
        return sum(vals, Decimal("0")) / len(vals)

    def build(self, timed: Sequence[TimedSignal]) -> tuple[list[Event], dict[str, int]]:
        cfg = self.cfg
        drops: dict[str, int] = defaultdict(int)
        last_event: dict[str, datetime] = {}
        events: list[Event] = []
        bench = self.prices[cfg.benchmark]
        for ts in sorted(timed, key=lambda t: t.available_at):
            sig: Signal = ts.signal
            prev = last_event.get(sig.ticker)
            if prev is not None and ts.available_at - prev < timedelta(days=cfg.min_gap_days):
                continue  # same cluster / too close to a previous event
            last_event[sig.ticker] = ts.available_at
            entry = self._entry_day(ts.available_at)
            bars = self.prices.get(sig.ticker, {})
            if entry is None or entry not in bars:
                drops["no_price_at_entry"] += 1
                continue
            idx = self.index[entry]
            price = bars[entry].open
            if price < cfg.min_price:
                drops["below_min_price"] += 1
                continue
            adv = self._adv(sig.ticker, idx)
            if adv is None or adv < cfg.min_adv:
                drops["illiquid"] += 1
                continue
            ev = Event(sig.ticker, ts.available_at, entry, price, adv, sig.conviction)
            for h in cfg.horizons:
                exit_idx = idx + h - 1
                if exit_idx >= len(self.calendar):
                    continue  # horizon not yet observable
                exit_day = self.calendar[exit_idx]
                held = [d for d in self.calendar[idx : exit_idx + 1] if d in bars]
                last_day = held[-1]
                if last_day != exit_day:
                    ev.truncated.add(h)  # stopped trading: exit at last available close
                stock = float(bars[last_day].close / price - 1)
                b = float(bench[exit_day].close / bench[entry].open - 1)
                ev.returns[h] = (stock, b)
            events.append(ev)
        return events, dict(drops)


def report_block(events: Sequence[Event], cfg: StudyConfig) -> dict[str, Any]:
    out: dict[str, Any] = {"events": len(events)}
    for h in cfg.horizons:
        xs = [x for e in events if (x := e.excess(h)) is not None]
        raw = [e.returns[h][0] for e in events if h in e.returns]
        out[f"h{h}"] = {
            "excess": summarize(xs),
            "raw": summarize(raw),
            **clustered_by_month(events, h),
            "truncated": sum(1 for e in events if h in e.truncated),
        }
    return out


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    generator: Callable[[datetime], list[Signal]]


def default_variants(db: Database) -> list[Variant]:
    """The four variants declared before running the study (do not add after seeing results)."""
    grid = [
        ("A", "2+ insider, totale >= 100k USD (parametri di produzione)", InsiderClusterParams()),
        ("B", "3+ insider, totale >= 100k USD", InsiderClusterParams(min_insiders=3)),
        ("C", "2+ insider, totale >= 500k USD", InsiderClusterParams(min_total_value=Decimal("500000"))),
        (
            "D",
            "2+ insider, totale >= 100k USD, almeno un CEO/CFO/Presidente/Chair",
            InsiderClusterParams(require_senior_buyer=True),
        ),
    ]

    def make(params: InsiderClusterParams) -> Callable[[datetime], list[Signal]]:
        def generate(now: datetime) -> list[Signal]:
            return find_insider_clusters(db, now, params)

        return generate

    return [Variant(name, desc, make(params)) for name, desc, params in grid]


def run_study(
    variants: Sequence[Variant],
    trading_days: Sequence[date],
    prices: PriceData,
    cfg: StudyConfig,
    *,
    limits: RiskLimits | None = None,
    portfolio_horizon_days: int = 28,
) -> dict[str, Any]:
    days = [d for d in trading_days if cfg.start <= d <= cfg.end]
    split_dt = datetime.combine(cfg.split, time(0), tzinfo=NEW_YORK).astimezone(UTC)
    builder = EventBuilder(prices, cfg)
    results: dict[str, Any] = {"config": {k: str(v) for k, v in cfg.__dict__.items()}, "variants": {}}
    timed_by_variant: dict[str, list[TimedSignal]] = {}
    for v in variants:
        timed = replay_signals(v.generator, days)
        timed_by_variant[v.name] = timed
        events, drops = builder.build(timed)
        ins = [e for e in events if e.available_at < split_dt]
        oos = [e for e in events if e.available_at >= split_dt]
        results["variants"][v.name] = {
            "description": v.description,
            "raw_signals": len(timed),
            "dropped": drops,
            "in_sample": report_block(ins, cfg),
            "out_of_sample": report_block(oos, cfg),
        }

    def score(name: str) -> float:
        block = results["variants"][name]["in_sample"][f"h{cfg.primary_horizon}"]
        return float(block.get("months_mean", float("-inf")))

    chosen = max((v.name for v in variants), key=score)
    results["chosen_variant"] = chosen
    results["selection_rule"] = (
        f"max in-sample month-clustered mean excess return at {cfg.primary_horizon} sessions"
    )

    # Realistic $1,000 portfolio with the production sizing/risk engine, chosen variant only.
    portfolio: dict[str, Any] = {}
    chosen_timed = timed_by_variant[chosen]
    for label, lo, hi in (
        ("in_sample", cfg.start, cfg.split - timedelta(days=1)),
        ("out_of_sample", cfg.split, cfg.end),
    ):
        sigs = [
            TimedSignal(t.signal.model_copy(update={"horizon_days": portfolio_horizon_days}), t.available_at)
            for t in chosen_timed
            if lo <= t.available_at.astimezone(NEW_YORK).date() <= hi
        ]
        last_price_day = max(
            d for d in builder.calendar if d <= hi + timedelta(days=portfolio_horizon_days * 2)
        )
        bt = Backtester(prices, limits=limits, initial_cash=Decimal("1000"), benchmark=cfg.benchmark)
        res = bt.run(sigs, lo, min(last_price_day, builder.calendar[-1]))
        portfolio[label] = {
            "metrics": res.metrics(),
            "trades": len(res.trades),
            "rejected": len(res.rejected),
        }
    results["portfolio_1000_usd"] = portfolio
    return results


def _pct(x: float | None) -> str:
    return "n/d" if x is None else f"{x * 100:+.2f}%"


def render_markdown(results: dict[str, Any]) -> str:
    cfg = results["config"]
    horizons = [int(h) for h in cfg["horizons"].strip("()").split(",") if h.strip()]
    primary = int(cfg["primary_horizon"])
    lines = [
        "# Fase 2 — Studio sugli acquisti in cluster degli insider",
        "",
        f"Periodo segnali: {cfg['start']} → {cfg['end']} · split in-sample/out-of-sample: {cfg['split']} · "
        f"benchmark: {cfg['benchmark']}",
        f"Filtri: prezzo ≥ {cfg['min_price']} USD, controvalore medio 20 sedute ≥ {cfg['min_adv']} USD, "
        f"un evento per titolo ogni {cfg['min_gap_days']} giorni.",
        f"Regola di selezione (fissata prima dei risultati): {results['selection_rule']}.",
        f"**Variante scelta: {results['chosen_variant']}**",
        "",
        "## Extra-rendimento medio per evento rispetto a SPY",
        "",
        "| Variante | Campione | Eventi | "
        + " | ".join(f"{h} sedute (media / mediana / % positivi / t mensile)" for h in horizons)
        + " |",
        "|---|---|---|" + "---|" * len(horizons),
    ]
    for name, v in results["variants"].items():
        for sample in ("in_sample", "out_of_sample"):
            block = v[sample]
            cells = []
            for h in horizons:
                b = block[f"h{h}"]
                ex = b["excess"]
                if not ex.get("n"):
                    cells.append("—")
                    continue
                t = b.get("months_t_stat")
                cells.append(
                    f"{_pct(ex['mean'])} / {_pct(ex['median'])} / {ex['hit_rate'] * 100:.0f}% / "
                    f"{'n/d' if t is None else f'{t:.2f}'}"
                )
            label = "IS" if sample == "in_sample" else "OOS"
            lines.append(f"| {name} | {label} | {block['events']} | " + " | ".join(cells) + " |")
    lines += ["", "Varianti:"]
    lines += [
        f"- **{n}**: {v['description']} (segnali grezzi {v['raw_signals']}, scartati {v['dropped']})"
        for n, v in results["variants"].items()
    ]
    lines += [
        "",
        f"## Portafoglio simulato da 1.000 USD (variante {results['chosen_variant']}, "
        "stesso dimensionamento e motore di rischio della produzione)",
        "",
        "| Campione | Operazioni | Rendimento | SPY | Differenza | Max drawdown | Sharpe | % positive |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for sample, p in results["portfolio_1000_usd"].items():
        m = p["metrics"]
        lines.append(
            f"| {'IS' if sample == 'in_sample' else 'OOS'} | {p['trades']} | {_pct(m.get('total_return'))} | "
            f"{_pct(m.get('benchmark_return'))} | {_pct(m.get('excess_return'))} | "
            f"{_pct(m.get('max_drawdown'))} | "
            f"{m.get('sharpe', 0):.2f} | {m.get('win_rate', 0) * 100:.0f}% |"
        )
    chosen = results["variants"][results["chosen_variant"]]
    oos = chosen["out_of_sample"][f"h{primary}"]
    lines += ["", "## Lettura", ""]
    t = oos.get("months_t_stat")
    mean = oos["excess"].get("mean")
    if mean is None:
        lines.append("Nessun evento out-of-sample: conclusione impossibile.")
    elif t is not None and t >= 2 and mean > 0:
        lines.append(
            f"Out-of-sample l'extra-rendimento a {primary} sedute è positivo e statisticamente robusto "
            f"(t mensile {t:.2f}). Il segnale merita la fase 3 (paper trading)."
        )
    elif mean > 0:
        lines.append(
            f"Out-of-sample l'extra-rendimento a {primary} sedute è positivo ({_pct(mean)}) ma non "
            f"statisticamente robusto (t mensile {'n/d' if t is None else f'{t:.2f}'}): indizio, non prova."
        )
    else:
        lines.append(
            f"Out-of-sample l'extra-rendimento a {primary} sedute non è positivo ({_pct(mean)}): "
            "nessuna evidenza di vantaggio. Non procedere con denaro reale su questo segnale."
        )
    lines += [
        "",
        "Limiti: costi e slippage inclusi solo nel portafoglio simulato (non nello studio per evento); "
        "prezzi rettificati per split ma non per dividendi (anche per SPY); finestra di 2 anni; "
        "il t mensile tratta i mesi come indipendenti.",
        "",
    ]
    return "\n".join(lines)
