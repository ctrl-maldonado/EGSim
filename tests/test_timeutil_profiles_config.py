import random
from datetime import date
from decimal import Decimal

import pytest

from egsim.config import ConfigError, parse_config
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


BASE = {"admin": {"apiKey": "k"}, "communities": [
    {"name": "PV", "generation": {"profile": "solar", "dailyKwh": [50, 100]},
     "priceExpectation": {"probability": 1, "range": [0.08, 0.12], "decimals": 3}},
    {"name": "Pro", "generation": {"profile": "solar", "dailyKwh": 10}, "consumption": {"profile": "flat", "dailyKwh": 30}},
]}


def test_plan_tagesmarkt_und_prosumer_saldo():
    cfg = parse_config(BASE)
    pv, pro = cfg.communities
    p = plan_positions(pv, "DAY", None, None, random.Random(1))[0]
    assert p.type == "OFFER" and 50 <= p.quantity <= 100 and p.interval_start is None
    assert -p.price_expectation.as_tuple().exponent <= 3
    q = plan_positions(pro, "DAY", None, None, random.Random(1))[0]
    assert q.type == "DEMAND" and q.quantity == Decimal("20.000") and q.price_expectation is None


def test_plan_viertelstunden():
    cfg = parse_config(BASE)
    ivs = quarter_hours(date(2026, 11, 20))
    ps = plan_positions(cfg.communities[0], "QUARTER_HOUR", ivs, ivs, random.Random(2))
    assert ps and all(p.quantity > 0 and -p.quantity.as_tuple().exponent <= 3 for p in ps)
    assert all(p.interval_start.endswith(("00:00Z", "15:00Z", "30:00Z", "45:00Z")) for p in ps)
    assert len(select_intervals(ivs, 10, random.Random(0))) == 10


def test_config_umgebungsvariablen(monkeypatch, tmp_path):
    from egsim.config import load_config
    monkeypatch.setenv("EGSIM_TEST_KEY", "geheim")
    f = tmp_path / "c.yaml"
    f.write_text("admin:\n  apiKey: ${EGSIM_TEST_KEY}\nplatform:\n  baseUrl: ${NICHT_GESETZT:-http://x:1}\n"
                 "communities: [{name: A, consumption: {profile: flat, dailyKwh: 1}}]\n")
    cfg = load_config(f)
    assert cfg.admin_api_key == "geheim" and cfg.base_url == "http://x:1"


def test_config_fehler():
    with pytest.raises(ConfigError, match="Admin"):
        parse_config({"communities": BASE["communities"]})
    with pytest.raises(ConfigError, match="profile"):
        parse_config({**BASE, "communities": [{"name": "X", "generation": {"profile": "atom", "dailyKwh": 1}}]})
    with pytest.raises(ConfigError, match="generation"):
        parse_config({**BASE, "communities": [{"name": "X"}]})
