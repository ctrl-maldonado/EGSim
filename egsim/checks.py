"""Negativ- und Vertragstests: prüfen Statuscodes, Fehlercodes, Rollen und Mandantentrennung.

Validierungstests nutzen eine EG, die für denselben Liefertag/dasselbe Intervall schon eine Position hat.
Ein Server, der die Validierung auslässt, antwortet dann mit 409 statt eine falsche Position anzulegen
(und verletzt nebenbei die vorgeschriebene Prüfreihenfolge 400 vor 409).
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from egsim.client import ApiClient, ApiResponse
from egsim.timeutil import day_bounds_utc, format_utc, normalize_interval, quarter_hours, resolve_delivery_date

if TYPE_CHECKING:
    from egsim.simulator import MarketRun, SimEG, Simulator, SubmittedPosition

log = logging.getLogger("egsim.checks")

Expect = list[tuple[int, tuple[str, ...]]]  # erlaubte (HTTP-Status, Fehlercodes); () = Code egal


class Checker:
    def __init__(self, sim: "Simulator", phase: str) -> None:
        self.sim = sim
        self.phase = phase
        self.results: list[dict] = []

    def run(self, cid: str, title: str, fn: Callable[[], ApiResponse], expect: Expect,
            *, note_on: dict[tuple[int, str], str] | None = None,
            on_unexpected_success: Callable[[ApiResponse], None] | None = None,
            extra: Callable[[ApiResponse], str | None] | None = None) -> ApiResponse | None:
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001 – ein kaputter Check soll den Lauf nicht abbrechen
            self._add(cid, title, "FAIL", expect, None, None, f"Ausnahme: {exc!r}")
            return None
        passed, via_details = False, False
        for status, codes in expect:
            if r.status != status:
                continue
            if not codes or r.code in codes:
                passed = True
            elif r.code == "VALIDATION_FAILED" and set(codes) & r.detail_codes:
                passed, via_details = True, True
            if passed:
                break
        note = None
        if via_details:
            note = "Code nur in details, oberster Code VALIDATION_FAILED"
        if note_on and (r.status, r.code) in note_on:
            note = note_on[(r.status, r.code)]
        if passed and extra:
            problem = extra(r)
            if problem:
                passed, note = False, problem
        if not passed and r.ok and on_unexpected_success:
            on_unexpected_success(r)
        self._add(cid, title, "PASS" if passed else "FAIL", expect, r.status, r.code, note, r.short())
        return r

    def skip(self, cid: str, title: str, reason: str) -> None:
        self._add(cid, title, "SKIP", [], None, None, reason)

    def _add(self, cid, title, result, expect, status, code, note, request=None) -> None:
        exp = " oder ".join(f"{s} {'/'.join(c) if c else ''}".strip() for s, c in expect)
        self.results.append({"id": cid, "title": title, "phase": self.phase, "result": result, "expected": exp,
                             "actualStatus": status, "actualCode": code, "note": note, "request": request})
        if result == "FAIL":
            self.sim.findings.error("check", f"{cid}: {title} – erwartet {exp}, erhalten {status} {code or ''}"
                                             f"{' (' + note + ')' if note else ''}", request)
        elif note and result == "PASS":
            self.sim.findings.info("check", f"{cid}: {note}", request)


def _err(status: int, *codes: str) -> tuple[int, tuple[str, ...]]:
    return status, codes


def _pick(run: "MarketRun") -> tuple["SubmittedPosition | None", "SubmittedPosition | None"]:
    """Zwei offene Positionen verschiedener EGs."""
    live = [sp for sp in run.positions if sp.id and not sp.cancelled and sp.status == "OPEN"]
    a = live[0] if live else None
    b = next((sp for sp in live if a and sp.eg.id != a.eg.id), None)
    return a, b


def _cleanup(sim: "Simulator", client: ApiClient) -> Callable[[ApiResponse], None]:
    def f(r: ApiResponse) -> None:
        pid = (r.body or {}).get("id") if isinstance(r.body, dict) else None
        if pid:
            client.cancel_position(pid)
            sim.findings.warning("check", f"Ungültige Position wurde angelegt ({pid}) und wieder storniert")
    return f


# =====================================================================================================================
def run_open_market_checks(sim: "Simulator", run: "MarketRun") -> list[dict]:
    c = Checker(sim, f"open:{run.delivery_date}")
    d = run.delivery_date
    res = run.market["resolution"]
    sp_a, sp_b = _pick(run)
    anon = sim.base
    rnd = str(uuid.uuid4())

    c.run("AUTH-01", "Ohne Zugangsdaten → 401", lambda: anon.call("GET", "/me", auth=False), [_err(401, "UNAUTHORIZED")])
    c.run("AUTH-02", "Ungültiger API-Key → 401", lambda: anon.with_auth(api_key="egsim-invalid-" + rnd).me(),
          [_err(401, "UNAUTHORIZED")])
    c.run("AUTH-03", "Ungültiges Bearer-Token → 401", lambda: anon.with_auth(token="egsim.invalid." + rnd).me(),
          [_err(401, "UNAUTHORIZED")])
    if not sp_a:
        c.skip("POS-*", "Positions-Checks", "keine offene eigene Position vorhanden")
        return c.results
    c.run("NF-01", "Markt ohne Eintrag → 404 MARKET_NOT_FOUND", lambda: sp_a.eg.client.get_market("2099-12-31"),
          [_err(404, "MARKET_NOT_FOUND")])
    c.run("NF-02", "Unbekannte EG → 404 ENERGY_COMMUNITY_NOT_FOUND", lambda: sp_a.eg.client.get_community(rnd),
          [_err(404, "ENERGY_COMMUNITY_NOT_FOUND")])
    c.run("NF-03", "Ungültige EG-ID → 400 INVALID_ID", lambda: sp_a.eg.client.get_community("not-a-uuid"),
          [_err(400, "INVALID_ID")])
    c.run("NF-06", "Ungültiges Datum im Pfad → 400 INVALID_DELIVERY_DATE", lambda: sp_a.eg.client.get_market("20-11-2026"),
          [_err(400, "INVALID_DELIVERY_DATE")])
    c.run("STATE-02", "Zuteilungen vor Settlement → 409 MARKET_NOT_SETTLED", lambda: sp_a.eg.client.allocations(d),
          [_err(409, "MARKET_NOT_SETTLED")])
    c.run("STATE-03", "Trades vor Settlement → 409 MARKET_NOT_SETTLED", lambda: sp_a.eg.client.trades(d),
          [_err(409, "MARKET_NOT_SETTLED")])

    eg = sp_a.eg
    cl = eg.client
    base = sp_a.planned.body(d)

    def body(**changes):
        b = dict(base)
        for k, v in changes.items():
            if v is _DROP:
                b.pop(k, None)
            else:
                b[k] = v
        return b

    def post(b):
        return lambda: cl.submit_position(b)

    cleanup = _cleanup(sim, cl)

    # --- Rollen
    c.run("ROLE-02", "EG listet alle EGs → 403", lambda: cl.list_communities(), [_err(403, "FORBIDDEN")])
    c.run("ROLE-03", "EG ruft Tagesergebnis ab → 403", lambda: cl.market_result(d), [_err(403, "FORBIDDEN")])
    c.run("ROLE-04", "EG ändert eigenen Status → 403", lambda: cl.set_status(eg.id, "ACTIVE"), [_err(403, "FORBIDDEN")])

    def reattach(r: ApiResponse) -> None:  # falls der Server das fälschlich erlaubt: neuen Key übernehmen
        if isinstance(r.body, dict) and r.body.get("apiKey"):
            eg.cfg.api_key, eg.cfg.password = r.body["apiKey"], r.body.get("password") or eg.cfg.password
            eg.client = cl.with_auth(api_key=eg.cfg.api_key)
            sim.findings.error("check", f"[{eg.key}] EG konnte sich selbst neue Zugangsdaten ausstellen – "
                                        "neuer API-Key wurde in die Konfiguration übernommen")
    c.run("ROLE-05", "EG vergibt sich selbst Zugangsdaten → 403", lambda: cl.issue_credentials(eg.id),
          [_err(403, "FORBIDDEN")], on_unexpected_success=reattach)
    cl = eg.client

    # --- Profil
    c.run("PROF-01", "Profil mit Feld status → 400 VALIDATION_FAILED",
          lambda: cl.update_community(eg.id, {"status": "ACTIVE"}), [_err(400, "VALIDATION_FAILED")])
    c.run("PROF-02", "Eigenes Profil ändern → 200",
          lambda: cl.update_community(eg.id, {"description": "Simulierte EG (EGSim), Profil geändert"}), [_err(200)])
    c.run("PROF-03", "Leerer Name → 400 VALIDATION_FAILED", lambda: cl.update_community(eg.id, {"name": "   "}),
          [_err(400, "VALIDATION_FAILED")])

    # --- Validierung (Prüfreihenfolge: 400 vor 409)
    offer = base["type"] == "OFFER"
    c.run("VAL-01", "quantityKwh = 0 → 400 INVALID_QUANTITY", post(body(quantityKwh=Decimal(0))),
          [_err(400, "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    c.run("VAL-02", "quantityKwh mit 4 Nachkommastellen → 400 INVALID_QUANTITY", post(body(quantityKwh=Decimal("1.2345"))),
          [_err(400, "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    c.run("VAL-03", "quantityKwh > 1.000.000 → 400 INVALID_QUANTITY", post(body(quantityKwh=Decimal("1000000.001"))),
          [_err(400, "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    c.run("VAL-04", "quantityKwh als String → 400", post(body(quantityKwh="10")),
          [_err(400, "VALIDATION_FAILED", "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    ob = body(type="OFFER")
    c.run("VAL-05", "Preis mit 6 Nachkommastellen → 400 INVALID_PRICE",
          post({**ob, "priceExpectationEurPerKwh": Decimal("0.123456")}), [_err(400, "INVALID_PRICE")],
          on_unexpected_success=cleanup)
    c.run("VAL-06", "Negativer Preis → 400 INVALID_PRICE", post({**ob, "priceExpectationEurPerKwh": Decimal("-0.01")}),
          [_err(400, "INVALID_PRICE")], on_unexpected_success=cleanup)
    c.run("VAL-07", "Preis > 100 → 400 INVALID_PRICE", post({**ob, "priceExpectationEurPerKwh": Decimal("100.5")}),
          [_err(400, "INVALID_PRICE")], on_unexpected_success=cleanup)
    db = body(type="DEMAND")
    c.run("VAL-08", "DEMAND mit Preisvorstellung → 400 VALIDATION_FAILED",
          post({**db, "priceExpectationEurPerKwh": Decimal("0.1")}), [_err(400, "VALIDATION_FAILED")],
          on_unexpected_success=cleanup)
    c.run("VAL-09", "Unbekanntes Feld → 400 VALIDATION_FAILED", post(body(foo="bar")), [_err(400, "VALIDATION_FAILED")],
          on_unexpected_success=cleanup)
    c.run("VAL-10", "Pflichtfeld type fehlt → 400 VALIDATION_FAILED", post(body(type=_DROP)),
          [_err(400, "VALIDATION_FAILED")], on_unexpected_success=cleanup)
    c.run("VAL-11", "Ungültiger type → 400 INVALID_POSITION_TYPE", post(body(type="SELL")),
          [_err(400, "INVALID_POSITION_TYPE")], on_unexpected_success=cleanup)
    c.run("VAL-12", "deliveryDate falsches Format → 400 INVALID_DELIVERY_DATE", post(body(deliveryDate="20.11.2026")),
          [_err(400, "INVALID_DELIVERY_DATE")], on_unexpected_success=cleanup)
    c.run("VAL-13", "deliveryDate in der Vergangenheit → 422 DELIVERY_DATE_IN_PAST",
          post(body(deliveryDate="2020-01-01",
                    intervalStart=_DROP if res == "DAY" else format_utc(quarter_hours(resolve_delivery_date("2020-01-01"))[48]))),
          [_err(422, "DELIVERY_DATE_IN_PAST"), _err(404, "MARKET_NOT_FOUND")],
          note_on={(404, "MARKET_NOT_FOUND"): "404 statt 422: Vertrag regelt nicht, ob 404 (kein Markt) oder 422 "
                                              "(Vergangenheit) Vorrang hat – Reihenfolge 404 vor 422 spricht für 404"},
          on_unexpected_success=cleanup)
    c.run("VAL-14", "Kaputtes JSON → 400 VALIDATION_FAILED",
          lambda: cl.call("POST", "/positions", raw_body='{"deliveryDate": "' + d + '", '), [_err(400, "VALIDATION_FAILED")])
    c.run("VAL-15", "Fremde, ungültige energyCommunityId → 400 INVALID_ID", post(body(energyCommunityId="xyz")),
          [_err(400, "INVALID_ID", "VALIDATION_FAILED")], on_unexpected_success=cleanup)

    if res == "DAY":
        c.run("INT-01", "intervalStart im Tagesmarkt → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(day_bounds_utc(resolve_delivery_date(d))[0] + timedelta(hours=11)))),
              [_err(400, "INVALID_INTERVAL")], on_unexpected_success=cleanup)
    else:
        start = day_bounds_utc(resolve_delivery_date(d))[0]
        c.run("INT-01", "intervalStart fehlt im 15-Min-Markt → 400 INVALID_INTERVAL", post(body(intervalStart=_DROP)),
              [_err(400, "INVALID_INTERVAL")], on_unexpected_success=cleanup)
        c.run("INT-02", "intervalStart keine volle Viertelstunde → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(start + timedelta(minutes=7)))), [_err(400, "INVALID_INTERVAL")],
              on_unexpected_success=cleanup)
        c.run("INT-03", "intervalStart außerhalb des Liefertags → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(start - timedelta(minutes=15)))), [_err(400, "INVALID_INTERVAL")],
              on_unexpected_success=cleanup)
        c.run("INT-04", "intervalStart mit Offset statt UTC → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(start + timedelta(hours=12)).replace("Z", "+00:00"))),
              [_err(400, "INVALID_INTERVAL", "VALIDATION_FAILED")], on_unexpected_success=cleanup)

    # --- Zustand
    c.run("STATE-04", "Zweite Position im selben Intervall → 409 POSITION_ALREADY_EXISTS", post(dict(base)),
          [_err(409, "POSITION_ALREADY_EXISTS")], on_unexpected_success=cleanup)
    c.run("NF-04", "Unbekannte Position → 404 POSITION_NOT_FOUND", lambda: cl.get_position(rnd),
          [_err(404, "POSITION_NOT_FOUND")])
    c.run("NF-05", "Ungültige Positions-ID → 400 INVALID_ID", lambda: cl.get_position("123"), [_err(400, "INVALID_ID")])
    c.run("POS-01", "Eigene Position abrufen → 200", lambda: cl.get_position(sp_a.id), [_err(200)],
          extra=lambda r: None if (r.body or {}).get("id") == sp_a.id else "falsche Position zurückgegeben")
    c.run("POS-02", "PUT mit unbekanntem Feld → 400 VALIDATION_FAILED",
          lambda: cl.update_position(sp_a.id, {"quantityKwh": sp_a.quantity, "type": "DEMAND"}),
          [_err(400, "VALIDATION_FAILED")])
    c.run("POS-03", "PUT ohne quantityKwh → 400 VALIDATION_FAILED", lambda: cl.update_position(sp_a.id, {}),
          [_err(400, "VALIDATION_FAILED")])
    cancelled = next((sp for sp in run.positions if sp.cancelled and sp.eg.ready), None)
    if cancelled:
        ccl = cancelled.eg.client
        c.run("STATE-05", "Stornierte Position ändern → 409 POSITION_NOT_MODIFIABLE",
              lambda: ccl.update_position(cancelled.id, {"quantityKwh": Decimal(1)}), [_err(409, "POSITION_NOT_MODIFIABLE")])
        c.run("STATE-06", "Stornierte Position erneut stornieren → 409 POSITION_NOT_MODIFIABLE",
              lambda: ccl.cancel_position(cancelled.id), [_err(409, "POSITION_NOT_MODIFIABLE")])
    else:
        c.skip("STATE-05", "Stornierte Position ändern", "keine stornierte Position (behaviour.cancelProbability = 0)")

    def own_only(r: ApiResponse) -> str | None:
        rows = r.body if isinstance(r.body, list) else []
        foreign = [x for x in rows if x.get("energyCommunityId") != eg.id]
        return f"{len(foreign)} fremde Positionen in der Liste" if foreign else None
    c.run("TEN-01", "EG-Liste enthält nur eigene Positionen", lambda: cl.list_positions(deliveryDate=d), [_err(200)],
          extra=own_only)

    # --- Mandantentrennung (zweite EG nötig)
    if not sp_b:
        c.skip("TEN-02", "Mandantentrennung", "nur eine EG mit offener Position – mindestens zwei EGs konfigurieren")
        return c.results
    other = sp_b.eg
    c.run("TEN-02", "Fremde energyCommunityId beim Einreichen → 403", post(body(energyCommunityId=other.id)),
          [_err(403, "FORBIDDEN")], on_unexpected_success=_cleanup(sim, other.client))
    c.run("TEN-03", "Fremde Position abrufen → 403", lambda: cl.get_position(sp_b.id), [_err(403, "FORBIDDEN")])
    c.run("TEN-04", "Fremde Position ändern → 403",
          lambda: cl.update_position(sp_b.id, {"quantityKwh": sp_b.quantity,
                                               **({"priceExpectationEurPerKwh": sp_b.price_expectation}
                                                  if sp_b.price_expectation is not None else {})}),
          [_err(403, "FORBIDDEN")])

    def mark_cancelled(r: ApiResponse) -> None:
        sp_b.cancelled, sp_b.status = True, "CANCELLED"
    c.run("TEN-05", "Fremde Position stornieren → 403", lambda: cl.cancel_position(sp_b.id), [_err(403, "FORBIDDEN")],
          on_unexpected_success=mark_cancelled)
    c.run("TEN-06", "Positionsfilter auf fremde EG → 403", lambda: cl.list_positions(energyCommunityId=other.id),
          [_err(403, "FORBIDDEN")])
    c.run("TEN-07", "Fremde EG abrufen → 403", lambda: cl.get_community(other.id), [_err(403, "FORBIDDEN")])
    c.run("TEN-08", "Fremdes Profil ändern → 403", lambda: cl.update_community(other.id, {"description": "x"}),
          [_err(403, "FORBIDDEN")])
    c.run("TEN-09", "Zuteilungsfilter auf fremde EG → 403", lambda: cl.allocations(d, energyCommunityId=other.id),
          [_err(403, "FORBIDDEN")])
    c.run("TEN-10", "Trade-Filter auf fremde EG → 403", lambda: cl.trades(d, energyCommunityId=other.id),
          [_err(403, "FORBIDDEN")])
    return c.results


class _Drop:
    pass


_DROP = _Drop()


# =====================================================================================================================
def run_closed_market_checks(sim: "Simulator", run: "MarketRun") -> list[dict]:
    c = Checker(sim, f"closed:{run.delivery_date}")
    d = run.delivery_date
    live = [sp for sp in run.positions if sp.id and not sp.cancelled]
    if not live:
        c.skip("CLOSED-*", "Checks nach Marktschluss", "keine eigenen Positionen")
        return c.results
    sp = live[0]
    cl = sp.eg.client

    # neue Position in einem freien Slot, damit 409 eindeutig MARKET_CLOSED sein muss
    taken = {(p.eg.id, p.planned.interval_start) for p in live}
    free_body = None
    if (run.market or {}).get("resolution") == "QUARTER_HOUR":
        for t in quarter_hours(resolve_delivery_date(d)):
            if (sp.eg.id, format_utc(t)) not in taken:
                free_body = {"deliveryDate": d, "intervalStart": format_utc(t), "type": "DEMAND", "quantityKwh": Decimal(1)}
                break
        submitter = sp.eg
    else:
        submitter = next((e for e in sim.egs if e.ready and (e.id, None) not in taken), None)
        if submitter:
            free_body = {"deliveryDate": d, "type": "DEMAND", "quantityKwh": Decimal(1)}
    if free_body and submitter:
        c.run("CLOSED-01", "Neue Position nach Marktschluss → 409 MARKET_CLOSED",
              lambda: submitter.client.submit_position(free_body), [_err(409, "MARKET_CLOSED")],
              on_unexpected_success=_cleanup(sim, submitter.client))
    else:
        c.run("CLOSED-01", "Neue Position nach Marktschluss → 409 MARKET_CLOSED",
              lambda: cl.submit_position(sp.planned.body(d)), [_err(409, "MARKET_CLOSED", "POSITION_ALREADY_EXISTS")])

    upd = {"quantityKwh": sp.quantity}
    if sp.price_expectation is not None:
        upd["priceExpectationEurPerKwh"] = sp.price_expectation
    c.run("CLOSED-02", "Position nach Marktschluss ändern → 409", lambda: cl.update_position(sp.id, upd),
          [_err(409, "MARKET_CLOSED", "POSITION_NOT_MODIFIABLE")])
    c.run("CLOSED-03", "Position nach Marktschluss stornieren → 409", lambda: cl.cancel_position(sp.id),
          [_err(409, "MARKET_CLOSED", "POSITION_NOT_MODIFIABLE")])
    c.run("CLOSED-04", "EG schließt Markt → 403", lambda: cl.close_market(d), [_err(403, "FORBIDDEN")])
    if run.final_status == "SETTLED":
        c.run("CLOSED-05", "Position nach Settlement ist PROCESSED", lambda: cl.get_position(sp.id), [_err(200)],
              extra=lambda r: None if (r.body or {}).get("status") == "PROCESSED"
              else f"Status {(r.body or {}).get('status')}")
        c.run("CLOSED-07", "EG ruft eigene Zuteilungen ab → 200", lambda: cl.allocations(d), [_err(200)])
    return c.results


# =====================================================================================================================
def run_account_checks(sim: "Simulator") -> list[dict]:
    """Login/Logout mit den Zugangsdaten der EGs und optional Registrierungs-Checks."""
    c = Checker(sim, "konto")
    anon = sim.base
    rnd = uuid.uuid4().hex[:8]
    c.run("AUTH-04", "Login unbekannte E-Mail → 401 INVALID_CREDENTIALS",
          lambda: anon.login(f"unbekannt-{rnd}@egsim.example", "falsch-123456"), [_err(401, "INVALID_CREDENTIALS")])
    c.run("AUTH-05", "Login ohne Passwort → 400 VALIDATION_FAILED",
          lambda: anon.call("POST", "/auth/login", auth=False, json_body={"email": "a@egsim.example"}),
          [_err(400, "VALIDATION_FAILED")])

    eg = next((e for e in sim.egs if e.ready and e.cfg.email and e.cfg.password), None)
    if eg:
        email, pw = eg.cfg.email, eg.cfg.password
        c.run("AUTH-06", "Login mit falschem Passwort → 401 INVALID_CREDENTIALS", lambda: anon.login(email, pw + "x"),
              [_err(401, "INVALID_CREDENTIALS")])

        def login_ok(r: ApiResponse) -> str | None:
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tokenType") != "Bearer" or not b.get("token"):
                return "token/tokenType fehlen"
            if b.get("role") != "ENERGY_COMMUNITY" or (b.get("energyCommunity") or {}).get("id") != eg.id:
                return "role oder energyCommunity falsch"
            if not isinstance(b.get("expiresIn"), int) or b["expiresIn"] < 1:
                return "expiresIn fehlt"
            return None
        lg = c.run("AUTH-07", "Login → 200 mit Bearer-Token", lambda: anon.login(email, pw), [_err(200)], extra=login_ok)
        if lg and lg.status == 200 and isinstance(lg.body, dict) and lg.body.get("token"):
            tk = anon.with_auth(token=lg.body["token"], label=eg.key)
            c.run("AUTH-08", "GET /me mit Token → 200", tk.me, [_err(200)])
            c.run("AUTH-09", "Logout → 204", tk.logout, [_err(204)])
            c.run("AUTH-10", "Token nach Logout → 401", tk.me, [_err(401, "UNAUTHORIZED")])
    else:
        c.skip("AUTH-06", "Login-Checks", "keine EG mit E-Mail und Passwort hinterlegt")

    if not sim.cfg.registration_checks:
        c.skip("REG-*", "Registrierungs-Checks", "abgeschaltet (legen eine PENDING-Test-EG auf der Plattform an)")
        return c.results
    email = f"egsim-regtest-{sim.run_id}@egsim.example"
    c.run("REG-01", "Registrierung mit ungültiger E-Mail → 400 VALIDATION_FAILED",
          lambda: anon.register("keine-email", "EGSim Test"), [_err(400, "VALIDATION_FAILED")])
    c.run("REG-02", "Registrierung mit Feld status → 400 VALIDATION_FAILED",
          lambda: anon.call("POST", "/energy-communities", auth=False,
                            json_body={"email": "x-" + email, "name": "EGSim", "status": "ACTIVE"}),
          [_err(400, "VALIDATION_FAILED")])
    c.run("REG-03", "Registrierung mit leerem Namen → 400 VALIDATION_FAILED", lambda: anon.register("y-" + email, "   "),
          [_err(400, "VALIDATION_FAILED")])
    c.run("REG-04", "Name mit 121 Zeichen → 400 VALIDATION_FAILED", lambda: anon.register("z-" + email, "N" * 121),
          [_err(400, "VALIDATION_FAILED")])

    def pending(r: ApiResponse) -> str | None:
        b = r.body if isinstance(r.body, dict) else {}
        if b.get("status") != "PENDING":
            return f"Status {b.get('status')} statt PENDING"
        if not b.get("id") or "apiKey" in b or "password" in b:
            return "id fehlt oder Antwort enthält Zugangsdaten"
        return None
    r = c.run("REG-05", "Registrierung → 201 PENDING", lambda: anon.register(email, f"EGSim Registrierungstest {rnd}",
                                                                           "Von EGSim angelegt, kann gelöscht werden"),
              [_err(201)], extra=pending)
    if r and r.status == 201:
        c.run("REG-06", "Gleiche E-Mail erneut → 409 EMAIL_ALREADY_REGISTERED", lambda: anon.register(email, "Doppelt"),
              [_err(409, "EMAIL_ALREADY_REGISTERED")])
        c.run("REG-07", "Gleiche E-Mail in Großbuchstaben → 409 EMAIL_ALREADY_REGISTERED",
              lambda: anon.register(email.upper(), "Doppelt"), [_err(409, "EMAIL_ALREADY_REGISTERED")],
              note_on={(201, None): "E-Mail-Vergleich ist case-sensitiv"})
        c.run("REG-08", "Login einer PENDING-EG ohne Passwort-Vergabe → 401/403",
              lambda: anon.login(email, "egal-12345678"),
              [_err(401, "INVALID_CREDENTIALS"), _err(403, "ENERGY_COMMUNITY_NOT_ACTIVE")])
        sim.findings.info("check", f"Registrierungstest hat die PENDING-EG {email} angelegt – Admin kann sie löschen")
    return c.results
