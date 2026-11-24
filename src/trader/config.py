"""Runtime settings (environment) and risk limits (versioned YAML file)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RiskLimits(BaseModel):
    """Hard limits enforced by the deterministic risk engine.

    Percentages are fractions of account equity (0.12 = 12%). Defaults are deliberately
    conservative for a small cash account; tune them in ``config/risk.yaml``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Sizing
    risk_per_trade_pct: Decimal = Decimal("0.01")
    max_position_pct: Decimal = Decimal("0.12")
    max_positions: int = 10
    max_gross_exposure_pct: Decimal = Decimal("1.0")
    min_conviction: Decimal = Decimal("0.5")
    default_stop_loss_pct: Decimal = Decimal("0.08")
    min_stop_loss_pct: Decimal = Decimal("0.02")
    max_stop_loss_pct: Decimal = Decimal("0.20")
    allow_fractional: bool = True
    qty_step: Decimal = Decimal("0.0001")
    limit_offset_pct: Decimal = Decimal("0.002")

    # Order sanity
    min_order_value: Decimal = Decimal("25")
    max_commission_pct: Decimal = Decimal("0.015")
    max_price_deviation_pct: Decimal = Decimal("0.03")
    max_spread_pct: Decimal = Decimal("0.02")
    max_quote_age_seconds: int = 900
    min_price: Decimal = Decimal("5")
    max_adv_pct: Decimal = Decimal("0.01")
    require_adv: bool = True
    max_orders_per_day: int = 20

    # Loss limits
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    max_drawdown_pct: Decimal = Decimal("0.15")

    # Universe
    whitelist: tuple[str, ...] = ()
    restricted: tuple[str, ...] = ()

    # Workflow
    signal_ttl_minutes: int = 240  # how long an approval request stays valid
    signal_max_age_hours: int = 120  # proposals older than this expire unreviewed (survives long weekends)

    @field_validator("whitelist", "restricted", mode="before")
    @classmethod
    def _upper_tickers(cls, v: object) -> tuple[str, ...]:
        if v is None:
            return ()
        if not isinstance(v, list | tuple | set | frozenset):
            raise ValueError("expected a list of tickers")
        return tuple(sorted({str(t).strip().upper() for t in v if str(t).strip()}))

    @model_validator(mode="after")
    def _coherent(self) -> RiskLimits:
        fractions = {
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_position_pct": self.max_position_pct,
            "min_conviction": self.min_conviction,
            "default_stop_loss_pct": self.default_stop_loss_pct,
            "min_stop_loss_pct": self.min_stop_loss_pct,
            "max_stop_loss_pct": self.max_stop_loss_pct,
            "limit_offset_pct": self.limit_offset_pct,
            "max_commission_pct": self.max_commission_pct,
            "max_price_deviation_pct": self.max_price_deviation_pct,
            "max_spread_pct": self.max_spread_pct,
            "max_adv_pct": self.max_adv_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
        }
        for name, value in fractions.items():
            if not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be within [0, 1], got {value}")
        if not Decimal("0") < self.max_gross_exposure_pct <= Decimal("1"):
            raise ValueError("max_gross_exposure_pct must be in (0, 1]: cash account, no leverage")
        if not self.min_stop_loss_pct <= self.default_stop_loss_pct <= self.max_stop_loss_pct:
            raise ValueError("default_stop_loss_pct must lie between min and max stop loss")
        if self.qty_step <= 0:
            raise ValueError("qty_step must be positive")
        if not self.allow_fractional and self.qty_step != Decimal("1"):
            raise ValueError("qty_step must be 1 when fractional shares are disabled")
        if self.max_positions < 1 or self.max_orders_per_day < 1:
            raise ValueError("max_positions and max_orders_per_day must be >= 1")
        return self

    def is_restricted(self, ticker: str) -> bool:
        return ticker.upper() in self.restricted

    def in_universe(self, ticker: str) -> bool:
        return not self.whitelist or ticker.upper() in self.whitelist


def load_restricted_list(path: Path) -> tuple[str, ...]:
    """One ticker per line; '#' starts a comment. Missing file means an empty list."""
    if not path.exists():
        return ()
    tickers: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip().upper()
        if line:
            tickers.add(line)
    return tuple(sorted(tickers))


def load_risk_limits(path: Path, restricted_path: Path | None = None) -> RiskLimits:
    data: dict[str, object] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: expected a mapping at top level")
        data = {str(k): v for k, v in loaded.items()}
    if restricted_path is not None:
        extra = load_restricted_list(restricted_path)
        existing = data.get("restricted") or []
        if not isinstance(existing, list):
            raise ValueError(f"{path}: 'restricted' must be a list")
        data["restricted"] = [*existing, *extra]
    # YAML floats -> Decimal via str to avoid binary float artefacts.
    for key, value in list(data.items()):
        if isinstance(value, float):
            data[key] = Decimal(str(value))
    return RiskLimits.model_validate(data)


class Settings(BaseSettings):
    """Environment-driven settings. Secrets live only in the environment / ``.env``."""

    model_config = SettingsConfigDict(env_prefix="TRADER_", env_file=".env", extra="ignore")

    db_url: str = Field(default=f"sqlite:///{PROJECT_ROOT / 'var' / 'trader.sqlite'}")
    risk_config_path: Path = PROJECT_ROOT / "config" / "risk.yaml"
    restricted_list_path: Path = PROJECT_ROOT / "config" / "restricted_list.txt"

    broker: Literal["sim", "ibkr"] = "sim"
    approval_mode: Literal["manual", "auto"] = "manual"
    allow_auto_execution: bool = False

    sim_initial_cash: Decimal = Decimal("1000")
    sim_slippage_bps: Decimal = Decimal("5")

    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002  # IB Gateway paper; live gateway is 4001
    ibkr_client_id: int = 17
    ibkr_account: str | None = None
    ibkr_market_data_type: int = 1  # 1 live, 3 delayed

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None

    sec_user_agent: str | None = None

    llm_enabled: bool = False
    llm_triage_model: str = "claude-haiku-5-5"
    llm_analysis_model: str = "claude-opus-5-5"
    llm_analysis_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    llm_max_filing_chars: int = 400_000

    @model_validator(mode="after")
    def _auto_needs_explicit_opt_in(self) -> Settings:
        if self.approval_mode == "auto" and not self.allow_auto_execution:
            raise ValueError(
                "approval_mode=auto requires TRADER_ALLOW_AUTO_EXECUTION=true (explicit opt-in)"
            )
        return self

    def risk_limits(self) -> RiskLimits:
        return load_risk_limits(self.risk_config_path, self.restricted_list_path)
