"""Neue Szenarien und Checks erkennen gezielt eingebaute Plattformfehler (und bestehen gegen den korrekten Mock)."""
import json
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from egsim.config import parse_config
from egsim.mockserver import MockPlatform
from egsim.simulator import Simulator
from egsim.timeutil import format_utc, now_utc

from test_e2e_mock import _admin_setup, _setup


def run_scenarios(platform, ids, resolution="QUARTER_HOUR", strict=False):
    srv, raw, d = _admin_setup(platform, resolution, scenarios=ids, strictReference=strict)
    raw["communities"] = []
    try:
        rep = Simulator(parse_config(raw)).run_scenarios()
    finally:
        srv.shutdown()
    return {s["id"]: s for s in rep["scenarios"]}, rep


def failed_checks(rep):
    return {c["id"]: c["note"] for c in rep["checks"] if c["result"] == "FAIL"}


# ------------------------------------------------------------------------------------------------ Szenarien
@pytest.mark.parametrize("resolution", ["QUARTER_HOUR", "DAY"])
def test_neue_szenarien_bestehen_gegen_korrekten_mock(resolution):
    ids = ["SZ-03", "SZ-13", "SZ-14", "SZ-15", "SZ-16", "SZ-17", "SZ-18"]
    p = MockPlatform()
    if resolution == "DAY":  # im Tagesmarkt braucht jedes Szenario einen eigenen Liefertag
        d0 = now_utc().date()
        for i in range(1, len(ids) + 1):
            p.add_market((d0 + timedelta(days=i)).isoformat(), "DAY", closes_at=now_utc() + timedelta(hours=1))
        srv, raw, _ = _admin_setup(p, "DAY", scenarios=ids, strictReference=True)
        raw["run"]["scenarioDates"] = [(d0 + timedelta(days=i)).isoformat() for i in range(1, len(ids) + 1)]
        raw["communities"] = []
        try:
            rep = Simulator(parse_config(raw)).run_scenarios()
        finally:
            srv.shutdown()
        res = {s["id"]: s for s in rep["scenarios"]}
    else:
        res, rep = run_scenarios(p, ids, strict=True)
    assert {k: v["result"] for k, v in res.items()} == {i: "PASS" for i in ids}, \
        {k: v["messages"] for k, v in res.items() if v["result"] != "PASS"}
    assert rep["totals"]["errors"] == 0, [f for f in rep["findings"] if f["severity"] == "error"]


def test_beispielablauf_aus_dem_vertrag_marge_4_80():
    """SZ-14 mit den Preisen des Vertrags 0.3.1 (0,10 / 0,14 / 0,20): Einkauf 15,00, Verkauf 23,80, Reserve 4,00,
    Marge 4,80. Das Angebot mit Preisvorstellung 0,12 wird trotzdem gehandelt."""
    p = MockPlatform()
    srv, raw, d = _admin_setup(p, "DAY", scenarios=["SZ-14"])
    raw["communities"] = []
    try:
        rep = Simulator(parse_config(raw)).run_scenarios()
        res = p.results[d]["result"]
    finally:
        srv.shutdown()
    assert rep["scenarios"][0]["result"] == "PASS", rep["scenarios"][0]["messages"]
    assert (res["purchaseCostEur"], res["salesRevenueEur"], res["marginEur"]) == (Decimal("15.00"), Decimal("23.80"),
                                                                                  Decimal("4.80"))
    assert res["reserve"]["costEur"] == Decimal("4.00") and res["reserve"]["requiredKwh"] == Decimal("20")
    assert res["prices"]["buyPriceEurPerKwh"] == Decimal("0.10") and res["ruleVersion"]


class OhneIdempotenz(MockPlatform):
    """Prüft Duplikate nicht: derselbe Request wird mehrfach angenommen."""

    def h_submit(self, q, h, raw):
        saved = [p for p in self.positions.values() if p["status"] == "OPEN"]
        for p in saved:
            p["status"] = "CANCELLED"
        try:
            return super().h_submit(q, h, raw)
        finally:
            for p in saved:
                p["status"] = "OPEN"


def test_sz16_erkennt_fehlende_idempotenz():
    res, rep = run_scenarios(OhneIdempotenz(), ["SZ-16", "SZ-01"])
    assert res["SZ-01"]["result"] == "PASS"
    assert res["SZ-16"]["result"] == "FAIL"
    assert any("erwartet genau 1" in m for m in res["SZ-16"]["messages"]), res["SZ-16"]["messages"]


class FalscheReserveAufteilung(MockPlatform):
    """Vertauscht die Reservemengen zweier Bedarfe (statt pro rata)."""

    def _settle(self, d):
        super()._settle(d)
        rt = [t for t in self.results[d]["trades"] if t["fromReserve"]]
        if len(rt) >= 2:
            rt[0]["quantityKwh"], rt[1]["quantityKwh"] = rt[1]["quantityKwh"], rt[0]["quantityKwh"]


@pytest.mark.parametrize("strict", [False, True])
def test_sz15_erkennt_falsche_reserveaufteilung(strict):
    # Aufteilung der Reserve auf die Bedarfe ist Referenzbeispiel Gruppe 1: Warnung, mit strictReference Fehler
    res, _ = run_scenarios(FalscheReserveAufteilung(), ["SZ-15"], strict=strict)
    assert res["SZ-15"]["result"] == ("FAIL" if strict else "PASS")
    assert any("SELLS_RESERVE" in m for m in res["SZ-15"]["messages"]), res["SZ-15"]["messages"]


class AlleAngeboteVoll(MockPlatform):
    """Kauft bei Überangebot alle Angebote voll (Überbuchung) statt pro rata."""

    def _settle(self, d):
        super()._settle(d)
        for a in self.results[d]["allocations"]:
            if a["reason"] == "PARTIALLY_ALLOCATED":
                a["allocatedKwh"] = a["requestedKwh"]


def test_sz17_erkennt_ueberbuchung():
    res, _ = run_scenarios(AlleAngeboteVoll(), ["SZ-17"])  # verletzt Invariante 3, auch ohne strictReference
    assert res["SZ-17"]["result"] == "FAIL"
    assert any("Invariante 3" in m for m in res["SZ-17"]["messages"]), res["SZ-17"]["messages"]


class DoppelterTrade(MockPlatform):
    """Bucht einen Trade doppelt (z. B. Settlement läuft zweimal)."""

    def _settle(self, d):
        super()._settle(d)
        first = {}
        for t in self.results[d]["trades"]:
            first.setdefault(t["intervalStart"], t)
        for t in first.values():  # je Intervall ein Trade doppelt
            self.results[d]["trades"].append({**t, "id": str(uuid.uuid4())})


def test_sz16_und_17_erkennen_doppelte_handelsabschluesse():
    res, _ = run_scenarios(DoppelterTrade(), ["SZ-16", "SZ-17"])
    assert res["SZ-16"]["result"] == "FAIL" and res["SZ-17"]["result"] == "FAIL"
    assert any("Trades im Intervall" in m for m in res["SZ-17"]["messages"]), res["SZ-17"]["messages"]


class FalscherAblehnungsgrund(MockPlatform):
    def close_market(self, d):
        r = super().close_market(d)
        for p in self.positions.values():
            if p["status"] == "REJECTED":
                p["rejectionReason"] = "SUSPENDED"
        return r


def test_sz12_prueft_rejection_reason():
    res, rep = run_scenarios(FalscherAblehnungsgrund(), ["SZ-12"])
    assert res["SZ-12"]["result"] == "FAIL"
    assert any("rejectionReason" in m for m in res["SZ-12"]["messages"]), res["SZ-12"]["messages"]


# ------------------------------------------------------------------------------------------------ Checks
class KaputtAdmin(MockPlatform):
    """Mehrere Vertragsverstöße: PUT mit Menge 0 wird akzeptiert, Token überleben neue Zugangsdaten,
    der Filter type wird ignoriert, ein erneutes close bucht einen zweiten Trade."""

    def handle(self, method, path, query, headers, raw):
        if method == "PUT" and path.startswith("/positions/") and raw:
            b = json.loads(raw, parse_float=Decimal)
            if b.get("quantityKwh") == 0:
                return 200, self.positions[path.rsplit("/", 1)[1]]
        if method == "GET" and path == "/positions":
            query = {k: v for k, v in query.items() if k != "type"}
        return super().handle(method, path, query, headers, raw)

    def h_credentials(self, raw_id, q, h, raw):
        tokens = dict(self.tokens)
        r = super().h_credentials(raw_id, q, h, raw)
        self.tokens = tokens
        return r

    def close_market(self, d):
        with self.lock:
            was = self.markets.get(d, {}).get("status") == "SETTLED"
            r = super().close_market(d)
            if was and self.results[d]["trades"]:
                t = dict(self.results[d]["trades"][0])
                t["id"] = str(uuid.uuid4())
                self.results[d]["trades"].append(t)
            return r


def test_checks_erkennen_vertragsverstoesse():
    p = KaputtAdmin()
    srv, raw, d = _admin_setup(p, "DAY")
    raw["behaviour"].update(updateProbability=0.0, cancelProbability=0.0)
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    failed = failed_checks(rep)
    assert {"POS-04", "ADM-14b", "FIL-01b", "FIL-05", "ADM-22"} <= set(failed), failed
    assert "verändert" in failed["ADM-22"]


class IgnoriertClosesAt(MockPlatform):
    @staticmethod
    def _market_closed(m):
        return m["status"] != "OPEN"


@pytest.mark.parametrize("platform_cls,expected", [(MockPlatform, "PASS"), (IgnoriertClosesAt, "FAIL")])
def test_checks_nach_closes_at_bei_status_open(monkeypatch, platform_cls, expected):
    p = platform_cls()
    srv, raw, d = _setup(p, "QUARTER_HOUR", n=3)
    orig = Simulator._finish_market

    def finish_late(self, run, reader):  # closesAt verstreichen lassen, der Betreiber schließt erst danach (deterministisch)
        p.markets[d]["closesAt"] = format_utc(now_utc() - timedelta(seconds=1))
        threading.Timer(2, p.close_market, args=(d,)).start()
        orig(self, run, reader)
    monkeypatch.setattr(Simulator, "_finish_market", finish_late)
    try:
        rep = Simulator(parse_config({**raw, "run": {**raw["run"], "waitForSettlementMinutes": 1}})).run()
    finally:
        srv.shutdown()
    late = {c["id"]: c["result"] for c in rep["checks"] if c["id"].startswith("CLOSES-")}
    assert set(late) == {"CLOSES-01", "CLOSES-02", "CLOSES-03"}, late
    assert set(late.values()) == {expected}, late


class PreiseAendernSich(MockPlatform):
    def _settle(self, d):
        super()._settle(d)
        self.markets[d]["prices"]["sellPriceEurPerKwh"] += Decimal("0.01")


class UnvollstaendigerMarkt(MockPlatform):
    def _settle(self, d):
        super()._settle(d)
        self.markets[d]["failureReason"] = "kaputt"
        self.markets[d]["settledAt"] = None


@pytest.mark.parametrize("platform_cls,expected,severity", [(PreiseAendernSich, "unverändert", "warning"),
                                                             (UnvollstaendigerMarkt, "failureReason", "error")])
def test_marktzustand_nach_settlement_wird_geprueft(platform_cls, expected, severity):
    # 0.3: Market.prices ist nur eine Preisindikation, eine Änderung ist eine Abweichung vom Referenzbeispiel
    p = platform_cls()
    srv, raw, d = _setup(p, "DAY", n=3)
    try:
        threading.Timer(2, p.close_market, args=(d,)).start()
        rep = Simulator(parse_config({**raw, "run": {**raw["run"], "waitForSettlementMinutes": 1}})).run()
    finally:
        srv.shutdown()
    msgs = [f["message"] for f in rep["findings"] if f["severity"] == severity]
    assert any(expected in m for m in msgs), msgs
    assert rep["markets"][0]["verification"]["status"] == ("MISMATCH" if severity == "error" else "OK")


def test_paralleler_marktschluss_bei_korrekter_plattform():
    p = MockPlatform(settlement_delay=0.5)
    srv, raw, d = _admin_setup(p, "DAY", parallelClose=5)
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    assert rep["totals"]["errors"] == 0, [f for f in rep["findings"] if f["severity"] == "error"]
    assert rep["markets"][0]["verification"]["status"] == "OK"
