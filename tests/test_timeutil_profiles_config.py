import random
from datetime import date
from decimal import Decimal

import pytest

from egsim.config import ConfigError, default_config_dict, new_community_dict, parse_config
from egsim.profiles import plan_positions, select_intervals
from egsim.timeutil import format_utc, quarter_hours, resolve_delivery_date


def test_intervalle_pro_tag():
    assert len(quarter_hours(date(2026, 11, 20))) == 96
    assert len(quarter_hours(date(2026, 3, 29))) == 92   # Sommerzeit
    assert len(quarter_hours(date(2026, 10, 25))) == 100  # Winterzeit
    assert format_utc(quarter_hours(date(2026, 11, 20))[0]) == "2026-11-19T23:00:00Z"


def test_liefertag_aufloesen():
    today = date(2026, 11, 18)
    assert resolve_delivery_date("tomorrow", today) == date(2026, 11, 19)
    assert resolve_delivery_date("+3", today) == date(2026, 11, 21)
    assert resolve_delivery_date("2026-12-01", today) == date(2026, 12, 1)
    with pytest.raises(ValueError):
        resolve_delivery_date("morgen", today)


def _cfg(*communities):
    raw = default_config_dict()
    raw["communities"] = list(communities)
    return parse_config(raw)


PV = new_community_dict("PV", generation={"profile": "solar", "dailyKwh": [50, 100]},
                        priceExpectation={"probability": 1, "range": [0.08, 0.12], "decimals": 3})
PRO = new_community_dict("Pro", generation={"profile": "solar", "dailyKwh": 10},
                         consumption={"profile": "flat", "dailyKwh": 30})


def test_standardkonfiguration_ist_gueltig():
    cfg = parse_config(default_config_dict())
    assert cfg.communities and not cfg.communities[0].has_credentials


def test_plan_tagesmarkt_und_prosumer_saldo():
    pv, pro = _cfg(PV, PRO).communities
    p = plan_positions(pv, "DAY", None, None, random.Random(1))[0]
    assert p.type == "OFFER" and 50 <= p.quantity <= 100 and p.interval_start is None
    assert -p.price_expectation.as_tuple().exponent <= 3
    q = plan_positions(pro, "DAY", None, None, random.Random(1))[0]
    assert q.type == "DEMAND" and q.quantity == Decimal("20.000") and q.price_expectation is None


def test_plan_viertelstunden():
    pv = _cfg(PV).communities[0]
    ivs = quarter_hours(date(2026, 11, 20))
    ps = plan_positions(pv, "QUARTER_HOUR", ivs, ivs, random.Random(2))
    assert ps and all(p.quantity > 0 and -p.quantity.as_tuple().exponent <= 3 for p in ps)
    assert all(p.interval_start.endswith(("00:00Z", "15:00Z", "30:00Z", "45:00Z")) for p in ps)
    assert len(select_intervals(ivs, 10, random.Random(0))) == 10


def test_zugangsdaten_je_anmeldeart():
    c = _cfg(new_community_dict("A", auth="bearer", email="a@x.example", password="pw",
                                consumption={"profile": "flat", "dailyKwh": 1})).communities[0]
    assert c.has_credentials
    c = _cfg(new_community_dict("B", consumption={"profile": "flat", "dailyKwh": 1})).communities[0]
    assert not c.has_credentials


def test_umgebungsvariablen(monkeypatch):
    monkeypatch.setenv("EGSIM_TEST_KEY", "geheim")
    c = _cfg(new_community_dict("A", apiKey="${EGSIM_TEST_KEY}", consumption={"profile": "flat", "dailyKwh": 1}))
    assert c.communities[0].api_key == "geheim"


def test_config_fehler():
    with pytest.raises(ConfigError, match="Profil"):
        _cfg(new_community_dict("X", generation={"profile": "atom", "dailyKwh": 1}))
    # EG ohne Profil ist erlaubt (reicht im Zufallslauf nichts ein, dient z. B. als Szenario-EG)
    assert _cfg(new_community_dict("X")).communities[0].generation is None
    with pytest.raises(ConfigError, match="eindeutig"):
        _cfg(PV, dict(PV, uid="other"))
    raw = default_config_dict()
    raw["run"]["deliveryDates"] = ["übermorgen"]
    with pytest.raises(ConfigError, match="Liefertag"):
        parse_config(raw)
    # deaktivierte EG ohne Profil ist erlaubt
    assert _cfg(new_community_dict("Y", enabled=False)).communities[0].enabled is False
