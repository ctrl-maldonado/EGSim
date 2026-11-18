"""Ablaufsteuerung: EGs bereitstellen, Positionen einreichen/ändern/stornieren, Markt schließen, prüfen."""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from egsim import CONTRACT_VERSION, __version__
from egsim.client import ApiClient, ApiResponse, RequestStats
from egsim.config import CommunityConfig, Config
from egsim.findings import Findings
from egsim.profiles import KWH, PlannedPosition, plan_positions, select_intervals
from egsim.settlement import D
from egsim.timeutil import normalize_interval, now_utc, parse_ts, quarter_hours, resolve_delivery_date

log = logging.getLogger("egsim")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class SimEG:
    key: str
    cfg: CommunityConfig
    id: str | None = None
    email: str | None = None
    api_key: str | None = None
    password: str | None = None
    client: ApiClient | None = None
    status: str | None = None

    @property
    def ready(self) -> bool:
        return self.client is not None and self.id is not None


@dataclass
class SubmittedPosition:
    eg: SimEG
    planned: PlannedPosition
    id: str | None = None
    status: str | None = None
    quantity: Decimal | None = None
    price_expectation: Decimal | None = None
    created_at: str | None = None
    updated: bool = False
    cancelled: bool = False


@dataclass
class MarketRun:
    delivery_date: str
    market: dict | None = None
    positions: list[SubmittedPosition] = field(default_factory=list)
    submitted: int = 0
    rejected_submissions: int = 0
    updates: int = 0
    cancels: int = 0
    close_status: int | None = None
    final_status: str | None = None
    verification: dict | None = None

    def summary(self) -> dict:
        m = self.market or {}
        return {
            "deliveryDate": self.delivery_date,
            "resolution": m.get("resolution"),
            "marketStatusAtStart": m.get("status"),
            "prices": m.get("prices"),
            "closesAt": m.get("closesAt"),
            "positionsPlanned": len(self.positions),
            "positionsSubmitted": self.submitted,
            "submissionsFailed": self.rejected_submissions,
            "updates": self.updates,
            "cancellations": self.cancels,
            "offersKwh": str(sum((p.quantity or 0 for p in self.positions if p.id and not p.cancelled and p.planned.type == "OFFER"), Decimal(0))),
            "demandKwh": str(sum((p.quantity or 0 for p in self.positions if p.id and not p.cancelled and p.planned.type == "DEMAND"), Decimal(0))),
            "closeHttpStatus": self.close_status,
            "marketStatusAtEnd": self.final_status,
            "verification": self.verification,
        }


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "eg"


class Simulator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stats = RequestStats()
        self.findings = Findings()
        self.run_id = now_utc().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.seed = cfg.seed if cfg.seed is not None else random.randrange(1_000_000)
        self.base = ApiClient(cfg.base_url, stats=self.stats, timeout=cfg.timeout, verify_tls=cfg.verify_tls, label="anon")
        self.admin: ApiClient | None = None
        self.egs: list[SimEG] = []
        self.markets: list[MarketRun] = []
        self.check_results: list[dict] = []
        self._state: dict = {}
        self.started_at = now_utc()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ util
    def _inc(self, run: "MarketRun", attr: str) -> None:
        with self._lock:
            setattr(run, attr, getattr(run, attr) + 1)

    def _rng(self, *parts: Any) -> random.Random:
        return random.Random(":".join(str(p) for p in (self.seed, *parts)))

    def _sleep(self, rng: random.Random) -> None:
        lo, hi = self.cfg.delay_ms
        if hi > 0:
            time.sleep(rng.uniform(lo, hi) / 1000)

    def _expect(self, resp: ApiResponse, statuses: tuple[int, ...], area: str, what: str) -> bool:
        if resp.status in statuses:
            return True
        detail = resp.body if isinstance(resp.body, (dict, str)) else ""
        self.findings.error(area, f"{what}: erwartet HTTP {'/'.join(map(str, statuses))}, erhalten {resp.status} {detail}",
                            resp.short())
        return False

    # ------------------------------------------------------------ state file
    def _load_state(self) -> None:
        sf = self.cfg.state_file
        if not sf or not Path(sf).exists():
            return
        try:
            data = json.loads(Path(sf).read_text(encoding="utf-8"))
        except ValueError:
            self.findings.warning("provisioning", f"State-Datei {sf} ist kein gültiges JSON, wird ignoriert")
            return
        if data.get("baseUrl") == self.cfg.base_url:
            self._state = data.get("communities", {})

    def _save_state(self) -> None:
        sf = self.cfg.state_file
        if not sf or self.cfg.provisioning_mode != "register":
            return
        comms = dict(self._state)
        for eg in self.egs:
            if eg.id and eg.api_key:
                comms[eg.key] = {"id": eg.id, "email": eg.email, "apiKey": eg.api_key, "password": eg.password}
        Path(sf).parent.mkdir(parents=True, exist_ok=True)
        Path(sf).write_text(json.dumps({"baseUrl": self.cfg.base_url, "communities": comms}, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- admin/auth
    def _client_for(self, label: str, api_key: str | None, email: str | None, password: str | None,
                    prefer: str) -> tuple[ApiClient | None, ApiResponse | None]:
        if prefer == "bearer" and email and password or (not api_key and email and password):
            r = self.base.login(email, password)
            if r.status == 200 and isinstance(r.body, dict) and r.body.get("token"):
                return self.base.with_auth(token=r.body["token"], label=label), r
            return None, r
        if api_key:
            return self.base.with_auth(api_key=api_key, label=label), None
        return None, None

    def setup_admin(self) -> None:
        if not self.cfg.has_admin:
            log.info("Kein Admin-Zugang konfiguriert – Admin-Schritte werden übersprungen")
            return
        client, login = self._client_for("admin", self.cfg.admin_api_key, self.cfg.admin_email, self.cfg.admin_password,
                                         "apiKey" if self.cfg.admin_api_key else "bearer")
        if client is None:
            self.findings.error("admin", "Admin-Login fehlgeschlagen", login.short() if login else None)
            return
        me = client.me()
        if me.status != 200 or not isinstance(me.body, dict) or me.body.get("role") != "ADMIN":
            self.findings.error("admin", f"GET /me liefert für Admin-Zugang nicht role=ADMIN: {me.body}", me.short())
            return
        self.admin = client
        log.info("Admin-Zugang ok")

    # ----------------------------------------------------------- provisioning
    def provision(self) -> None:
        self._load_state()
        for c in self.cfg.communities:
            for n in range(1, c.count + 1):
                key = c.name if c.count == 1 else f"{c.name} #{n}"
                eg = SimEG(key=key, cfg=c)
                self.egs.append(eg)
        with ThreadPoolExecutor(self.cfg.concurrency) as ex:
            list(ex.map(self._provision_one, self.egs))
        self._save_state()
        ready = sum(1 for e in self.egs if e.ready)
        log.info("%d/%d EGs einsatzbereit", ready, len(self.egs))
        if ready == 0:
            self.findings.error("provisioning", "Keine EG einsatzbereit – Lauf kann keine Positionen einreichen")

    def _provision_one(self, eg: SimEG) -> None:
        if self.cfg.provisioning_mode == "existing":
            eg.api_key, eg.email, eg.password = eg.cfg.api_key, eg.cfg.email, eg.cfg.password
            self._attach_client(eg)
            return
        st = self._state.get(eg.key)
        if st:
            eg.id, eg.email, eg.api_key, eg.password = st.get("id"), st.get("email"), st.get("apiKey"), st.get("password")
            if self._attach_client(eg, quiet=True):
                log.info("[%s] aus State-Datei wiederverwendet (%s)", eg.key, eg.id)
                return
            log.info("[%s] Zugang aus State-Datei ungültig – registriere neu bzw. vergebe neue Zugangsdaten", eg.key)
            if eg.id and self.admin and self.admin.get_community(eg.id).status == 200:
                self._activate_and_issue(eg)
                return
        self._register(eg)

    def _register(self, eg: SimEG) -> None:
        eg.email = f"egsim-{_slug(eg.key)}-{self.run_id}@{self.cfg.email_domain}"
        r = self.base.register(eg.email, eg.key[:120], eg.cfg.description or "Simulierte EG (EGSim)")
        if not self._expect(r, (201,), "provisioning", f"[{eg.key}] Registrierung"):
            return
        body = r.body or {}
        eg.id = body.get("id")
        if body.get("status") != "PENDING":
            self.findings.error("provisioning", f"[{eg.key}] neue EG hat Status {body.get('status')}, erwartet PENDING", r.short())
        if not eg.id or not UUID_RE.match(str(eg.id)):
            self.findings.error("provisioning", f"[{eg.key}] Registrierung liefert keine gültige UUID als id", r.short())
            return
        self._activate_and_issue(eg)

    def _activate_and_issue(self, eg: SimEG) -> None:
        if not self.admin:
            self.findings.error("provisioning", f"[{eg.key}] kein Admin-Zugang zum Freischalten")
            return
        r = self.admin.set_status(eg.id, "ACTIVE")
        if self._expect(r, (200,), "provisioning", f"[{eg.key}] Freischalten") and (r.body or {}).get("status") != "ACTIVE":
            self.findings.error("provisioning", f"[{eg.key}] Status nach Freischalten {(r.body or {}).get('status')}", r.short())
        r = self.admin.issue_credentials(eg.id)
        if not self._expect(r, (201,), "provisioning", f"[{eg.key}] Zugangsdaten vergeben"):
            return
        eg.api_key, eg.password = (r.body or {}).get("apiKey"), (r.body or {}).get("password")
        if not eg.api_key or not eg.password:
            self.findings.error("provisioning", f"[{eg.key}] Antwort enthält nicht apiKey und password", r.short())
        elif len(eg.password) < 8:
            self.findings.warning("provisioning", f"[{eg.key}] generiertes Passwort kürzer als 8 Zeichen")
        self._attach_client(eg)

    def _attach_client(self, eg: SimEG, quiet: bool = False) -> bool:
        client, login = self._client_for(eg.key, eg.api_key, eg.email, eg.password, self.cfg.auth)
        if client is None:
            if not quiet:
                self.findings.error("provisioning", f"[{eg.key}] Anmeldung fehlgeschlagen", login.short() if login else None)
            return False
        me = client.me()
        if me.status != 200:
            if not quiet:
                self.findings.error("provisioning", f"[{eg.key}] GET /me fehlgeschlagen", me.short())
            return False
        body = me.body or {}
        ec = body.get("energyCommunity") or {}
        if body.get("role") != "ENERGY_COMMUNITY":
            self.findings.error("provisioning", f"[{eg.key}] GET /me liefert role={body.get('role')}", me.short())
            return False
        if eg.id and ec.get("id") != eg.id:
            self.findings.error("provisioning", f"[{eg.key}] GET /me liefert fremde EG {ec.get('id')}", me.short())
            return False
        eg.id = ec.get("id") or eg.id
        eg.status = ec.get("status")
        eg.client = client
        return True

    # ---------------------------------------------------------------- market
    def run_market(self, delivery: str) -> MarketRun:
        run = MarketRun(delivery)
        self.markets.append(run)
        reader = self.admin or next((e.client for e in self.egs if e.ready), None)
        if reader is None:
            self.findings.error("market", f"{delivery}: kein angemeldeter Client verfügbar")
            return run
        r = reader.get_market(delivery)
        if r.status == 404:
            self.findings.error("market", f"Für {delivery} existiert kein Markt (404). Der Betreiber muss ihn anlegen.", r.short())
            return run
        if not self._expect(r, (200,), "market", f"Markt {delivery} abrufen"):
            return run
        run.market = m = r.body
        res = m.get("resolution")
        if res not in ("DAY", "QUARTER_HOUR"):
            self.findings.error("market", f"{delivery}: unbekannte resolution {res}", r.short())
            return run
        log.info("Markt %s: %s, Status %s, Preise %s, closesAt %s", delivery, res, m.get("status"),
                 {k: str(v) for k, v in (m.get("prices") or {}).items() if k != "deliveryDate"}, m.get("closesAt"))

        closes_at = parse_ts(m["closesAt"]) if m.get("closesAt") else None
        open_for_orders = m.get("status") == "OPEN" and (closes_at is None or now_utc() < closes_at)
        if open_for_orders:
            self._submit_all(run)
            self._update_some(run)
            self._cancel_some(run)
        else:
            self.findings.info("market", f"{delivery}: Markt nimmt keine Positionen an (Status {m.get('status')}, "
                                         f"closesAt {m.get('closesAt')}) – Einreichen übersprungen")

        from egsim import checks  # lazy, um Zyklen zu vermeiden
        if self.cfg.checks and open_for_orders:
            self.check_results += checks.run_open_market_checks(self, run)

        if self.cfg.close_market and self.admin and m.get("status") in ("OPEN", "CLOSED", "FAILED"):
            self._close(run)
        else:
            cur = reader.get_market(delivery)
            run.final_status = (cur.body or {}).get("status") if cur.status == 200 else None

        if run.final_status in ("CLOSED", "SETTLED", "FAILED") and self.cfg.checks:
            self.check_results += checks.run_closed_market_checks(self, run)

        if run.final_status == "SETTLED" and self.cfg.verify_results:
            from egsim.verify import verify_market
            run.verification = verify_market(self, run)
        return run

    def _plan(self, run: MarketRun) -> None:
        res = run.market["resolution"]
        d = resolve_delivery_date(run.delivery_date)
        all_iv = quarter_hours(d) if res == "QUARTER_HOUR" else None
        for eg in self.egs:
            if not eg.ready:
                continue
            rng = self._rng(eg.key, run.delivery_date)
            ivs = select_intervals(all_iv, self.cfg.quarter_hour_intervals, rng) if all_iv else None
            for p in plan_positions(eg.cfg, res, ivs, all_iv, rng):
                run.positions.append(SubmittedPosition(eg=eg, planned=p))
        log.info("%s: %d Positionen geplant (%d EGs)", run.delivery_date, len(run.positions),
                 sum(1 for e in self.egs if e.ready))

    def _submit_all(self, run: MarketRun) -> None:
        self._plan(run)
        with ThreadPoolExecutor(self.cfg.concurrency) as ex:
            list(ex.map(lambda sp: self._submit(run, sp), run.positions))
        log.info("%s: %d/%d Positionen angenommen", run.delivery_date, run.submitted, len(run.positions))

    def _submit(self, run: MarketRun, sp: SubmittedPosition) -> None:
        rng = self._rng("delay", sp.eg.key, sp.planned.interval_start)
        self._sleep(rng)
        body = sp.planned.body(run.delivery_date)
        r = sp.eg.client.submit_position(body)
        if r.status == 409 and r.code == "POSITION_ALREADY_EXISTS":
            self._handle_existing(run, sp, r)
            return
        if not self._expect(r, (201,), "submit", f"[{sp.eg.key}] Position einreichen"):
            self._inc(run, "rejected_submissions")
            return
        self._accept(run, sp, r.body or {}, r)

    def _accept(self, run: MarketRun, sp: SubmittedPosition, b: dict, r: ApiResponse, expected_status: str = "OPEN") -> None:
        sp.id, sp.status, sp.created_at = b.get("id"), b.get("status"), b.get("createdAt")
        sp.quantity = D(b.get("quantityKwh"))
        sp.price_expectation = D(b.get("priceExpectationEurPerKwh"))
        self._inc(run, "submitted")
        self.validate_position_body(sp, b, run.delivery_date, r, expected_status)

    def _handle_existing(self, run: MarketRun, sp: SubmittedPosition, r: ApiResponse) -> None:
        if self.cfg.on_existing_position == "skip":
            self.findings.info("submit", f"[{sp.eg.key}] Position existiert bereits für {run.delivery_date} "
                                         f"{sp.planned.interval_start or ''} – übersprungen", r.short())
            return
        lst = sp.eg.client.list_positions(deliveryDate=run.delivery_date, intervalStart=sp.planned.interval_start, status="OPEN")
        existing = (lst.body or []) if lst.status == 200 else []
        if not existing:
            self.findings.warning("submit", f"[{sp.eg.key}] 409 POSITION_ALREADY_EXISTS, aber keine OPEN-Position gefunden",
                                  lst.short())
            return
        pos = existing[0]
        if pos.get("type") != sp.planned.type:
            # Typ lässt sich per PUT nicht ändern: stornieren und neu einreichen
            c = sp.eg.client.cancel_position(pos["id"])
            if c.status == 200:
                r2 = sp.eg.client.submit_position(sp.planned.body(run.delivery_date))
                if self._expect(r2, (201,), "submit", f"[{sp.eg.key}] Neu einreichen nach Storno"):
                    self._accept(run, sp, r2.body or {}, r2)
            return
        body = {"quantityKwh": sp.planned.quantity}
        if sp.planned.price_expectation is not None:
            body["priceExpectationEurPerKwh"] = sp.planned.price_expectation
        u = sp.eg.client.update_position(pos["id"], body)
        if self._expect(u, (200,), "submit", f"[{sp.eg.key}] bestehende Position aktualisieren"):
            self._accept(run, sp, u.body or {}, u)
            sp.updated = True

    def validate_position_body(self, sp: SubmittedPosition, b: dict, delivery: str, r: ApiResponse,
                               expected_status: str) -> None:
        who = f"[{sp.eg.key}]"
        problems = []
        if not b.get("id") or not UUID_RE.match(str(b.get("id"))):
            problems.append("id ist keine UUID")
        if b.get("energyCommunityId") != sp.eg.id:
            problems.append(f"energyCommunityId={b.get('energyCommunityId')} statt {sp.eg.id}")
        if b.get("deliveryDate") != delivery:
            problems.append(f"deliveryDate={b.get('deliveryDate')}")
        if "intervalStart" not in b:
            problems.append("intervalStart fehlt (Pflichtfeld, im Tagesmarkt null)")
        elif normalize_interval(b.get("intervalStart")) != sp.planned.interval_start:
            problems.append(f"intervalStart={b.get('intervalStart')} statt {sp.planned.interval_start}")
        if b.get("type") != sp.planned.type:
            problems.append(f"type={b.get('type')}")
        if b.get("status") != expected_status:
            problems.append(f"status={b.get('status')} statt {expected_status}")
        if not b.get("createdAt") or not b.get("updatedAt"):
            problems.append("createdAt/updatedAt fehlen")
        if problems:
            self.findings.error("response", f"{who} Positionsantwort weicht ab: " + "; ".join(problems), r.short())

    def _update_some(self, run: MarketRun) -> None:
        if self.cfg.update_probability <= 0:
            return
        pct = Decimal(str(self.cfg.update_change_percent)) / 100
        todo = []
        for sp in run.positions:
            rng = self._rng("update", sp.eg.key, sp.planned.interval_start, run.delivery_date)
            if sp.id and sp.status == "OPEN" and rng.random() < self.cfg.update_probability:
                factor = 1 + pct * Decimal(str(rng.uniform(-1, 1)))
                q = max(KWH, (sp.quantity * factor).quantize(KWH, rounding=ROUND_HALF_UP))
                body = {"quantityKwh": q}
                if sp.planned.type == "OFFER" and sp.price_expectation is not None:
                    body["priceExpectationEurPerKwh"] = sp.price_expectation
                todo.append((sp, body))

        def do(item):
            sp, body = item
            r = sp.eg.client.update_position(sp.id, body)
            if self._expect(r, (200,), "update", f"[{sp.eg.key}] Position ändern"):
                b = r.body or {}
                if D(b.get("quantityKwh")) != body["quantityKwh"]:
                    self.findings.error("update", f"[{sp.eg.key}] quantityKwh nach PUT {b.get('quantityKwh')} "
                                                  f"statt {body['quantityKwh']}", r.short())
                if b.get("createdAt") != sp.created_at:
                    self.findings.warning("update", f"[{sp.eg.key}] createdAt hat sich durch PUT geändert "
                                                    "(relevant für Rundungsreihenfolge)", r.short())
                sp.quantity = D(b.get("quantityKwh"))
                sp.updated = True
                self._inc(run, "updates")

        with ThreadPoolExecutor(self.cfg.concurrency) as ex:
            list(ex.map(do, todo))

    def _cancel_some(self, run: MarketRun) -> None:
        if self.cfg.cancel_probability <= 0:
            return
        todo = [sp for sp in run.positions if sp.id and sp.status == "OPEN"
                and self._rng("cancel", sp.eg.key, sp.planned.interval_start, run.delivery_date).random()
                < self.cfg.cancel_probability]

        def do(sp: SubmittedPosition):
            r = sp.eg.client.cancel_position(sp.id)
            if self._expect(r, (200,), "cancel", f"[{sp.eg.key}] Position stornieren"):
                if (r.body or {}).get("status") != "CANCELLED":
                    self.findings.error("cancel", f"[{sp.eg.key}] Status nach Storno {(r.body or {}).get('status')}", r.short())
                sp.cancelled, sp.status = True, "CANCELLED"
                self._inc(run, "cancels")

        with ThreadPoolExecutor(self.cfg.concurrency) as ex:
            list(ex.map(do, todo))

    def _close(self, run: MarketRun) -> None:
        r = self.admin.close_market(run.delivery_date)
        run.close_status = r.status
        if not self._expect(r, (200, 202), "close", f"Markt {run.delivery_date} schließen"):
            return
        status = (r.body or {}).get("status")
        if r.status == 200 and status != "SETTLED":
            self.findings.error("close", f"close antwortet 200, Status aber {status} (erwartet SETTLED)", r.short())
        if r.status == 202 and status != "CLOSED":
            self.findings.error("close", f"close antwortet 202, Status aber {status} (erwartet CLOSED)", r.short())
        deadline = time.monotonic() + self.cfg.settlement_timeout
        while status not in ("SETTLED", "FAILED") and time.monotonic() < deadline:
            time.sleep(1)
            g = self.admin.get_market(run.delivery_date)
            status = (g.body or {}).get("status") if g.status == 200 else status
        run.final_status = status
        if status == "FAILED":
            g = self.admin.get_market(run.delivery_date)
            self.findings.error("close", f"Settlement FAILED: {(g.body or {}).get('failureReason')}")
        elif status != "SETTLED":
            self.findings.error("close", f"Settlement nach {self.cfg.settlement_timeout:.0f}s nicht abgeschlossen (Status {status})")
        else:
            log.info("Markt %s abgewickelt (SETTLED)", run.delivery_date)

    # ------------------------------------------------------------------- run
    def run(self) -> dict:
        h = self.base.health_ready()
        if h.status != 200:
            self.findings.warning("health", f"/health/ready liefert {h.status}", h.short())
        self.setup_admin()
        self.provision()
        for spec in self.cfg.delivery_dates:
            try:
                d = resolve_delivery_date(spec).isoformat()
            except ValueError as exc:
                self.findings.error("config", str(exc))
                continue
            self.run_market(d)
        if self.cfg.checks and self.cfg.lifecycle_checks and self.admin and self.cfg.provisioning_mode == "register":
            from egsim import checks
            self.check_results += checks.run_lifecycle_checks(self)
        return self.report()

    def report(self) -> dict:
        failed_checks = [c for c in self.check_results if c["result"] == "FAIL"]
        return {
            "egsimVersion": __version__,
            "contractVersion": CONTRACT_VERSION,
            "runId": self.run_id,
            "seed": self.seed,
            "baseUrl": self.cfg.base_url,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": now_utc().isoformat(),
            "communities": [{"name": e.key, "id": e.id, "ready": e.ready} for e in self.egs],
            "markets": [m.summary() for m in self.markets],
            "checks": self.check_results,
            "findings": self.findings.as_list(),
            "requests": self.stats.summary(),
            "totals": {
                "errors": self.findings.count("error"),
                "warnings": self.findings.count("warning"),
                "checksPassed": sum(1 for c in self.check_results if c["result"] == "PASS"),
                "checksFailed": len(failed_checks),
                "checksSkipped": sum(1 for c in self.check_results if c["result"] == "SKIP"),
                "requests": sum(x["requests"] for x in self.stats.summary()),
            },
        }
