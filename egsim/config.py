"""Konfiguration. Quelle ist ein JSON-Dokument, das die Web-UI bearbeitet und speichert (egsim-ui.json).

Der Simulator hat keinen Admin-Zugang: jede EG braucht eigene Zugangsdaten (API-Key oder E-Mail/Passwort),
die der Admin der Plattform nach der Freischaltung manuell übergibt.
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

PROFILES = ("solar", "wind", "flat", "household", "business", "night")

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _expand(value: Any) -> Any:
    """${VAR} und ${VAR:-default} aus der Umgebung ersetzen (praktisch für CI)."""
    if isinstance(value, str):
        def rep(m: re.Match) -> str:
            v = os.environ.get(m.group(1))
            return v if v is not None else (m.group(2) or "")
        out = _ENV.sub(rep, value)
        if out == "" and _ENV.fullmatch(value):
            return None
        return out
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _range(raw: Any, where: str) -> tuple[Decimal, Decimal]:
    try:
        if isinstance(raw, (int, float, str)) and not isinstance(raw, bool):
            v = Decimal(str(raw))
            return v, v
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            lo, hi = Decimal(str(raw[0])), Decimal(str(raw[1]))
        elif isinstance(raw, dict) and "min" in raw and "max" in raw:
            lo, hi = Decimal(str(raw["min"])), Decimal(str(raw["max"]))
        else:
            raise ConfigError(f"{where}: erwartet Zahl oder [min, max]")
    except ArithmeticError:
        raise ConfigError(f"{where}: keine gültige Zahl")
    if lo > hi:
        raise ConfigError(f"{where}: min ist größer als max")
    return lo, hi


@dataclass
class EnergyFlow:
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
    uid: str
    name: str
    enabled: bool = True
    description: str | None = None
    email: str | None = None
    auth: str = "apiKey"  # apiKey | bearer
    api_key: str | None = None
    password: str | None = None
    energy_community_id: str | None = None
    generation: EnergyFlow | None = None
    consumption: EnergyFlow | None = None
    price_expectation: PriceExpectation | None = None

    @property
    def has_credentials(self) -> bool:
        if self.auth == "bearer":
            return bool(self.email and self.password)
        return bool(self.api_key)


@dataclass
class Config:
    base_url: str = "http://localhost:8080"
    timeout: float = 10.0
    verify_tls: bool = True

    seed: int | None = None
    delivery_dates: list[str] = field(default_factory=lambda: ["tomorrow"])
    concurrency: int = 4
    delay_ms: tuple[int, int] = (0, 0)
    submit_positions: bool = True
    wait_for_settlement_minutes: float = 0.0
    verify_results: bool = True
    all_participants_simulated: bool = False
    checks: bool = True
    registration_checks: bool = False
    on_existing_position: str = "skip"
    report_dir: str | None = "reports"
    fail_on_findings: bool = True
    tolerance_kwh: Decimal = Decimal("0")
    tolerance_eur: Decimal = Decimal("0")

    update_probability: float = 0.0
    update_change_percent: float = 20.0
    cancel_probability: float = 0.0
    quarter_hour_intervals: str | int = "all"

    communities: list[CommunityConfig] = field(default_factory=list)

    @property
    def active_communities(self) -> list[CommunityConfig]:
        return [c for c in self.communities if c.enabled]


def default_config_dict() -> dict:
    return {
        "platform": {"baseUrl": "http://localhost:8080", "timeoutSeconds": 10, "verifyTls": True},
        "run": {
            "seed": 42, "deliveryDates": ["tomorrow"], "concurrency": 4, "delayMs": [0, 0],
            "submitPositions": True, "waitForSettlementMinutes": 0, "verifyResults": True,
            "allParticipantsSimulated": False, "checks": True, "registrationChecks": False,
            "onExistingPosition": "skip", "reportDir": "reports", "failOnFindings": True,
            "tolerance": {"kwh": 0, "eur": 0},
        },
        "behaviour": {"updateProbability": 0.2, "updateChangePercent": 20, "cancelProbability": 0.05,
                      "quarterHour": {"intervals": "all"}},
        "communities": [new_community_dict("EG Sonnenfeld", generation={"profile": "solar", "dailyKwh": [80, 150]},
                                           priceExpectation={"probability": 0.8, "range": [0.07, 0.11], "decimals": 3})],
    }


def new_community_dict(name: str = "Neue EG", **extra) -> dict:
    d = {"uid": uuid.uuid4().hex[:12], "name": name, "enabled": True, "description": "Simulierte EG (EGSim)",
         "email": "", "auth": "apiKey", "apiKey": "", "password": "", "energyCommunityId": "",
         "generation": None, "consumption": None, "priceExpectation": None}
    d.update(extra)
    return d


def _flow(raw: Any, where: str) -> EnergyFlow | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: erwartet Objekt mit profile und dailyKwh")
    profile = raw.get("profile", "flat")
    if profile not in PROFILES:
        raise ConfigError(f"{where}: Profil '{profile}' unbekannt (erlaubt: {', '.join(PROFILES)})")
    rng = _range(raw.get("dailyKwh"), f"{where} Tagesmenge")
    if rng[0] < 0:
        raise ConfigError(f"{where}: Tagesmenge darf nicht negativ sein")
    return EnergyFlow(profile=profile, daily_kwh=rng, noise=float(raw.get("noise", 0.15)))


def _community(raw: dict, i: int) -> CommunityConfig:
    name = (raw or {}).get("name") or ""
    where = f"EG '{name}'" if name else f"EG Nr. {i + 1}"
    if not isinstance(raw, dict) or not name.strip():
        raise ConfigError(f"{where}: Name fehlt")
    pe_raw = raw.get("priceExpectation")
    pe = None
    if pe_raw:
        pe = PriceExpectation(
            probability=float(pe_raw.get("probability", 1.0)),
            range=_range(pe_raw.get("range", [0.08, 0.12]), f"{where} Preisvorstellung"),
            decimals=int(pe_raw.get("decimals", 3)),
        )
        if not 0 <= pe.decimals <= 5:
            raise ConfigError(f"{where}: Nachkommastellen der Preisvorstellung 0 bis 5")
        if not 0 <= pe.probability <= 1:
            raise ConfigError(f"{where}: Wahrscheinlichkeit der Preisvorstellung 0 bis 1")
    auth = raw.get("auth") or "apiKey"
    if auth not in ("apiKey", "bearer"):
        raise ConfigError(f"{where}: Anmeldung apiKey oder bearer")
    c = CommunityConfig(
        uid=str(raw.get("uid") or uuid.uuid4().hex[:12]),
        name=name.strip(),
        enabled=bool(raw.get("enabled", True)),
        description=raw.get("description") or None,
        email=raw.get("email") or None,
        auth=auth,
        api_key=raw.get("apiKey") or None,
        password=raw.get("password") or None,
        energy_community_id=raw.get("energyCommunityId") or None,
        generation=_flow(raw.get("generation"), f"{where} Erzeugung"),
        consumption=_flow(raw.get("consumption"), f"{where} Verbrauch"),
        price_expectation=pe,
    )
    if c.enabled and not c.generation and not c.consumption:
        raise ConfigError(f"{where}: Erzeugung und/oder Verbrauch angeben")
    return c


def parse_config(raw: dict) -> Config:
    raw = _expand(copy.deepcopy(raw or {}))
    cfg = Config()
    plat = raw.get("platform") or {}
    cfg.base_url = (plat.get("baseUrl") or cfg.base_url).strip()
    if not re.match(r"^https?://", cfg.base_url):
        raise ConfigError("Plattform-URL muss mit http:// oder https:// beginnen")
    cfg.timeout = float(plat.get("timeoutSeconds", cfg.timeout))
    cfg.verify_tls = bool(plat.get("verifyTls", cfg.verify_tls))

    run = raw.get("run") or {}
    seed = run.get("seed")
    cfg.seed = int(seed) if seed not in (None, "") else None
    dd = run.get("deliveryDates", cfg.delivery_dates)
    cfg.delivery_dates = [str(x).strip() for x in (dd if isinstance(dd, list) else [dd]) if str(x).strip()]
    if not cfg.delivery_dates:
        raise ConfigError("Mindestens einen Liefertag angeben")
    from egsim.timeutil import resolve_delivery_date
    for d in cfg.delivery_dates:
        try:
            resolve_delivery_date(d)
        except ValueError as exc:
            raise ConfigError(str(exc))
    cfg.concurrency = max(1, int(run.get("concurrency", cfg.concurrency)))
    dl = _range(run.get("delayMs", 0), "Verzögerung")
    cfg.delay_ms = (int(dl[0]), int(dl[1]))
    cfg.submit_positions = bool(run.get("submitPositions", cfg.submit_positions))
    cfg.wait_for_settlement_minutes = float(run.get("waitForSettlementMinutes") or 0)
    cfg.verify_results = bool(run.get("verifyResults", cfg.verify_results))
    cfg.all_participants_simulated = bool(run.get("allParticipantsSimulated", cfg.all_participants_simulated))
    cfg.checks = bool(run.get("checks", cfg.checks))
    cfg.registration_checks = bool(run.get("registrationChecks", cfg.registration_checks))
    cfg.on_existing_position = run.get("onExistingPosition", cfg.on_existing_position)
    if cfg.on_existing_position not in ("skip", "update"):
        raise ConfigError("Bestehende Position: skip oder update")
    cfg.report_dir = run.get("reportDir", cfg.report_dir) or None
    cfg.fail_on_findings = bool(run.get("failOnFindings", cfg.fail_on_findings))
    tol = run.get("tolerance") or {}
    cfg.tolerance_kwh = Decimal(str(tol.get("kwh") or 0))
    cfg.tolerance_eur = Decimal(str(tol.get("eur") or 0))

    beh = raw.get("behaviour") or {}
    cfg.update_probability = float(beh.get("updateProbability", cfg.update_probability))
    cfg.update_change_percent = float(beh.get("updateChangePercent", cfg.update_change_percent))
    cfg.cancel_probability = float(beh.get("cancelProbability", cfg.cancel_probability))
    for name, v in (("Änderungsanteil", cfg.update_probability), ("Stornoanteil", cfg.cancel_probability)):
        if not 0 <= v <= 1:
            raise ConfigError(f"{name} muss zwischen 0 und 1 liegen")
    qh = (beh.get("quarterHour") or {}).get("intervals", cfg.quarter_hour_intervals)
    if isinstance(qh, str) and qh.isdigit():
        qh = int(qh)
    if not (qh in ("all", "daylight") or (isinstance(qh, int) and qh > 0)):
        raise ConfigError("15-Minuten-Intervalle: all, daylight oder positive Zahl")
    cfg.quarter_hour_intervals = qh

    cfg.communities = [_community(c, i) for i, c in enumerate(raw.get("communities") or [])]
    names = [c.name for c in cfg.communities]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ConfigError(f"EG-Namen müssen eindeutig sein: {', '.join(sorted(dup))}")
    return cfg


def load_config_dict(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Konfigurationsdatei {p} nicht gefunden")
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml  # optional
        return yaml.safe_load(text) or {}
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{p}: kein gültiges JSON ({exc})")


def load_config(path: str | Path) -> Config:
    return parse_config(load_config_dict(path))
