"""Prüft nach dem Settlement die Zuteilungen und Handelsabschlüsse aus Sicht der simulierten EGs.

Ohne Admin-Zugang sieht jede EG nur ihre eigenen Daten. Geprüft wird deshalb:
- Mandantentrennung (nur eigene Datensätze), Positionsstatus nach Settlement
- je Position: Zuteilung vorhanden und plausibel (Menge, reason), Trades passen zur Zuteilung, Preise, Beträge
- über alle simulierten EGs hinweg: Teilzuteilungs-Anteil gleich, keine Reserve bei Überangebot usw.
- Option „nur simulierte EGs am Markt“: vollständige Nachrechnung mit der Referenzlogik
  (gelieferte Reserve = Summe der Reserve-Trades, weil /result nur der Admin sehen darf)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from egsim.settlement import STEP, ZERO, D, Pos, Prices, round_eur, settle
from egsim.timeutil import normalize_interval, parse_ts

if TYPE_CHECKING:
    from egsim.simulator import MarketRun, Simulator

log = logging.getLogger("egsim.verify")
OFFER_REASONS = {"FULLY_ALLOCATED", "PARTIALLY_ALLOCATED", "PRICE_NOT_MET", "NOT_NEEDED"}
DEMAND_REASONS = {"FULLY_ALLOCATED", "RESERVE_SHORTAGE"}


class Diff:
    def __init__(self, sim: "Simulator", delivery: str) -> None:
        self.sim = sim
        self.delivery = delivery
        self.count = 0
        self.compared = 0
        self.market_final: dict | None = None  # GET /markets/{d} nach dem Settlement

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

    def true(self, where: str, cond: bool, msg: str) -> None:
        self.compared += 1
        if not cond:
            self.fail(where, msg)

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


def _decimals(v) -> int:
    exp = D(v).as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _check_rejection_reason(diff: Diff, where: str, p: dict) -> None:
    """rejectionReason ist nur bei REJECTED gesetzt, und zwar mit dem Grund ENERGY_COMMUNITY_NOT_ACTIVE."""
    reason = p.get("rejectionReason")
    if p.get("status") == "REJECTED":
        diff.true(where, reason == "ENERGY_COMMUNITY_NOT_ACTIVE",
                  f"Status REJECTED, rejectionReason {reason!r} (erwartet ENERGY_COMMUNITY_NOT_ACTIVE)")
    else:
        diff.true(where, reason is None, f"rejectionReason {reason!r} gesetzt, obwohl der Status {p.get('status')} ist")


def _check_market_state(sim: "Simulator", run: "MarketRun", diff: Diff) -> dict | None:
    """Marktzustand nach dem Settlement: Zeitstempel gesetzt, kein failureReason, Preise und Auflösung unverändert."""
    reader = sim.reader()
    if reader is None:
        return None
    r = reader.get_market(run.delivery_date)
    if r.status != 200 or not isinstance(r.body, dict):
        diff.fail("Markt nach Settlement", f"GET /markets/{run.delivery_date}: HTTP {r.status}")
        return None
    fin, start, w = r.body, run.market or {}, "Markt nach Settlement"
    diff.eq(w, "status", "SETTLED", fin.get("status"))
    diff.true(w, fin.get("closedAt") is not None, "closedAt fehlt bei Status SETTLED")
    diff.true(w, fin.get("settledAt") is not None, "settledAt fehlt bei Status SETTLED")
    diff.true(w, fin.get("failureReason") is None, f"failureReason ist gesetzt ({fin.get('failureReason')!r}), "
                                                   "gilt aber nur bei FAILED")
    try:
        if fin.get("closedAt") and fin.get("settledAt"):
            diff.true(w, parse_ts(fin["closedAt"]) <= parse_ts(fin["settledAt"]), "closedAt liegt nach settledAt")
    except (TypeError, ValueError, AttributeError):
        pass  # ungültige Zeitstempel meldet die Schema-Prüfung
    if start.get("resolution") is not None:
        diff.eq(w, "resolution", start.get("resolution"), fin.get("resolution"))
    try:  # der Vertrag erklärt nur Preise und Auflösung für fest, die Frist darf der Betreiber verschieben: nur Hinweis
        if start.get("closesAt") and fin.get("closesAt") and parse_ts(start["closesAt"]) != parse_ts(fin["closesAt"]):
            sim.findings.warning("verify", f"{run.delivery_date}: closesAt hat sich seit dem Start geändert "
                                           f"({start['closesAt']} → {fin['closesAt']})")
    except (TypeError, ValueError, AttributeError):
        pass
    sp, fp = start.get("prices") or {}, fin.get("prices") or {}
    for f in ("buyPriceEurPerKwh", "sellPriceEurPerKwh", "reservePriceEurPerKwh"):
        if f in sp:
            diff.num(w, f"prices.{f} (bleibt bis zum Settlement unverändert)", sp.get(f), fp.get(f))
    return fin


def verify_market(sim: "Simulator", run: "MarketRun") -> dict:
    d = run.delivery_date
    diff = Diff(sim, d)
    tol_k, tol_e = sim.cfg.tolerance_kwh, sim.cfg.tolerance_eur
    market = run.market or {}
    resolution = market.get("resolution")
    prices = Prices.from_api(market.get("prices") or {})
    iv_key = (lambda x: normalize_interval(x)) if resolution == "QUARTER_HOUR" else (lambda x: None)

    all_pos: list[dict] = []
    all_alloc: dict[str, dict] = {}
    all_trades: list[dict] = []
    by_status: dict[str, int] = defaultdict(int)
    tracked = {sp.id: sp for sp in run.positions if sp.id}
    diff.market_final = _check_market_state(sim, run, diff)

    for eg in sim.egs:
        if not eg.ready:
            continue
        who = f"[{eg.key}]"
        pos = _list(sim, eg.client.list_positions(deliveryDate=d), f"{who} GET /positions")
        alloc = _list(sim, eg.client.allocations(d), f"{who} GET /allocations")
        trades = _list(sim, eg.client.trades(d), f"{who} GET /trades")
        if pos is None or alloc is None or trades is None:
            diff.count += 1
            continue
        foreign = [x for x in pos + alloc + trades if x.get("energyCommunityId") != eg.id]
        diff.true(f"{who} Mandantentrennung", not foreign, f"{len(foreign)} fremde Datensätze sichtbar")

        own = {p["id"]: p for p in pos if p.get("energyCommunityId") == eg.id}
        for p in own.values():
            by_status[p.get("status")] += 1
            diff.true(f"{who} Position {p['id']}", p.get("status") not in ("OPEN", "CLOSED"),
                      f"Status {p.get('status')} nach SETTLED (erwartet PROCESSED/CANCELLED/REJECTED)")
            _check_rejection_reason(diff, f"{who} Position {p['id']}", p)
            sp = tracked.get(p["id"])
            if sp:
                diff.eq(f"{who} Position {p['id']}", "status", "CANCELLED" if sp.cancelled else "PROCESSED", p.get("status"))
                if not sp.cancelled:
                    diff.num(f"{who} Position {p['id']}", "quantityKwh", sp.quantity, p.get("quantityKwh"))
        for sp in tracked.values():
            if sp.eg is eg and sp.id not in own:
                diff.fail(f"{who} Position {sp.id}", "eigene Position fehlt in GET /positions")

        alloc_by_pos: dict[str, list[dict]] = defaultdict(list)
        for a in alloc:
            alloc_by_pos[a.get("positionId")].append(a)
        trades_by_pos: dict[str, list[dict]] = defaultdict(list)
        seen = set()
        for t in trades:
            w = f"{who} Trade {t.get('id')}"
            diff.true(w, t.get("id") not in seen, "id doppelt")
            seen.add(t.get("id"))
            diff.true(w, t.get("positionId") in own, "positionId gehört zu keiner eigenen Position")
            trades_by_pos[t.get("positionId")].append(t)
            q, pr, amt = D(t.get("quantityKwh")), D(t.get("priceEurPerKwh")), D(t.get("amountEur"))
            diff.true(w, q is not None and q > 0, "quantityKwh <= 0 (Mengen von 0 kWh erzeugen keinen Trade)")
            if None not in (q, pr, amt):
                diff.true(w, abs(round_eur(q * pr) - amt) <= tol_e,
                          f"amountEur {amt} ≠ round(quantityKwh × priceEurPerKwh) = {round_eur(q * pr)}")
                diff.true(w, _decimals(amt) <= 2, f"amountEur {amt} hat mehr als 2 Nachkommastellen")
            diff.eq(w, "deliveryDate", d, t.get("deliveryDate"))
            if t.get("direction") == "PLATFORM_BUYS":
                diff.true(w, not t.get("fromReserve"), "fromReserve=true bei PLATFORM_BUYS")
                diff.num(w, "priceEurPerKwh (Ankauf)", prices.buy, pr)
            elif t.get("direction") == "PLATFORM_SELLS":
                diff.num(w, "priceEurPerKwh (Verkauf)", prices.sell, pr)
            else:
                diff.fail(w, f"direction {t.get('direction')} unbekannt")

        for pid, p in own.items():
            w = f"{who} Position {pid} ({p.get('type')})"
            al = alloc_by_pos.get(pid, [])
            tr = trades_by_pos.get(pid, [])
            if p.get("status") != "PROCESSED":
                diff.true(w, not al and not tr, f"Status {p.get('status')}, hat aber Zuteilung/Trades")
                continue
            if len(al) != 1:
                diff.fail(w, f"{len(al)} Zuteilungen (erwartet genau 1, auch mit 0 kWh)")
                continue
            a = al[0]
            all_alloc[pid] = a
            all_pos.append(p)
            all_trades += tr
            req, got = D(p.get("quantityKwh")), D(a.get("allocatedKwh"))
            diff.num(w, "requestedKwh", req, a.get("requestedKwh"))
            diff.eq(w, "type", p.get("type"), a.get("type"))
            diff.eq(w, "intervalStart", normalize_interval(p.get("intervalStart")), normalize_interval(a.get("intervalStart")))
            diff.eq(w, "deliveryDate", d, a.get("deliveryDate"))
            if got is None:
                diff.fail(w, "allocatedKwh fehlt")
                continue
            diff.true(w, ZERO <= got <= req, f"allocatedKwh {got} außerhalb 0…{req}")
            diff.true(w, _decimals(got) <= 3, f"allocatedKwh {got} hat mehr als 3 Nachkommastellen")
            reason = a.get("reason")
            if p.get("type") == "OFFER":
                pe = D(p.get("priceExpectationEurPerKwh"))
                too_high = pe is not None and (prices.buy is None or pe > prices.buy)
                diff.true(w, reason in OFFER_REASONS, f"reason {reason} unzulässig für OFFER")
                if too_high:
                    diff.true(w, reason == "PRICE_NOT_MET" and got == 0,
                              f"Preisvorstellung {pe} > Ankaufspreis {prices.buy}: erwartet PRICE_NOT_MET/0, "
                              f"Plattform {reason}/{got}")
                else:
                    diff.true(w, reason != "PRICE_NOT_MET", f"PRICE_NOT_MET obwohl Preisvorstellung {pe} ≤ {prices.buy}")
                if reason == "FULLY_ALLOCATED":
                    diff.true(w, got == req, f"FULLY_ALLOCATED, aber {got} von {req}")
                elif reason == "PARTIALLY_ALLOCATED":
                    diff.true(w, got < req, f"PARTIALLY_ALLOCATED, aber {got} von {req}")
                elif reason == "NOT_NEEDED":
                    diff.true(w, got == 0, f"NOT_NEEDED, aber {got} kWh")
                buys = [t for t in tr if t.get("direction") == "PLATFORM_BUYS"]
                diff.true(w, len(buys) == len(tr), "OFFER hat PLATFORM_SELLS-Trades")
                if got > 0:
                    diff.true(w, len(buys) == 1, f"{len(buys)} PLATFORM_BUYS-Trades (erwartet 1)")
                    if buys:
                        diff.num(w, "Trade quantityKwh", got, buys[0].get("quantityKwh"), tol_k)
                else:
                    diff.true(w, not tr, "Trade trotz 0 kWh Zuteilung")
            else:
                diff.true(w, reason in DEMAND_REASONS, f"reason {reason} unzulässig für DEMAND")
                if reason == "FULLY_ALLOCATED":
                    diff.true(w, got == req, f"FULLY_ALLOCATED, aber {got} von {req}")
                elif reason == "RESERVE_SHORTAGE":
                    diff.true(w, got < req, f"RESERVE_SHORTAGE, aber {got} von {req}")
                sells = [t for t in tr if t.get("direction") == "PLATFORM_SELLS"]
                diff.true(w, len(sells) == len(tr), "DEMAND hat PLATFORM_BUYS-Trades")
                diff.true(w, sum(1 for t in sells if not t.get("fromReserve")) <= 1
                          and sum(1 for t in sells if t.get("fromReserve")) <= 1,
                          "mehr als ein Trade je Anteil (Angebote/Reserve)")
                total = sum((D(t.get("quantityKwh")) or ZERO for t in sells), ZERO)
                diff.num(w, "Summe Trade-Mengen", got, total, tol_k)

    # --- Konsistenz über alle simulierten EGs je Intervall
    groups: dict[Any, list[dict]] = defaultdict(list)
    for p in all_pos:
        groups[iv_key(p.get("intervalStart"))].append(p)
    reserve_trades: dict[Any, Decimal] = defaultdict(lambda: ZERO)
    for t in all_trades:
        if t.get("fromReserve"):
            reserve_trades[iv_key(t.get("intervalStart"))] += D(t.get("quantityKwh")) or ZERO
    for key, ps in groups.items():
        w = f"Intervall {key}" if key else "Tagesmarkt"
        partial = [p for p in ps if all_alloc[p["id"]].get("reason") == "PARTIALLY_ALLOCATED"]
        shortage = [p for p in ps if all_alloc[p["id"]].get("reason") == "RESERVE_SHORTAGE"]
        if partial:
            diff.true(w, reserve_trades[key] == 0 and not shortage,
                      "Teilzuteilung von Angeboten (Überangebot) und gleichzeitig Reserve/Reserveknappheit")
            ratios = [D(all_alloc[p["id"]]["allocatedKwh"]) / D(p["quantityKwh"]) for p in partial]
            spread = max(ratios) - min(ratios)
            tolerance = max(STEP / D(p["quantityKwh"]) for p in partial) * 2
            diff.true(w, spread <= tolerance, f"Teilzuteilung nicht pro rata (Anteile {min(ratios):.4f}…{max(ratios):.4f})")
            full_tradable = [p for p in ps if p.get("type") == "OFFER"
                             and all_alloc[p["id"]].get("reason") in ("FULLY_ALLOCATED", "NOT_NEEDED")]
            diff.true(w, not full_tradable, "handelbare Angebote teils voll, teils anteilig zugeteilt")
        if reserve_trades[key] > 0:
            offers_cut = [p for p in ps if p.get("type") == "OFFER" and all_alloc[p["id"]].get("reason")
                          in ("PARTIALLY_ALLOCATED", "NOT_NEEDED")]
            diff.true(w, not offers_cut, "Reserve genutzt, aber handelbare Angebote nicht voll gekauft")

    # --- Optional: vollständige Nachrechnung, wenn nur simulierte EGs am Markt sind
    full = None
    mode = "egView"
    if getattr(sim, "admin", None) is not None:
        full = verify_market_admin(sim, run, diff)
        mode = "admin" if full else mode
    elif sim.cfg.all_participants_simulated and all_pos:
        full = _full_recalc(diff, d, resolution, prices, all_pos, all_alloc, all_trades, reserve_trades, tol_k, tol_e)
        mode = "full" if full else mode

    summary = {
        "status": "OK" if diff.count == 0 else "MISMATCH",
        "mode": mode,
        "comparisons": diff.compared,
        "mismatches": diff.count,
        "positionsByStatus": dict(by_status),
        "processedPositions": len(all_pos),
        "trades": len(all_trades),
        "ownPurchaseEur": str(sum((D(t["amountEur"]) for t in all_trades if t.get("direction") == "PLATFORM_BUYS"), ZERO)),
        "ownSalesEur": str(sum((D(t["amountEur"]) for t in all_trades if t.get("direction") == "PLATFORM_SELLS"), ZERO)),
        "reference": full,
    }
    log.info("Prüfung %s: %s (%d Vergleiche, %d Abweichungen)", d, summary["status"], diff.compared, diff.count)
    return summary


def _full_recalc(diff: Diff, d, resolution, prices, all_pos, all_alloc, all_trades, reserve_trades, tol_k, tol_e) -> dict:
    processed = [Pos.from_api(p) for p in all_pos]
    dispatched = dict(reserve_trades) if resolution == "QUARTER_HOUR" else {None: reserve_trades.get(None, ZERO)}
    ref = settle(processed, prices, resolution, dispatched)
    for ra in ref.allocations:
        pa = all_alloc.get(ra.position.id, {})
        w = f"Nachrechnung Position {ra.position.id}"
        diff.num(w, "allocatedKwh", ra.allocated, pa.get("allocatedKwh"), tol_k)
        diff.eq(w, "reason", ra.reason, pa.get("reason"))
    plat = {(t.get("positionId"), t.get("direction"), bool(t.get("fromReserve"))): t for t in all_trades}
    diff.true(f"Nachrechnung {d}", len(plat) == len(all_trades), "doppelte Handelsabschlüsse")
    refk = {(t.position.id, t.direction, t.from_reserve): t for t in ref.trades}
    for k, rt in refk.items():
        pt = plat.get(k)
        w = f"Nachrechnung Trade {k[0]} {k[1]}{' (Reserve)' if k[2] else ''}"
        if pt is None:
            diff.fail(w, f"fehlt (erwartet {rt.quantity} kWh)")
            continue
        diff.num(w, "quantityKwh", rt.quantity, pt.get("quantityKwh"), tol_k)
        diff.num(w, "amountEur", rt.amount, pt.get("amountEur"), tol_e)
    for k in set(plat) - set(refk):
        diff.fail(f"Nachrechnung Trade {k[0]} {k[1]}", f"unerwartet ({plat[k].get('quantityKwh')} kWh)")
    return {
        "totalOfferedKwh": str(ref.total_offered), "tradableOfferedKwh": str(ref.tradable_offered),
        "totalDemandKwh": str(ref.total_demand), "reserveRequiredKwh": str(ref.reserve.required),
        "reserveDispatchedKwh": str(ref.reserve.dispatched), "purchaseCostEur": str(ref.purchase_cost),
        "salesRevenueEur": str(ref.sales_revenue), "reserveCostEur": str(ref.reserve.cost), "marginEur": str(ref.margin),
    }


# ======================================================================================================================
def verify_market_admin(sim: "Simulator", run: "MarketRun", diff: Diff) -> dict | None:
    """Mit Admin-Key: alle Positionen des Markts laden, komplett nachrechnen und mit /allocations, /trades und
    /result (Tagesergebnis inkl. Reserve) vergleichen."""
    adm = sim.admin
    d = run.delivery_date
    tol_k, tol_e = sim.cfg.tolerance_kwh, sim.cfg.tolerance_eur
    positions = _list(sim, adm.list_positions(deliveryDate=d), "Admin: GET /positions")
    allocs = _list(sim, adm.allocations(d), "Admin: GET /allocations")
    trades = _list(sim, adm.trades(d), "Admin: GET /trades")
    rr = adm.market_result(d)
    if rr.status != 200 or not isinstance(rr.body, dict):
        sim.findings.error("verify", f"Admin: GET /result: HTTP {rr.status}", rr.short())
        return None
    if positions is None or allocs is None or trades is None:
        return None
    result = rr.body
    resolution = (run.market or {}).get("resolution")
    prices = Prices.from_api(result.get("prices") or (run.market or {}).get("prices") or {})
    for p in positions:
        diff.true(f"Admin: Position {p.get('id')}", p.get("status") not in ("OPEN", "CLOSED"),
                  f"Status {p.get('status')} nach SETTLED")
        _check_rejection_reason(diff, f"Admin: Position {p.get('id')}", p)
    fin = diff.market_final or {}
    for f in ("buyPriceEurPerKwh", "sellPriceEurPerKwh", "reservePriceEurPerKwh"):
        if f in (fin.get("prices") or {}):
            diff.num("Admin: Tagesergebnis", f"prices.{f} (wie im Markt)", fin["prices"][f], (result.get("prices") or {}).get(f))
    try:
        if fin.get("settledAt") and result.get("settledAt"):
            diff.eq("Admin: Tagesergebnis", "settledAt (wie im Markt)", parse_ts(fin["settledAt"]), parse_ts(result["settledAt"]))
    except (TypeError, ValueError, AttributeError):
        pass

    processed = [Pos.from_api(p) for p in positions if p.get("status") == "PROCESSED"]
    if resolution == "QUARTER_HOUR":
        dispatched = {normalize_interval(i.get("intervalStart")): D((i.get("reserve") or {}).get("dispatchedKwh", 0))
                      for i in result.get("intervals") or []}
    else:
        dispatched = {None: D((result.get("reserve") or {}).get("dispatchedKwh", 0))}
    ref = settle(processed, prices, resolution, dispatched)

    plat_alloc = {a.get("positionId"): a for a in allocs}
    diff.true("Admin: Zuteilungen", len(plat_alloc) == len(allocs), "positionId doppelt")
    for ra in ref.allocations:
        pa = plat_alloc.get(ra.position.id)
        w = f"Admin: Zuteilung {ra.position.id}"
        if pa is None:
            diff.fail(w, "fehlt")
            continue
        diff.eq(w, "energyCommunityId", ra.position.energy_community_id, pa.get("energyCommunityId"))
        diff.num(w, "allocatedKwh", ra.allocated, pa.get("allocatedKwh"), tol_k)
        diff.eq(w, "reason", ra.reason, pa.get("reason"))
    for pid in set(plat_alloc) - {a.position.id for a in ref.allocations}:
        diff.fail(f"Admin: Zuteilung {pid}", "gehört zu keiner PROCESSED-Position")

    plat_tr = {(t.get("positionId"), t.get("direction"), bool(t.get("fromReserve"))): t for t in trades}
    diff.true("Admin: Trades", len(plat_tr) == len(trades),
              "doppelte Handelsabschlüsse (gleiche positionId, direction und fromReserve mehrfach)")
    diff.true("Admin: Trades", len({t.get("id") for t in trades}) == len(trades), "Trade-IDs nicht eindeutig")
    ref_tr = {(t.position.id, t.direction, t.from_reserve): t for t in ref.trades}
    for k, rt in ref_tr.items():
        pt = plat_tr.get(k)
        w = f"Admin: Trade {k[0]} {k[1]}{' (Reserve)' if k[2] else ''}"
        if pt is None:
            diff.fail(w, f"fehlt (erwartet {rt.quantity} kWh)")
            continue
        diff.num(w, "quantityKwh", rt.quantity, pt.get("quantityKwh"), tol_k)
        diff.num(w, "priceEurPerKwh", rt.price, pt.get("priceEurPerKwh"))
        diff.num(w, "amountEur", rt.amount, pt.get("amountEur"), tol_e)
    for k in set(plat_tr) - set(ref_tr):
        diff.fail(f"Admin: Trade {k[0]} {k[1]}", f"unerwartet ({plat_tr[k].get('quantityKwh')} kWh)")

    w = "Admin: Tagesergebnis"
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
            wi = f"Admin: Intervall {ri.interval_start}"
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
    return {
        "totalOfferedKwh": str(ref.total_offered), "tradableOfferedKwh": str(ref.tradable_offered),
        "totalDemandKwh": str(ref.total_demand), "reserveRequiredKwh": str(ref.reserve.required),
        "reserveDispatchedKwh": str(ref.reserve.dispatched), "purchaseCostEur": str(ref.purchase_cost),
        "salesRevenueEur": str(ref.sales_revenue), "reserveCostEur": str(ref.reserve.cost), "marginEur": str(ref.margin),
    }


def _cmp_reserve(diff: Diff, where: str, ref, plat: dict, tol_k, tol_e) -> None:
    w = f"{where} reserve"
    diff.eq(w, "status", ref.status, plat.get("status"))
    diff.num(w, "requiredKwh", ref.required, plat.get("requiredKwh"), tol_k)
    diff.num(w, "dispatchedKwh", ref.dispatched, plat.get("dispatchedKwh"), tol_k)
    diff.num(w, "costEur", ref.cost, plat.get("costEur"), tol_e)
    if ref.required > 0:
        diff.num(w, "priceEurPerKwh", ref.price, plat.get("priceEurPerKwh"))
