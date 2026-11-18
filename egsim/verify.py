"""Prüft Zuteilungen, Handelsabschlüsse und Tagesergebnis der Plattform gegen die Referenzrechnung."""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from egsim.settlement import ZERO, D, Pos, Prices, round_eur, settle
from egsim.timeutil import normalize_interval

if TYPE_CHECKING:
    from egsim.simulator import MarketRun, Simulator

log = logging.getLogger("egsim.verify")


class Diff:
    def __init__(self, sim: "Simulator", delivery: str) -> None:
        self.sim = sim
        self.delivery = delivery
        self.count = 0
        self.compared = 0

    def num(self, where: str, field: str, expected: Any, actual: Any, tol: Decimal = ZERO) -> None:
        self.compared += 1
        try:
            e = D(expected)
            a = None if actual is None or isinstance(actual, bool) else D(actual)
        except ArithmeticError:
            self.fail(where, f"{field}: keine Zahl ({actual!r})")
            return
        if e is None and a is None:
            return
        if e is None or a is None or abs(e - a) > tol:
            self.fail(where, f"{field}: erwartet {e}, Plattform {a}")

    def eq(self, where: str, field: str, expected: Any, actual: Any) -> None:
        self.compared += 1
        if expected != actual:
            self.fail(where, f"{field}: erwartet {expected}, Plattform {actual}")

    def fail(self, where: str, msg: str) -> None:
        self.count += 1
        if self.count <= 200:
            self.sim.findings.error("verify", f"{self.delivery} {where}: {msg}")
        elif self.count == 201:
            self.sim.findings.error("verify", f"{self.delivery}: weitere Abweichungen unterdrückt")


def _list(sim: "Simulator", resp, what: str) -> list | None:
    if resp.status != 200 or not isinstance(resp.body, list):
        sim.findings.error("verify", f"{what}: HTTP {resp.status} {resp.body if isinstance(resp.body, dict) else ''}",
                           resp.short())
        return None
    return resp.body


def verify_market(sim: "Simulator", run: "MarketRun") -> dict:
    d = run.delivery_date
    if sim.admin is None:
        return _verify_eg_views_only(sim, run)
    adm = sim.admin
    diff = Diff(sim, d)
    tol_k, tol_e = sim.cfg.tolerance_kwh, sim.cfg.tolerance_eur

    positions = _list(sim, adm.list_positions(deliveryDate=d), "GET /positions (Admin)")
    result_r = adm.market_result(d)
    allocs = _list(sim, adm.allocations(d), "GET /allocations (Admin)")
    trades = _list(sim, adm.trades(d), "GET /trades (Admin)")
    if result_r.status != 200 or not isinstance(result_r.body, dict):
        sim.findings.error("verify", f"GET /result: HTTP {result_r.status}", result_r.short())
        return {"status": "ERROR"}
    if positions is None or allocs is None or trades is None:
        return {"status": "ERROR"}
    result = result_r.body
    market = run.market or {}
    resolution = market.get("resolution")
    prices = Prices.from_api(result.get("prices") or market.get("prices") or {})

    # --- Positionsstatus nach Settlement
    by_status: dict[str, int] = defaultdict(int)
    for p in positions:
        by_status[p.get("status")] += 1
        if p.get("status") in ("OPEN", "CLOSED"):
            diff.fail(f"Position {p.get('id')}", f"Status {p.get('status')} nach SETTLED (erwartet PROCESSED/CANCELLED/REJECTED)")
    pos_by_id = {p["id"]: p for p in positions}
    for sp in run.positions:
        if not sp.id:
            continue
        pp = pos_by_id.get(sp.id)
        if pp is None:
            diff.fail(f"Position {sp.id}", "eigene Position fehlt in GET /positions (Admin)")
            continue
        expected = "CANCELLED" if sp.cancelled else ("PROCESSED" if sp.eg.status in (None, "ACTIVE") else "REJECTED")
        diff.eq(f"Position {sp.id} [{sp.eg.key}]", "status", expected, pp.get("status"))
        if not sp.cancelled:
            diff.num(f"Position {sp.id} [{sp.eg.key}]", "quantityKwh", sp.quantity, pp.get("quantityKwh"))

    # --- Referenzrechnung (Reserve-Lieferung des Kraftwerks aus dem Plattformergebnis übernommen)
    processed = [Pos.from_api(p) for p in positions if p.get("status") == "PROCESSED"]
    if resolution == "QUARTER_HOUR":
        dispatched = {normalize_interval(i.get("intervalStart")): D((i.get("reserve") or {}).get("dispatchedKwh", 0))
                      for i in result.get("intervals") or []}
    else:
        dispatched = {None: D((result.get("reserve") or {}).get("dispatchedKwh", 0))}
    ref = settle(processed, prices, resolution, dispatched)

    # --- Zuteilungen
    ref_alloc = {a.position.id: a for a in ref.allocations}
    plat_alloc = {}
    for a in allocs:
        pid = a.get("positionId")
        if pid in plat_alloc:
            diff.fail(f"Zuteilung {pid}", "doppelt vorhanden")
        plat_alloc[pid] = a
    for pid, ra in ref_alloc.items():
        pa = plat_alloc.get(pid)
        w = f"Zuteilung {pid}"
        if pa is None:
            diff.fail(w, "fehlt (jede PROCESSED-Position braucht eine Zuteilung, auch mit 0 kWh)")
            continue
        diff.eq(w, "energyCommunityId", ra.position.energy_community_id, pa.get("energyCommunityId"))
        diff.eq(w, "type", ra.position.type, pa.get("type"))
        diff.eq(w, "deliveryDate", d, pa.get("deliveryDate"))
        diff.eq(w, "intervalStart", ra.position.interval_start, normalize_interval(pa.get("intervalStart")))
        diff.num(w, "requestedKwh", ra.position.quantity, pa.get("requestedKwh"))
        diff.num(w, "allocatedKwh", ra.allocated, pa.get("allocatedKwh"), tol_k)
        diff.eq(w, "reason", ra.reason, pa.get("reason"))
    for pid in set(plat_alloc) - set(ref_alloc):
        diff.fail(f"Zuteilung {pid}", "gehört zu keiner PROCESSED-Position")

    # --- Handelsabschlüsse (gruppiert nach Position, Richtung, fromReserve)
    def tkey(pid, direction, fr):
        return (pid, direction, bool(fr))

    ref_tr = {tkey(t.position.id, t.direction, t.from_reserve): t for t in ref.trades}
    plat_tr: dict[tuple, dict] = {}
    seen_ids = set()
    for t in trades:
        w = f"Trade {t.get('id')}"
        if t.get("id") in seen_ids:
            diff.fail(w, "id doppelt")
        seen_ids.add(t.get("id"))
        k = tkey(t.get("positionId"), t.get("direction"), t.get("fromReserve"))
        if k in plat_tr:
            diff.fail(w, f"mehrere Trades für Position {k[0]} {k[1]} fromReserve={k[2]}")
        plat_tr[k] = t
        q, pr, amt = D(t.get("quantityKwh")), D(t.get("priceEurPerKwh")), D(t.get("amountEur"))
        if q is not None and q <= 0:
            diff.fail(w, "quantityKwh <= 0 (Mengen von 0 kWh erzeugen keinen Trade)")
        if q is not None and pr is not None and amt is not None and abs(round_eur(q * pr) - amt) > tol_e:
            diff.fail(w, f"amountEur {amt} != round(quantityKwh × priceEurPerKwh) = {round_eur(q * pr)}")
        if t.get("direction") == "PLATFORM_BUYS" and t.get("fromReserve"):
            diff.fail(w, "fromReserve=true bei PLATFORM_BUYS")
        diff.eq(w, "deliveryDate", d, t.get("deliveryDate"))
    for k, rt in ref_tr.items():
        pt = plat_tr.get(k)
        w = f"Trade Position {k[0]} {k[1]}{' (Reserve)' if k[2] else ''}"
        if pt is None:
            diff.fail(w, f"fehlt (erwartet {rt.quantity} kWh)")
            continue
        diff.eq(w, "energyCommunityId", rt.position.energy_community_id, pt.get("energyCommunityId"))
        diff.eq(w, "intervalStart", rt.position.interval_start, normalize_interval(pt.get("intervalStart")))
        diff.num(w, "quantityKwh", rt.quantity, pt.get("quantityKwh"), tol_k)
        diff.num(w, "priceEurPerKwh", rt.price, pt.get("priceEurPerKwh"))
        diff.num(w, "amountEur", rt.amount, pt.get("amountEur"), tol_e)
    for k in set(plat_tr) - set(ref_tr):
        diff.fail(f"Trade Position {k[0]} {k[1]}", f"unerwartet (fromReserve={k[2]}, {plat_tr[k].get('quantityKwh')} kWh)")

    # --- Tagesergebnis
    w = "Tagesergebnis"
    diff.eq(w, "status", "SETTLED", result.get("status"))
    diff.eq(w, "resolution", resolution, result.get("resolution"))
    for f, v in (("totalOfferedKwh", ref.total_offered), ("tradableOfferedKwh", ref.tradable_offered),
                 ("totalDemandKwh", ref.total_demand), ("totalPurchasedKwh", ref.total_purchased),
                 ("totalSoldKwh", ref.total_sold)):
        diff.num(w, f, v, result.get(f), tol_k)
    for f, v in (("purchaseCostEur", ref.purchase_cost), ("salesRevenueEur", ref.sales_revenue), ("marginEur", ref.margin)):
        diff.num(w, f, v, result.get(f), tol_e)
    _cmp_reserve(diff, w, ref.reserve, result.get("reserve") or {}, tol_k, tol_e)
    if resolution == "QUARTER_HOUR":
        plat_iv = {normalize_interval(i.get("intervalStart")): i for i in result.get("intervals") or []}
        for ri in ref.intervals:
            pi = plat_iv.get(ri.interval_start)
            wi = f"Intervall {ri.interval_start}"
            if pi is None:
                diff.fail(wi, "fehlt in result.intervals")
                continue
            for f, v in (("totalOfferedKwh", ri.total_offered), ("tradableOfferedKwh", ri.tradable_offered),
                         ("totalDemandKwh", ri.total_demand), ("totalPurchasedKwh", ri.total_purchased),
                         ("totalSoldKwh", ri.total_sold)):
                diff.num(wi, f, v, pi.get(f), tol_k)
            _cmp_reserve(diff, wi, ri.reserve, pi.get("reserve") or {}, tol_k, tol_e)
    elif result.get("intervals"):
        diff.fail(w, "intervals ist bei DAY gesetzt")

    # --- Mandantensicht: jede simulierte EG sieht nur ihre eigenen Daten
    eg_view_errors = _verify_eg_views(sim, run, diff, ref_alloc, ref_tr)

    summary = {
        "status": "OK" if diff.count == 0 else "MISMATCH",
        "comparisons": diff.compared,
        "mismatches": diff.count,
        "positionsByStatus": dict(by_status),
        "processedPositions": len(processed),
        "reference": {
            "totalOfferedKwh": str(ref.total_offered), "tradableOfferedKwh": str(ref.tradable_offered),
            "totalDemandKwh": str(ref.total_demand), "reserveRequiredKwh": str(ref.reserve.required),
            "purchaseCostEur": str(ref.purchase_cost), "salesRevenueEur": str(ref.sales_revenue),
            "reserveCostEur": str(ref.reserve.cost), "marginEur": str(ref.margin),
        },
        "egViewErrors": eg_view_errors,
    }
    log.info("Prüfung %s: %s (%d Vergleiche, %d Abweichungen)", d, summary["status"], diff.compared, diff.count)
    return summary


def _cmp_reserve(diff: Diff, where: str, ref, plat: dict, tol_k, tol_e) -> None:
    w = f"{where} reserve"
    diff.eq(w, "status", ref.status, plat.get("status"))
    diff.num(w, "requiredKwh", ref.required, plat.get("requiredKwh"), tol_k)
    diff.num(w, "dispatchedKwh", ref.dispatched, plat.get("dispatchedKwh"), tol_k)
    diff.num(w, "costEur", ref.cost, plat.get("costEur"), tol_e)
    if ref.required > 0:
        diff.num(w, "priceEurPerKwh", ref.price, plat.get("priceEurPerKwh"))


def _verify_eg_views(sim: "Simulator", run: "MarketRun", diff: Diff, ref_alloc: dict, ref_tr: dict) -> int:
    before = diff.count
    d = run.delivery_date
    for eg in sim.egs:
        if not eg.ready:
            continue
        a = _list(sim, eg.client.allocations(d), f"[{eg.key}] GET /allocations")
        t = _list(sim, eg.client.trades(d), f"[{eg.key}] GET /trades")
        if a is None or t is None:
            diff.count += 1
            continue
        foreign = [x for x in a + t if x.get("energyCommunityId") != eg.id]
        if foreign:
            diff.fail(f"[{eg.key}] Mandantentrennung", f"{len(foreign)} fremde Zuteilungen/Trades sichtbar")
        own_ref_alloc = {pid for pid, r in ref_alloc.items() if r.position.energy_community_id == eg.id}
        diff.eq(f"[{eg.key}] eigene Zuteilungen", "positionIds", sorted(own_ref_alloc), sorted(x.get("positionId") for x in a))
        own_ref_tr = sum(1 for k, r in ref_tr.items() if r.position.energy_community_id == eg.id)
        diff.eq(f"[{eg.key}] eigene Trades", "Anzahl", own_ref_tr, len(t))
    return diff.count - before


def _verify_eg_views_only(sim: "Simulator", run: "MarketRun") -> dict:
    """Ohne Admin: nur Konsistenz der eigenen Sicht prüfen (keine vollständige Nachrechnung möglich)."""
    diff = Diff(sim, run.delivery_date)
    own_ids = {sp.id for sp in run.positions if sp.id and not sp.cancelled}
    for eg in sim.egs:
        if not eg.ready:
            continue
        a = _list(sim, eg.client.allocations(run.delivery_date), f"[{eg.key}] GET /allocations") or []
        t = _list(sim, eg.client.trades(run.delivery_date), f"[{eg.key}] GET /trades") or []
        for x in a + t:
            if x.get("energyCommunityId") != eg.id:
                diff.fail(f"[{eg.key}]", "fremder Datensatz sichtbar")
        for x in a:
            if x.get("positionId") in own_ids:
                own_ids.discard(x.get("positionId"))
            if D(x.get("allocatedKwh")) > D(x.get("requestedKwh")):
                diff.fail(f"[{eg.key}] Zuteilung {x.get('positionId')}", "allocatedKwh > requestedKwh")
        for x in t:
            q, p, amt = D(x.get("quantityKwh")), D(x.get("priceEurPerKwh")), D(x.get("amountEur"))
            if None not in (q, p, amt) and round_eur(q * p) != amt:
                diff.fail(f"[{eg.key}] Trade {x.get('id')}", f"amountEur {amt} != {round_eur(q * p)}")
    for pid in own_ids:
        diff.fail(f"Position {pid}", "keine Zuteilung gefunden")
    sim.findings.info("verify", f"{run.delivery_date}: ohne Admin-Zugang nur Teilprüfung der EG-Sicht möglich")
    return {"status": "OK" if diff.count == 0 else "MISMATCH", "mode": "egViewOnly",
            "comparisons": diff.compared, "mismatches": diff.count}
