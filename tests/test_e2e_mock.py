"""Ende-zu-Ende: Simulator gegen den Mock – einmal korrekt, einmal mit eingebauten Fehlern."""
from datetime import timedelta
from decimal import Decimal

import pytest

from egsim.config import parse_config
from egsim.mockserver import ApiErr, MockPlatform, start_server
from egsim.simulator import Simulator
from egsim.timeutil import now_utc, resolve_delivery_date


def _config(port, resolution_date="tomorrow", tmp_path=None, **run):
    return parse_config({
        "platform": {"baseUrl": f"http://127.0.0.1:{port}"},
        "admin": {"apiKey": "admin-key"},
        "run": {"seed": 1, "deliveryDates": [resolution_date], "concurrency": 4, "closeMarket": True,
                "reportFile": None, **run},
        "provisioning": {"mode": "register", "stateFile": str(tmp_path / "state.json") if tmp_path else None},
        "behaviour": {"updateProbability": 0.3, "cancelProbability": 0.2, "quarterHour": {"intervals": 12}},
        "communities": [
            {"name": "PV", "count": 2, "generation": {"profile": "solar", "dailyKwh": [40, 80]},
             "priceExpectation": {"probability": 0.7, "range": [0.08, 0.12]}},
            {"name": "Wind", "generation": {"profile": "wind", "dailyKwh": [20, 60]}},
            {"name": "Haushalte", "count": 2, "consumption": {"profile": "household", "dailyKwh": [50, 90]}},
        ],
    })


def _run(platform, resolution="DAY", tmp_path=None, **run):
    d = resolve_delivery_date("tomorrow").isoformat()
    platform.add_market(d, resolution, closes_at=now_utc() + timedelta(hours=1))
    srv = start_server(platform)
    try:
        return Simulator(_config(srv.server_address[1], d, tmp_path, **run)).run()
    finally:
        srv.shutdown()


@pytest.mark.parametrize("resolution", ["DAY", "QUARTER_HOUR"])
def test_korrekte_plattform_ohne_befunde(resolution, tmp_path):
    rep = _run(MockPlatform(), resolution, tmp_path)
    assert rep["totals"]["errors"] == 0, rep["findings"]
    assert rep["totals"]["checksFailed"] == 0
    assert rep["totals"]["checksPassed"] > 60
    m = rep["markets"][0]
    assert m["marketStatusAtEnd"] == "SETTLED"
    assert m["verification"]["status"] == "OK"


def test_reserve_knapp_und_asynchrones_settlement(tmp_path):
    rep = _run(MockPlatform(reserve_capacity=Decimal("1"), settlement_delay=1.5), "DAY", tmp_path)
    assert rep["totals"]["errors"] == 0, rep["findings"]
    assert rep["markets"][0]["closeHttpStatus"] == 202
    assert rep["markets"][0]["verification"]["status"] == "OK"


class KaputtePlattform(MockPlatform):
    """Typische Fehler: keine Eigentümerprüfung bei Positionen, falsche Zuteilungsmenge."""

    def _load_position(self, role, ec, raw_id):
        from egsim.mockserver import _check_uuid
        p = self.positions.get(_check_uuid(raw_id))
        if not p:
            raise ApiErr("POSITION_NOT_FOUND")
        return p  # Mandantentrennung fehlt

    def _settle(self, d):
        super()._settle(d)
        for a in self.results[d]["allocations"]:
            if a["allocatedKwh"] > 0:
                a["allocatedKwh"] = a["allocatedKwh"] + Decimal("0.5")
                break


def test_kaputte_plattform_wird_erkannt(tmp_path):
    rep = _run(KaputtePlattform(), "DAY", tmp_path, lifecycleChecks=False)
    failed = {c["id"] for c in rep["checks"] if c["result"] == "FAIL"}
    assert {"TEN-03", "TEN-04"} <= failed
    assert rep["markets"][0]["verification"]["status"] == "MISMATCH"
    assert rep["totals"]["errors"] > 0
