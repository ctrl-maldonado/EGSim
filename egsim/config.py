"""Konfiguration aus YAML laden. ${VAR} und ${VAR:-default} werden aus der Umgebung ersetzt."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

PROFILES = ("solar", "wind", "flat", "household", "business", "night")

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def rep(m: re.Match) -> str:
            v = os.environ.get(m.group(1))
            if v is None:
                return m.group(2) if m.group(2) is not None else ""
            return v
        out = _ENV.sub(rep, value)
        if out == "" and _ENV.fullmatch(value):
            return None  # nicht gesetzte Variable ohne Default
        return out
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _range(raw: Any, where: str, *, allow_none: bool = False) -> tuple[Decimal, Decimal] | None:
    if raw is None:
        if allow_none:
            return None
        raise ConfigError(f"{where}: Wert fehlt")
    if isinstance(raw, (int, float, str)):
        v = Decimal(str(raw))
        return v, v
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        lo, hi = Decimal(str(raw[0])), Decimal(str(raw[1]))
        if lo > hi:
            raise ConfigError(f"{where}: min > max")
        return lo, hi
    if isinstance(raw, dict) and "min" in raw and "max" in raw:
        return _range([raw["min"], raw["max"]], where)
    raise ConfigError(f"{where}: erwartet Zahl, [min, max] oder {{min, max}}")


@dataclass
class EnergyFlow:
    """Erzeugung oder Verbrauch einer EG pro Liefertag."""
    profile: str
    daily_kwh: tuple[Decimal, Decimal]
    noise: float = 0.15


@dataclass
class PriceExpectation:
    probability: float = 1.0
    range: tuple[Decimal, Decimal] = (Decimal("0.08"), Decimal("0.12"))
    decimals: int = 3


@dataclass
class CommunityConfig:
    name: str
    count: int = 1
    description: str | None = None
    generation: EnergyFlow | None = None
    consumption: EnergyFlow | None = None
    price_expectation: PriceExpectation | None = None
    # nur provisioning.mode = existing
    api_key: str | None = None
    email: str | None = None
    password: str | None = None


@dataclass
class Config:
    base_url: str = "http://localhost:8080"
    timeout: float = 10.0
    verify_tls: bool = True

    admin_api_key: str | None = None
    admin_email: str | None = None
    admin_password: str | None = None

    seed: int | None = None
    delivery_dates: list[str] = field(default_factory=lambda: ["tomorrow"])
    concurrency: int = 4
    delay_ms: tuple[int, int] = (0, 0)
    close_market: bool = False
    settlement_timeout: float = 60.0
    verify_results: bool = True
    checks: bool = True
    lifecycle_checks: bool = True
    report_file: str | None = "reports/egsim-{timestamp}.json"
    on_existing_position: str = "skip"  # skip | update
    fail_on_findings: bool = True

    provisioning_mode: str = "register"  # register | existing
    auth: str = "apiKey"  # apiKey | bearer
    email_domain: str = "egsim.example"
    state_file: str | None = ".egsim-state.json"

    update_probability: float = 0.0
    update_change_percent: float = 20.0
    cancel_probability: float = 0.0
    quarter_hour_intervals: str | int = "all"  # all | daylight | <Anzahl>

    tolerance_kwh: Decimal = Decimal("0")
    tolerance_eur: Decimal = Decimal("0")

    communities: list[CommunityConfig] = field(default_factory=list)

    @property
    def has_admin(self) -> bool:
        return bool(self.admin_api_key or (self.admin_email and self.admin_password))


def _flow(raw: Any, where: str) -> EnergyFlow | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: erwartet Objekt mit profile und dailyKwh")
    profile = raw.get("profile", "flat")
    if profile not in PROFILES:
        raise ConfigError(f"{where}.profile: '{profile}' unbekannt (erlaubt: {', '.join(PROFILES)})")
    rng = _range(raw.get("dailyKwh"), f"{where}.dailyKwh")
    if rng[0] < 0:
        raise ConfigError(f"{where}.dailyKwh: darf nicht negativ sein")
    return EnergyFlow(profile=profile, daily_kwh=rng, noise=float(raw.get("noise", 0.15)))


def _community(raw: dict, i: int, mode: str) -> CommunityConfig:
    where = f"communities[{i}]"
    if not isinstance(raw, dict) or not raw.get("name"):
        raise ConfigError(f"{where}: name fehlt")
    pe_raw = raw.get("priceExpectation")
    pe = None
    if pe_raw:
        pe = PriceExpectation(
            probability=float(pe_raw.get("probability", 1.0)),
            range=_range(pe_raw.get("range", [0.08, 0.12]), f"{where}.priceExpectation.range"),
            decimals=int(pe_raw.get("decimals", 3)),
        )
        if not 0 <= pe.decimals <= 5:
            raise ConfigError(f"{where}.priceExpectation.decimals: 0 bis 5")
    c = CommunityConfig(
        name=str(raw["name"]),
        count=int(raw.get("count", 1)),
        description=raw.get("description"),
        generation=_flow(raw.get("generation"), f"{where}.generation"),
        consumption=_flow(raw.get("consumption"), f"{where}.consumption"),
        price_expectation=pe,
        api_key=raw.get("apiKey"),
        email=raw.get("email"),
        password=raw.get("password"),
    )
    if not c.generation and not c.consumption:
        raise ConfigError(f"{where}: generation und/oder consumption angeben")
    if c.count < 1:
        raise ConfigError(f"{where}.count: mindestens 1")
    if mode == "existing":
        if c.count != 1:
            raise ConfigError(f"{where}: bei provisioning.mode=existing ist count=1 Pflicht")
        if not (c.api_key or (c.email and c.password)):
            raise ConfigError(f"{where}: bei provisioning.mode=existing apiKey oder email+password angeben")
    return c


def load_config(path: str | Path, *, for_run: bool = True) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Konfigurationsdatei {p} nicht gefunden")
    raw = _expand(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    return parse_config(raw, for_run=for_run)


def parse_config(raw: dict, *, for_run: bool = True) -> Config:
    cfg = Config()
    plat = raw.get("platform") or {}
    cfg.base_url = plat.get("baseUrl", cfg.base_url)
    cfg.timeout = float(plat.get("timeoutSeconds", cfg.timeout))
    cfg.verify_tls = bool(plat.get("verifyTls", cfg.verify_tls))

    adm = raw.get("admin") or {}
    cfg.admin_api_key = adm.get("apiKey") or None
    cfg.admin_email = adm.get("email") or None
    cfg.admin_password = adm.get("password") or None

    run = raw.get("run") or {}
    cfg.seed = run.get("seed")
    dd = run.get("deliveryDates", cfg.delivery_dates)
    cfg.delivery_dates = [str(x) for x in (dd if isinstance(dd, list) else [dd])]
    cfg.concurrency = max(1, int(run.get("concurrency", cfg.concurrency)))
    dl = _range(run.get("delayMs", 0), "run.delayMs")
    cfg.delay_ms = (int(dl[0]), int(dl[1]))
    cfg.close_market = bool(run.get("closeMarket", cfg.close_market))
    cfg.settlement_timeout = float(run.get("settlementTimeoutSeconds", cfg.settlement_timeout))
    cfg.verify_results = bool(run.get("verifyResults", cfg.verify_results))
    cfg.checks = bool(run.get("checks", cfg.checks))
    cfg.lifecycle_checks = bool(run.get("lifecycleChecks", cfg.lifecycle_checks))
    cfg.report_file = run.get("reportFile", cfg.report_file)
    cfg.on_existing_position = run.get("onExistingPosition", cfg.on_existing_position)
    if cfg.on_existing_position not in ("skip", "update"):
        raise ConfigError("run.onExistingPosition: skip oder update")
    cfg.fail_on_findings = bool(run.get("failOnFindings", cfg.fail_on_findings))
    tol = run.get("tolerance") or {}
    cfg.tolerance_kwh = Decimal(str(tol.get("kwh", 0)))
    cfg.tolerance_eur = Decimal(str(tol.get("eur", 0)))

    prov = raw.get("provisioning") or {}
    cfg.provisioning_mode = prov.get("mode", cfg.provisioning_mode)
    if cfg.provisioning_mode not in ("register", "existing"):
        raise ConfigError("provisioning.mode: register oder existing")
    cfg.auth = prov.get("auth", cfg.auth)
    if cfg.auth not in ("apiKey", "bearer"):
        raise ConfigError("provisioning.auth: apiKey oder bearer")
    cfg.email_domain = prov.get("emailDomain", cfg.email_domain)
    cfg.state_file = prov.get("stateFile", cfg.state_file)

    beh = raw.get("behaviour") or raw.get("behavior") or {}
    cfg.update_probability = float(beh.get("updateProbability", cfg.update_probability))
    cfg.update_change_percent = float(beh.get("updateChangePercent", cfg.update_change_percent))
    cfg.cancel_probability = float(beh.get("cancelProbability", cfg.cancel_probability))
    qh = (beh.get("quarterHour") or {}).get("intervals", cfg.quarter_hour_intervals)
    if not (qh in ("all", "daylight") or (isinstance(qh, int) and qh > 0)):
        raise ConfigError("behaviour.quarterHour.intervals: all, daylight oder positive Zahl")
    cfg.quarter_hour_intervals = qh

    comms = raw.get("communities") or []
    if not comms:
        raise ConfigError("communities: mindestens eine EG konfigurieren")
    cfg.communities = [_community(c, i, cfg.provisioning_mode) for i, c in enumerate(comms)]

    if not for_run:
        return cfg
    if cfg.provisioning_mode == "register" and not cfg.has_admin:
        raise ConfigError("provisioning.mode=register braucht Admin-Zugang (admin.apiKey oder admin.email/password), "
                          "um EGs freizuschalten und Zugangsdaten zu vergeben")
    if cfg.close_market and not cfg.has_admin:
        raise ConfigError("run.closeMarket=true braucht Admin-Zugang")
    return cfg
