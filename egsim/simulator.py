"""Ablaufsteuerung aus Sicht der EGs: anmelden, Positionen einreichen/ändern/stornieren, auf Settlement warten, prüfen.

Der Simulator hat keinen Admin-Zugang. Er nutzt ausschließlich Endpoints, die die Rolle ENERGY_COMMUNITY darf.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
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


class Stopped(Exception):
    pass


@dataclass
class SimEG:
    key: str
    cfg: CommunityConfig
    id: str | None = None
    client: ApiClient | None = None
    status: str | None = None
    problem: str | None = None

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
    final_status: str | None = None
    verification: dict | None = None

    def summary(self) -> dict:
        m = self.market or {}
        live = [p for p in self.positions if p.id and not p.cancelled]
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
            "offersKwh": str(sum((p.quantity or 0 for p in live if p.planned.type == "OFFER"), Decimal(0))),
            "demandKwh": str(sum((p.quantity or 0 for p in live if p.planned.type == "DEMAND"), Decimal(0))),
            "marketStatusAtEnd": self.final_status,
            "verification": self.verification,
        }


class Simulator:
    def __init__(self, cfg: Config, *, stats: RequestStats | None = None, stop: threading.Event | None = None) -> None:
        self.cfg = cfg
        self.stats = stats or RequestStats()
        self.stop = stop or threading.Event()
        self.findings = Findings()
        self.run_id = now_utc().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.seed = cfg.seed if cfg.seed is not None else random.randrange(1_000_000)
        self.base = ApiClient(cfg.base_url, stats=self.stats, timeout=cfg.timeout, verify_tls=cfg.verify_tls, label="anonym")
        self.egs: list[SimEG] = []
        self.markets: list[MarketRun] = []
        self.check_results: list[dict] = []
        self.started_at = now_utc()
        self.mode = "run"
        self.stopped = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ util
    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Stopped()

    def _inc(self, run: MarketRun, attr: str) -> None:
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

    def _map(self, fn, items) -> None:
        def guarded(x):
            if not self.stop.is_set():
                fn(x)
        with ThreadPoolExecutor(self.cfg.concurrency) as ex:
            list(ex.map(guarded, items))
        self._check_stop()

    # ------------------------------------------------------------- EG-Zugang
    def connect(self) -> None:
        for c in self.cfg.active_communities:
            self.egs.append(SimEG(key=c.name, cfg=c))
        self._map(self._connect_one, self.egs)
        ready = sum(1 for e in self.egs if e.ready)
        log.info("%d/%d EGs angemeldet", ready, len(self.egs))
        if not self.egs:
            self.findings.error("setup", "Keine aktive EG konfiguriert")
        elif ready == 0:
            self.findings.error("setup", "Keine EG konnte sich anmelden – Zugangsdaten prüfen")

    def _connect_one(self, eg: SimEG) -> None:
        c = eg.cfg
        if not c.has_credentials:
            eg.problem = "keine Zugangsdaten"
            self.findings.warning("setup", f"[{eg.key}] keine Zugangsdaten hinterlegt – EG wird übersprungen. "
                                           "Nach der Registrierung schaltet der Admin der Plattform die EG frei und "
                                           "übergibt API-Key und Passwort.")
            return
        if c.auth == "bearer":
            r = self.base.login(c.email, c.password)
            if r.status != 200 or not isinstance(r.body, dict) or not r.body.get("token"):
                eg.problem = f"Login {r.status} {r.code or ''}".strip()
                self._login_problem(eg, r)
                return
            if r.body.get("tokenType") != "Bearer" or r.body.get("role") != "ENERGY_COMMUNITY":
                self.findings.error("response", f"[{eg.key}] Login-Antwort: tokenType={r.body.get('tokenType')}, "
                                                f"role={r.body.get('role')}", r.short())
            client = self.base.with_auth(token=r.body["token"], label=eg.key)
        else:
            client = self.base.with_auth(api_key=c.api_key, label=eg.key)
        me = client.me()
        if me.status != 200:
            eg.problem = f"GET /me {me.status} {me.code or ''}".strip()
            self._login_problem(eg, me)
            return
        body = me.body or {}
        ec = body.get("energyCommunity") or {}
        if body.get("role") != "ENERGY_COMMUNITY" or not ec.get("id"):
            eg.problem = f"role={body.get('role')}"
            self.findings.error("setup", f"[{eg.key}] GET /me liefert role={body.get('role')} ohne energyCommunity – "
                                         "der Simulator braucht EG-Zugangsdaten, keinen Admin-Key", me.short())
            return
        if c.energy_community_id and ec["id"] != c.energy_community_id:
            self.findings.warning("setup", f"[{eg.key}] Zugangsdaten gehören zu EG {ec['id']}, eingetragen ist "
                                           f"{c.energy_community_id}", me.short())
        if ec.get("status") != "ACTIVE":
            self.findings.error("response", f"[{eg.key}] GET /me liefert Status {ec.get('status')} statt 403 "
                                            "für eine nicht aktive EG", me.short())
        eg.id, eg.status, eg.client = ec["id"], ec.get("status"), client

    def _login_problem(self, eg: SimEG, r: ApiResponse) -> None:
        if r.code == "ENERGY_COMMUNITY_NOT_ACTIVE":
            self.findings.warning("setup", f"[{eg.key}] EG ist noch nicht freigeschaltet (PENDING oder SUSPENDED)", r.short())
        elif r.status == 401:
            self.findings.error("setup", f"[{eg.key}] Zugangsdaten ungültig ({r.code})", r.short())
        else:
            self.findings.error("setup", f"[{eg.key}] Anmeldung fehlgeschlagen", r.short())

    def reader(self) -> ApiClient | None:
        return next((e.client for e in self.egs if e.ready), None)

    # ---------------------------------------------------------------- market
    def run_market(self, delivery: str) -> MarketRun:
        run = MarketRun(delivery)
        self.markets.append(run)
        reader = self.reader()
        if reader is None:
            return run
        r = reader.get_market(delivery)
        if r.status == 404:
            self.findings.error("market", f"Für {delivery} existiert kein Markt (404 {r.code}). "
                                          "Der Betreiber muss ihn anlegen.", r.short())
            return run
        if not self._expect(r, (200,), "market", f"Markt {delivery} abrufen"):
            return run
        run.market = m = r.body
        self._validate_market(m, delivery, r)
        res = m.get("resolution")
        if res not in ("DAY", "QUARTER_HOUR"):
            return run
        log.info("Markt %s: %s, Status %s, Preise %s, closesAt %s", delivery, res, m.get("status"),
                 {k: str(v) for k, v in (m.get("prices") or {}).items() if k != "deliveryDate"}, m.get("closesAt"))

        closes_at = parse_ts(m["closesAt"]) if m.get("closesAt") else None
        open_for_orders = m.get("status") == "OPEN" and (closes_at is None or now_utc() < closes_at)
        if open_for_orders and self.cfg.submit_positions:
            self._submit_all(run)
            self._update_some(run)
            self._cancel_some(run)
        elif not open_for_orders:
            self.findings.info("market", f"{delivery}: Markt nimmt keine Positionen an (Status {m.get('status')}, "
                                         f"closesAt {m.get('closesAt')}) – Einreichen übersprungen")

        from egsim import checks
        if self.cfg.checks and open_for_orders:
            self._check_stop()
            self.check_results += checks.run_open_market_checks(self, run)

        run.final_status = m.get("status")
        if self.cfg.wait_for_settlement_minutes > 0 and run.final_status != "SETTLED":
            self._wait_for_settlement(run, reader)
        else:
            cur = reader.get_market(delivery)
            run.final_status = (cur.body or {}).get("status") if cur.status == 200 else run.final_status

        if run.final_status in ("CLOSED", "SETTLED", "FAILED") and self.cfg.checks:
            self.check_results += checks.run_closed_market_checks(self, run)
        if run.final_status == "SETTLED" and self.cfg.verify_results:
            from egsim.verify import verify_market
            run.verification = verify_market(self, run)
        elif self.cfg.verify_results:
            self.findings.info("verify", f"{delivery}: Markt ist {run.final_status} – Ergebnisprüfung erst nach dem "
                                         "Settlement möglich (später mit „Ergebnisse prüfen“)")
        return run

    def _validate_market(self, m: dict, delivery: str, r: ApiResponse) -> None:
        problems = []
        if m.get("deliveryDate") != delivery:
            problems.append(f"deliveryDate={m.get('deliveryDate')}")
        if m.get("resolution") not in ("DAY", "QUARTER_HOUR"):
            problems.append(f"resolution={m.get('resolution')}")
        if m.get("status") not in ("OPEN", "CLOSED", "SETTLED", "FAILED"):
            problems.append(f"status={m.get('status')}")
        if not isinstance(m.get("prices"), dict):
            problems.append("prices fehlt")
        if not m.get("closesAt"):
            problems.append("closesAt fehlt")
        if problems:
            self.findings.error("response", f"Markt {delivery} weicht vom Schema ab: " + "; ".join(problems), r.short())

    def _wait_for_settlement(self, run: MarketRun, reader: ApiClient) -> None:
        deadline = time.monotonic() + self.cfg.wait_for_settlement_minutes * 60
        log.info("%s: warte bis zu %.0f min auf Marktschluss und Settlement durch den Betreiber …",
                 run.delivery_date, self.cfg.wait_for_settlement_minutes)
        last = None
        while True:
            g = reader.get_market(run.delivery_date)
            status = (g.body or {}).get("status") if g.status == 200 else None
            if status != last:
                log.info("%s: Marktstatus %s", run.delivery_date, status)
                last = status
            run.final_status = status or run.final_status
            if status in ("SETTLED", "FAILED"):
                if status == "FAILED":
                    self.findings.error("market", f"Settlement FAILED: {(g.body or {}).get('failureReason')}", g.short())
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self.stop.wait(min(10, remaining)):
                raise Stopped()
        self.findings.warning("market", f"{run.delivery_date}: nach {self.cfg.wait_for_settlement_minutes:g} min "
                                        f"nicht abgewickelt (Status {run.final_status})")

    # ------------------------------------------------------------- Positionen
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
        log.info("%s: %d Positionen geplant", run.delivery_date, len(run.positions))

    def _submit_all(self, run: MarketRun) -> None:
        self._plan(run)
        self._map(lambda sp: self._submit(run, sp), run.positions)
        log.info("%s: %d/%d Positionen angenommen", run.delivery_date, run.submitted, len(run.positions))

    def _submit(self, run: MarketRun, sp: SubmittedPosition) -> None:
        self._sleep(self._rng("delay", sp.eg.key, sp.planned.interval_start))
        r = sp.eg.client.submit_position(sp.planned.body(run.delivery_date))
        if r.status == 409 and r.code == "POSITION_ALREADY_EXISTS":
            self._handle_existing(run, sp, r)
            return
        if not self._expect(r, (201,), "submit", f"[{sp.eg.key}] Position einreichen"):
            self._inc(run, "rejected_submissions")
            return
        self._accept(run, sp, r.body or {}, r)

    def _accept(self, run: MarketRun, sp: SubmittedPosition, b: dict, r: ApiResponse) -> None:
        sp.id, sp.status, sp.created_at = b.get("id"), b.get("status"), b.get("createdAt")
        sp.quantity = D(b.get("quantityKwh"))
        sp.price_expectation = D(b.get("priceExpectationEurPerKwh"))
        self._inc(run, "submitted")
        self.validate_position_body(sp, b, run.delivery_date, r)

    def _handle_existing(self, run: MarketRun, sp: SubmittedPosition, r: ApiResponse) -> None:
        if self.cfg.on_existing_position == "skip":
            self.findings.info("submit", f"[{sp.eg.key}] Position existiert bereits für {run.delivery_date} "
                                         f"{sp.planned.interval_start or ''} – übersprungen", r.short())
            return
        lst = sp.eg.client.list_positions(deliveryDate=run.delivery_date, intervalStart=sp.planned.interval_start,
                                          status="OPEN")
        existing = (lst.body or []) if lst.status == 200 else []
        if not existing:
            self.findings.warning("submit", f"[{sp.eg.key}] 409 POSITION_ALREADY_EXISTS, aber keine OPEN-Position gefunden",
                                  lst.short())
            return
        pos = existing[0]
        sp.updated = True
        if pos.get("type") != sp.planned.type:  # Typ ist per PUT nicht änderbar
            if sp.eg.client.cancel_position(pos["id"]).status == 200:
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

    def validate_position_body(self, sp: SubmittedPosition, b: dict, delivery: str, r: ApiResponse) -> None:
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
        if D(b.get("quantityKwh")) != sp.planned.quantity:
            problems.append(f"quantityKwh={b.get('quantityKwh')} statt {sp.planned.quantity}")
        if D(b.get("priceExpectationEurPerKwh")) != sp.planned.price_expectation:
            problems.append(f"priceExpectationEurPerKwh={b.get('priceExpectationEurPerKwh')} "
                            f"statt {sp.planned.price_expectation}")
        if b.get("status") != "OPEN":
            problems.append(f"status={b.get('status')} statt OPEN")
        if not b.get("createdAt") or not b.get("updatedAt"):
            problems.append("createdAt/updatedAt fehlen")
        if problems:
            self.findings.error("response", f"[{sp.eg.key}] Positionsantwort weicht ab: " + "; ".join(problems), r.short())

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
                if D(b.get("priceExpectationEurPerKwh")) != body.get("priceExpectationEurPerKwh"):
                    self.findings.error("update", f"[{sp.eg.key}] Preisvorstellung nach PUT "
                                                  f"{b.get('priceExpectationEurPerKwh')}", r.short())
                if b.get("createdAt") != sp.created_at:
                    self.findings.warning("update", f"[{sp.eg.key}] createdAt hat sich durch PUT geändert "
                                                    "(relevant für die Rundungsreihenfolge)", r.short())
                sp.quantity, sp.updated = D(b.get("quantityKwh")), True
                self._inc(run, "updates")
        self._map(do, todo)

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
                    self.findings.error("cancel", f"[{sp.eg.key}] Status nach Storno {(r.body or {}).get('status')}",
                                        r.short())
                sp.cancelled, sp.status = True, "CANCELLED"
                self._inc(run, "cancels")
        self._map(do, todo)

    # ------------------------------------------------------------------- run
    def _dates(self) -> list[str]:
        out = []
        for spec in self.cfg.delivery_dates:
            try:
                out.append(resolve_delivery_date(spec).isoformat())
            except ValueError as exc:
                self.findings.error("config", str(exc))
        return out

    def run(self) -> dict:
        try:
            h = self.base.health_ready()
            if h.status != 200:
                self.findings.warning("health", f"/health/ready liefert {h.status or h.error}", h.short())
            self.connect()
            for d in self._dates():
                self._check_stop()
                self.run_market(d)
            if self.cfg.checks:
                from egsim import checks
                self._check_stop()
                self.check_results += checks.run_account_checks(self)
        except Stopped:
            self.stopped = True
            self.findings.warning("run", "Lauf wurde abgebrochen")
        return self.report()

    def verify_only(self) -> dict:
        """Nur Ergebnisse abholen und prüfen (z. B. nachdem der Betreiber den Markt geschlossen hat)."""
        self.mode = "verify"
        from egsim.verify import verify_market
        try:
            self.connect()
            reader = self.reader()
            for d in self._dates():
                self._check_stop()
                run = MarketRun(d)
                self.markets.append(run)
                if not reader:
                    break
                r = reader.get_market(d)
                if r.status != 200:
                    self.findings.error("market", f"Markt {d} abrufen: {r.status} {r.code or ''}", r.short())
                    continue
                run.market = r.body
                run.final_status = (r.body or {}).get("status")
                if run.final_status == "SETTLED":
                    run.verification = verify_market(self, run)
                else:
                    self.findings.info("verify", f"{d}: Markt ist {run.final_status}, noch nicht abgewickelt")
        except Stopped:
            self.stopped = True
        return self.report()

    def report(self) -> dict:
        summary = self.stats.summary()
        return {
            "egsimVersion": __version__,
            "contractVersion": CONTRACT_VERSION,
            "mode": self.mode,
            "runId": self.run_id,
            "seed": self.seed,
            "baseUrl": self.cfg.base_url,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": now_utc().isoformat(),
            "stopped": self.stopped,
            "communities": [{"name": e.key, "id": e.id, "ready": e.ready, "status": e.status, "problem": e.problem}
                            for e in self.egs],
            "markets": [m.summary() for m in self.markets],
            "checks": self.check_results,
            "findings": self.findings.as_list(),
            "requests": summary,
            "totals": {
                "errors": self.findings.count("error"),
                "warnings": self.findings.count("warning"),
                "checksPassed": sum(1 for c in self.check_results if c["result"] == "PASS"),
                "checksFailed": sum(1 for c in self.check_results if c["result"] == "FAIL"),
                "checksSkipped": sum(1 for c in self.check_results if c["result"] == "SKIP"),
                "requests": sum(x["requests"] for x in summary),
            },
        }


def register_community(base_url: str, name: str, email: str, description: str | None, *,
                       timeout: float = 10, verify_tls: bool = True, stats: RequestStats | None = None) -> ApiResponse:
    """Selbstregistrierung (POST /energy-communities, ohne Anmeldung). Danach Status PENDING."""
    client = ApiClient(base_url, stats=stats or RequestStats(), timeout=timeout, verify_tls=verify_tls, label=name)
    return client.register(email, name, description)


def eg_client(cfg: Config, community: CommunityConfig, stats: RequestStats) -> tuple[ApiClient | None, ApiResponse | None]:
    """Angemeldeter Client für eine EG (für manuelle Requests in der UI)."""
    base = ApiClient(cfg.base_url, stats=stats, timeout=cfg.timeout, verify_tls=cfg.verify_tls, label=community.name)
    if not community.has_credentials:
        return None, None
    if community.auth == "bearer":
        r = base.login(community.email, community.password)
        if r.status == 200 and isinstance(r.body, dict) and r.body.get("token"):
            return base.with_auth(token=r.body["token"]), r
        return None, r
    return base.with_auth(api_key=community.api_key), None


def test_connection(cfg: Config, community: CommunityConfig, stats: RequestStats | None = None) -> dict:
    """Health-Check, Anmeldung und GET /me für eine EG – für den Button „Verbindung testen“."""
    stats = stats or RequestStats()
    base = ApiClient(cfg.base_url, stats=stats, timeout=cfg.timeout, verify_tls=cfg.verify_tls, label=community.name)
    steps: list[dict] = []
    out: dict = {"steps": steps, "ok": False}
    h = base.health_ready()
    steps.append({"step": "GET /health/ready", "status": h.status, "ok": h.status == 200,
                  "detail": h.error or (h.body if isinstance(h.body, dict) else None)})
    if not community.has_credentials:
        steps.append({"step": "Anmeldung", "ok": False, "detail": "keine Zugangsdaten hinterlegt"})
        return out
    client, login = eg_client(cfg, community, stats)
    if login is not None:
        steps.append({"step": "POST /auth/login", "status": login.status, "ok": client is not None, "detail": login.code})
    if client is None:
        return out
    me = client.me()
    body = me.body if isinstance(me.body, dict) else {}
    ec = body.get("energyCommunity")
    detail = me.code or (" · ".join(str(x) for x in (body.get("role"), (ec or {}).get("name"), (ec or {}).get("status")) if x))
    steps.append({"step": "GET /me", "status": me.status, "ok": me.status == 200, "detail": detail})
    out["energyCommunity"] = ec
    out["ok"] = me.status == 200 and body.get("role") == "ENERGY_COMMUNITY"
    return out
