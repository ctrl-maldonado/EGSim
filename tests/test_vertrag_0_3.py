"""Regeln aus Vertrag 0.3.1: Mock setzt sie um, Simulator erkennt Plattformen, die noch 0.2 umsetzen."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from egsim.client import ApiClient, RequestStats
from egsim.config import parse_config
from egsim.mockserver import ApiErr, MockPlatform, start_server
from egsim.simulator import Simulator
from egsim.timeutil import now_utc

from test_e2e_mock import _admin_setup, _setup


@pytest.fixture()
def mock():
    p = MockPlatform()
    d = (date.today() + timedelta(days=1)).isoformat()
    p.add_market(d, "DAY")
    srv = start_server(p)
    base = ApiClient(f"http://127.0.0.1:{srv.server_address[1]}", stats=RequestStats())
    eg = p.add_demo_community("EG Test")
    yield p, base, base.with_auth(api_key=eg["apiKey"]), d, eg
    srv.shutdown()


def contract_issues(client):
    return [e["message"] for e in client.stats.contract_issues()]


def test_idempotency_key_bei_positionen(mock):
    p, base, cl, d, _ = mock
    body = {"deliveryDate": d, "type": "OFFER", "quantityKwh": Decimal(120)}
    assert cl.submit_position(body, idempotency_key="").code == "VALIDATION_FAILED"
    key = str(uuid.uuid4())
    first = cl.submit_position(body, idempotency_key=key)
    assert first.status == 201
    # gleicher Inhalt, andere Schreibweise und Feldreihenfolge: gespeicherte Antwort
    again = cl.call("POST", "/positions", raw_body=f'{{"quantityKwh": 120.0, "type": "OFFER", "deliveryDate": "{d}"}}',
                    headers={"Idempotency-Key": key})
    assert again.status == 201 and again.body["id"] == first.body["id"]
    reused = cl.submit_position({**body, "quantityKwh": Decimal(121)}, idempotency_key=key)
    assert (reused.status, reused.code) == (409, "IDEMPOTENCY_KEY_REUSED")
    assert len(p.positions) == 1
    # nach closesAt liefert die Wiederholung weiterhin dieselbe 201 (K3)
    p.markets[d]["closesAt"] = (now_utc() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    late = cl.submit_position(body, idempotency_key=key)
    assert late.status == 201 and late.body["id"] == first.body["id"]
    assert cl.submit_position(body).code == "MARKET_CLOSED"
    assert contract_issues(cl) == []


def test_stornieren_ist_idempotent_und_vergangener_liefertag_mit_markt(mock):
    p, base, cl, d, _ = mock
    past = (date.today() - timedelta(days=30)).isoformat()
    p.add_market(past, "DAY")
    r = cl.submit_position({"deliveryDate": past, "type": "DEMAND", "quantityKwh": Decimal(5)})
    assert r.status == 201  # 0.3 (B1): kein DELIVERY_DATE_IN_PAST mehr
    assert cl.cancel_position(r.body["id"]).status == 200
    again = cl.cancel_position(r.body["id"])
    assert again.status == 200 and again.body["status"] == "CANCELLED"


def test_request_id_api_version_und_markets(mock):
    p, base, cl, d, _ = mock
    r = cl.call("GET", "/markets/{deliveryDate}", path_params={"deliveryDate": "2099-12-31"},
                headers={"X-Request-Id": "meine-id"})
    assert r.status == 404 and r.headers["x-request-id"] == "meine-id" and r.body["requestId"] == "meine-id"
    assert r.headers["api-version"] == "0.3.1"
    lst = cl.list_markets("OPEN")
    assert lst.status == 200 and [m["deliveryDate"] for m in lst.body] == [d]
    assert cl.list_markets("FOO").code == "VALIDATION_FAILED"
    assert contract_issues(cl) == []


def test_registrierung_mit_community_code_und_rate_limit(mock):
    p, base, cl, d, eg = mock
    assert base.register("a@x.example", "EG A").code == "VALIDATION_FAILED"  # communityCode fehlt
    key = str(uuid.uuid4())
    r = base.register("a@x.example", "EG A", community_code="eg-a", idempotency_key=key)
    assert r.status == 201 and r.body["communityCode"] == "eg-a"
    again = base.register("a@x.example", "EG A", community_code="eg-a", idempotency_key=key)
    assert again.status == 201 and again.body["id"] == r.body["id"]
    assert base.register("b@x.example", "EG B", community_code="eg-a").code == "COMMUNITY_CODE_ALREADY_REGISTERED"
    assert base.register("c@x.example", "EG C", community_code="EG_C").code == "VALIDATION_FAILED"
    # Profil: communityCode nicht änderbar
    assert cl.update_community(eg["id"], {"communityCode": "neu"}).code == "VALIDATION_FAILED"
    # 5 Fehlversuche, danach 429 mit Retry-After, auch mit richtigem Passwort
    for _ in range(5):
        assert base.login(eg["email"], "falsch-123456").code == "INVALID_CREDENTIALS"
    locked = base.login(eg["email"], eg["password"])
    assert (locked.status, locked.code) == (429, "RATE_LIMITED") and locked.headers.get("retry-after")
    assert contract_issues(base) == []


# --------------------------------------------------------------------------------- Plattformen noch auf Stand 0.2
class OhneIdempotencyKey(MockPlatform):
    """Ignoriert den Idempotency-Key und lehnt einen schon stornierten Storno ab (Verhalten aus 0.2)."""

    def h_submit(self, q, h, raw):
        role, ec, _ = self._auth(h)
        self._need(role, "ENERGY_COMMUNITY")
        return self._submit(ec, raw)

    def h_cancel_position(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        if self._load_position(role, ec, raw_id)["status"] == "CANCELLED":
            raise ApiErr("POSITION_NOT_MODIFIABLE")
        return super().h_cancel_position(raw_id, q, h, raw)


def test_checks_erkennen_fehlende_idempotenz():
    p = OhneIdempotencyKey()
    srv, raw, d = _setup(p, "DAY", n=3)
    raw["behaviour"]["cancelProbability"] = 0.5
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    failed = {c["id"] for c in rep["checks"] if c["result"] == "FAIL"}
    assert {"IDEM-01", "IDEM-02", "IDEM-03", "IDEM-04"} <= failed, failed
    ran = {c["id"] for c in rep["checks"] if c["result"] != "SKIP"}
    assert "STATE-06" in failed or "STATE-06" not in ran  # nur wenn der Lauf eine Position storniert hat


class Mindestpreis(MockPlatform):
    """Behandelt die Preisvorstellung noch als Mindestpreis (0.2): Angebot über dem Ankaufspreis wird nicht gekauft."""

    def _settle(self, d):
        super()._settle(d)
        res = self.results[d]
        buy = self.markets[d]["prices"]["buyPriceEurPerKwh"]
        drop = {a["positionId"] for a in res["allocations"] if a["type"] == "OFFER"
                and (self.positions[a["positionId"]].get("priceExpectationEurPerKwh") or 0) > buy}
        for a in res["allocations"]:
            if a["positionId"] in drop:
                a["allocatedKwh"], a["reason"] = Decimal(0), "NOT_ALLOCATED"
        res["trades"] = [t for t in res["trades"] if t["positionId"] not in drop]


def test_mindestpreis_verletzt_invariante():
    res_by_id, rep = _run_scenarios(Mindestpreis(), ["SZ-03", "SZ-01"])
    assert res_by_id["SZ-01"]["result"] == "PASS"
    assert res_by_id["SZ-03"]["result"] == "FAIL"
    assert any("Invariante 3" in m for m in res_by_id["SZ-03"]["messages"]), res_by_id["SZ-03"]["messages"]


def _run_scenarios(platform, ids):
    srv, raw, d = _admin_setup(platform, scenarios=ids)
    raw["communities"] = []
    try:
        rep = Simulator(parse_config(raw)).run_scenarios()
    finally:
        srv.shutdown()
    return {s["id"]: s for s in rep["scenarios"]}, rep


class OhneHeader(MockPlatform):
    """Fehler ohne requestId; der HTTP-Handler wird so umgangen, dass X-Request-Id und Api-Version fehlen."""

    def handle(self, method, path, query, headers, raw):
        status, body = super().handle(method, path, query, headers, raw)
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if k != "requestId"}
        return status, body


def test_fehlende_request_id_wird_gemeldet():
    p = OhneHeader()
    d = (date.today() + timedelta(days=1)).isoformat()
    p.add_market(d)
    srv = start_server(p)
    try:
        cl = ApiClient(f"http://127.0.0.1:{srv.server_address[1]}", stats=RequestStats())
        cl.call("GET", "/me", auth=False)
    finally:
        srv.shutdown()
    assert any("requestId: Pflichtfeld fehlt" in m for m in contract_issues(cl)), contract_issues(cl)


def test_admin_verifikation_mit_invarianten_ohne_befunde():
    """Korrekte Plattform mit Admin-Key: Invarianten und Referenzbeispiel ohne Abweichung, Durchschnittspreise ok."""
    p = MockPlatform()
    srv, raw, d = _admin_setup(p, "DAY", strictReference=True)
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    v = rep["markets"][0]["verification"]
    assert v["mode"] == "admin" and v["status"] == "OK" and v["referenceDeviations"] == 0
    assert rep["totals"]["errors"] == 0, [f for f in rep["findings"] if f["severity"] == "error"]
