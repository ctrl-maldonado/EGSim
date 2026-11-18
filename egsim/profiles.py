"""Erzeugt Angebote/Bedarfe aus Tagesmengen und Lastprofilen."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from egsim.config import CommunityConfig, EnergyFlow, PriceExpectation
from egsim.timeutil import format_utc, local_hour

KWH = Decimal("0.001")
MAX_KWH = Decimal("1000000")


@dataclass
class PlannedPosition:
    interval_start: str | None
    type: str  # OFFER | DEMAND
    quantity: Decimal
    price_expectation: Decimal | None = None

    def body(self, delivery_date: str) -> dict:
        b: dict = {"deliveryDate": delivery_date, "type": self.type, "quantityKwh": self.quantity}
        if self.interval_start is not None:
            b["intervalStart"] = self.interval_start
        if self.price_expectation is not None:
            b["priceExpectationEurPerKwh"] = self.price_expectation
        return b


def shape(profile: str, h: float) -> float:
    """Relative Leistung zur lokalen Uhrzeit h (0–24)."""
    if profile == "solar":
        return max(0.0, math.sin(math.pi * (h - 6.5) / 11.0)) if 6.5 <= h <= 17.5 else 0.0
    if profile == "wind":
        return 1.0 + 0.25 * math.cos(2 * math.pi * (h - 3) / 24)
    if profile == "household":
        return 0.35 + 0.5 * math.exp(-((h - 7.5) / 1.2) ** 2) + 0.9 * math.exp(-((h - 19) / 1.8) ** 2)
    if profile == "business":
        return 1.2 if 8 <= h < 18 else 0.2
    if profile == "night":
        return 1.0 if (h >= 22 or h < 6) else 0.2
    return 1.0  # flat


def _uniform(lo: Decimal, hi: Decimal, rng: random.Random) -> Decimal:
    if lo == hi:
        return lo
    return lo + (hi - lo) * Decimal(str(rng.random()))


def _daily(flow: EnergyFlow | None, rng: random.Random) -> Decimal:
    return _uniform(*flow.daily_kwh, rng) if flow else Decimal(0)


def _per_interval(flow: EnergyFlow | None, daily: Decimal, hours: list[float], all_hours: list[float],
                  rng: random.Random) -> list[Decimal]:
    if not flow or daily == 0:
        return [Decimal(0)] * len(hours)
    total = sum(shape(flow.profile, h) for h in all_hours) or 1.0
    out = []
    for h in hours:
        f = shape(flow.profile, h) / total * (1 + flow.noise * rng.uniform(-1, 1))
        out.append(daily * Decimal(str(max(0.0, f))))
    return out


def _price(pe: PriceExpectation | None, rng: random.Random) -> Decimal | None:
    if not pe or rng.random() >= pe.probability:
        return None
    q = Decimal(1).scaleb(-pe.decimals)
    return _uniform(*pe.range, rng).quantize(q, rounding=ROUND_HALF_UP)


def _to_position(interval: str | None, net: Decimal, comm: CommunityConfig, rng: random.Random) -> PlannedPosition | None:
    qty = abs(net).quantize(KWH, rounding=ROUND_HALF_UP)
    if qty <= 0:
        return None
    qty = min(qty, MAX_KWH)
    if net > 0:
        return PlannedPosition(interval, "OFFER", qty, _price(comm.price_expectation, rng))
    return PlannedPosition(interval, "DEMAND", qty)


def select_intervals(all_intervals: list[datetime], mode: str | int, rng: random.Random) -> list[datetime]:
    if mode == "all":
        return list(all_intervals)
    if mode == "daylight":
        return [t for t in all_intervals if 6 <= local_hour(t) < 20]
    n = min(int(mode), len(all_intervals))
    return sorted(rng.sample(all_intervals, n))


def plan_positions(comm: CommunityConfig, resolution: str, intervals: list[datetime] | None,
                   all_intervals: list[datetime] | None, rng: random.Random) -> list[PlannedPosition]:
    """Eine Position pro Intervall (QUARTER_HOUR) bzw. eine pro Tag (DAY). Prosumer melden den Saldo."""
    gen_daily = _daily(comm.generation, rng)
    con_daily = _daily(comm.consumption, rng)
    if resolution == "DAY":
        p = _to_position(None, gen_daily - con_daily, comm, rng)
        return [p] if p else []
    assert intervals is not None and all_intervals is not None
    hours = [local_hour(t) for t in intervals]
    all_hours = [local_hour(t) for t in all_intervals]
    gen = _per_interval(comm.generation, gen_daily, hours, all_hours, rng)
    con = _per_interval(comm.consumption, con_daily, hours, all_hours, rng)
    out = []
    for t, g, c in zip(intervals, gen, con):
        p = _to_position(format_utc(t), g - c, comm, rng)
        if p:
            out.append(p)
    return out
