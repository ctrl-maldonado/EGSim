"""Prüft nach dem Settlement die Zuteilungen und Handelsabschlüsse aus Sicht der simulierten EGs.

Vertrag 0.3.1: Verbindlich sind nur die neun Invarianten im Abschnitt „Handelslogik“. Verstöße dagegen sind Fehler.
Abweichungen vom Referenzbeispiel Gruppe 1 (feste Preise, pro rata, Rundung, ein Trade je Anteil) sind nur Warnungen,
außer die Option „Referenzbeispiel streng“ (run.strictReference) ist gesetzt.

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

from egsim.settlement import STEP, ZERO, D, Pos, Prices, avg_price, round_eur, settle
from egsim.timeutil import normalize_interval, parse_ts

if TYPE_CHECKING:
    from egsim.simulator import MarketRun, Simulator

log = logging.getLogger("egsim.verify")
OFFER_REASONS = {"FULLY_ALLOCATED", "PARTIALLY_ALLOCATED", "NOT_ALLOCATED", "NOT_NEEDED"}
DEMAND_REASONS = {"FULLY_ALLOCATED", "RESERVE_SHORTAGE"}


class Diff:
    def __init__(self, sim: "Simulator", delivery: str) -> None:
        self.sim = sim
        self.delivery = delivery
        self.count = 0
        self.ref_count = 0       # Abweichungen vom Referenzbeispiel Gruppe 1 (nicht verbindlich)
        self.compared = 0
        self.strict_ref = bool(getattr(sim.cfg, "strict_reference", False))
        self.market_final: dict | None = None  # GET /markets/{d} nach dem Settlement

    def num(self, where: str, field: str, expected: Any, actual: Any, tol: Decimal = ZERO, ref: bool = False) -> None:
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
            self.fail(where, f"{field}: erwartet {e}, Plattform {a}", ref)

    def eq(self, where: str, field: str, expected: Any, actual: Any, ref: bool = False) -> None:
        self.compared += 1
        if expected != actual:
            self.fail(where, f"{field}: erwartet {expected}, Plattform {actual}", ref)

    def true(self, where: str, cond: bool, msg: str, ref: bool = False) -> None:
        self.compared += 1
        if not cond:
            self.fail(where, msg, ref)

    def fail(self, where: str, msg: str, ref: bool = False) -> None:
        if ref and not self.strict_ref:
            self.ref_count += 1
            if self.ref_count <= 100:
                self.sim.findings.warning("reference", f"{self.delivery} {where}: {msg} (Referenzbeispiel Gruppe 1, "
                                                       "nicht verbindlich)")
            return
        if ref:
            msg += " (Referenzbeispiel Gruppe 1)"
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
            diff.num(w, f"prices.{f} (Preisindikation bis zum Settlement unverändert)", sp.get(f), fp.get(f), ref=True)
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
                if prices.buy is not None:
                    diff.num(w, "priceEurPerKwh (Ankauf wie Market.prices)", prices.buy, pr, ref=True)
            elif t.get("direction") == "PLATFORM_SELLS":
                if prices.sell is not None:
                    diff.num(w, "priceEurPerKwh (Verkauf wie Market.prices)", prices.sell, pr, ref=True)
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
                diff.true(w, reason in OFFER_REASONS, f"reason {reason} unzulässig für OFFER")
                if reason == "FULLY_ALLOCATED":
                    diff.true(w, got == req, f"FULLY_ALLOCATED, aber {got} von {req}")
                elif reason == "PARTIALLY_ALLOCATED":
                    diff.true(w, ZERO < got < req, f"PARTIALLY_ALLOCATED, aber {got} von {req}")
                elif reason in ("NOT_NEEDED", "NOT_ALLOCATED"):
                    diff.true(w, got == 0, f"{reason}, aber {got} kWh")
                buys = [t for t in tr if t.get("direction") == "PLATFORM_BUYS"]
                diff.true(w, len(buys) == len(tr), "OFFER hat PLATFORM_SELLS-Trades")
                if got > 0:
                    diff.true(w, len(buys) >= 1, "gekauftes Angebot ohne PLATFORM_BUYS-Trade")
                    diff.true(w, len(buys) <= 1, f"{len(buys)} PLATFORM_BUYS-Trades", ref=True)
                    total = sum((D(t.get("quantityKwh")) or ZERO for t in buys), ZERO)
                    diff.num(w, "Summe Trade-Mengen", got, total, tol_k)
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
                          "mehr als ein Trade je Anteil (Angebote/Reserve)", ref=True)
                if got > 0:
                    diff.true(w, len(sells) >= 1, "gedeckter Bedarf ohne PLATFORM_SELLS-Trade")
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
        offers = [p for p in ps if p.get("type") == "OFFER"]
        cut = [p for p in offers if all_alloc[p["id"]].get("reason") in ("PARTIALLY_ALLOCATED", "NOT_ALLOCATED")]
        shortage = [p for p in ps if all_alloc[p["id"]].get("reason") == "RESERVE_SHORTAGE"]
        if cut:  # Überangebot (S > D): keine Reserve, kein gekürzter Bedarf (Invarianten 3, 4)
            diff.true(w, reserve_trades[key] == 0 and not shortage,
                      "Angebote gekürzt (Überangebot) und gleichzeitig Reserve/Reserveknappheit")
            partial = [p for p in cut if all_alloc[p["id"]].get("reason") == "PARTIALLY_ALLOCATED"]
            if partial:
                ratios = [D(all_alloc[p["id"]]["allocatedKwh"]) / D(p["quantityKwh"]) for p in partial]
                spread = max(ratios) - min(ratios)
                tolerance = max(STEP / D(p["quantityKwh"]) for p in partial) * 2
                diff.true(w, spread <= tolerance,
                          f"Teilzuteilung nicht pro rata (Anteile {min(ratios):.4f}…{max(ratios):.4f})", ref=True)
            full = [p for p in offers if all_alloc[p["id"]].get("reason") in ("FULLY_ALLOCATED", "NOT_NEEDED")]
            diff.true(w, not full, "Angebote teils voll, teils gekürzt zugeteilt", ref=True)
        if reserve_trades[key] > 0:  # Reserve nur bei D > S: dann wird jedes Angebot voll gekauft (Invariante 3)
            offers_cut = [p for p in offers if all_alloc[p["id"]].get("reason") != "FULLY_ALLOCATED"]
            diff.true(w, not offers_cut, "Reserve genutzt, aber nicht alle Angebote voll gekauft")

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
        "referenceDeviations": diff.ref_count,
        "strictReference": diff.strict_ref,
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
    """Vergleich mit dem Referenzbeispiel Gruppe 1 (nicht verbindlich, daher ref=True)."""
    processed = [Pos.from_api(p) for p in all_pos]
    _check_totals_invariants(diff, f"{d} (simulierte EGs)", resolution, processed, all_trades, reserve_trades, tol_k)
    if prices.buy is None or prices.sell is None:
        diff.sim.findings.info("verify", f"{d}: Markt ohne Preisindikation – Nachrechnung mit dem Referenzbeispiel "
                                         "übersprungen, geprüft wurden nur die Invarianten")
        return {}
    dispatched = dict(reserve_trades) if resolution == "QUARTER_HOUR" else {None: reserve_trades.get(None, ZERO)}
    ref = settle(processed, prices, resolution, dispatched)
    for ra in ref.allocations:
        pa = all_alloc.get(ra.position.id, {})
        w = f"Nachrechnung Position {ra.position.id}"
        diff.num(w, "allocatedKwh", ra.allocated, pa.get("allocatedKwh"), tol_k, ref=True)
        diff.eq(w, "reason", ra.reason, pa.get("reason"), ref=True)
    plat = {(t.get("positionId"), t.get("direction"), bool(t.get("fromReserve"))): t for t in all_trades}
    diff.true(f"Nachrechnung {d}", len(plat) == len(all_trades), "mehrere Trades je Position und Richtung", ref=True)
    refk = {(t.position.id, t.direction, t.from_reserve): t for t in ref.trades}
    for k, rt in refk.items():
        pt = plat.get(k)
        w = f"Nachrechnung Trade {k[0]} {k[1]}{' (Reserve)' if k[2] else ''}"
        if pt is None:
            diff.fail(w, f"fehlt (erwartet {rt.quantity} kWh)", ref=True)
            continue
        diff.num(w, "quantityKwh", rt.quantity, pt.get("quantityKwh"), tol_k, ref=True)
        diff.num(w, "amountEur", rt.amount, pt.get("amountEur"), tol_e, ref=True)
    for k in set(plat) - set(refk):
        diff.fail(f"Nachrechnung Trade {k[0]} {k[1]}", f"unerwartet ({plat[k].get('quantityKwh')} kWh)", ref=True)
    return _ref_summary(ref)


def _ref_summary(ref) -> dict:
    return {
        "totalOfferedKwh": str(ref.total_offered),
        "totalDemandKwh": str(ref.total_demand), "reserveRequiredKwh": str(ref.reserve.required),
        "reserveDispatchedKwh": str(ref.reserve.dispatched), "purchaseCostEur": str(ref.purchase_cost),
        "salesRevenueEur": str(ref.sales_revenue), "reserveCostEur": str(ref.reserve.cost), "marginEur": str(ref.margin),
    }


def _qty(trades: list[dict], direction: str, from_reserve: bool | None = None) -> Decimal:
    return sum((D(t.get("quantityKwh")) or ZERO for t in trades if t.get("direction") == direction
                and (from_reserve is None or bool(t.get("fromReserve")) == from_reserve)), ZERO)


def _amount(trades: list[dict], direction: str) -> Decimal:
    return sum((D(t.get("amountEur")) or ZERO for t in trades if t.get("direction") == direction), ZERO)


def _check_totals_invariants(diff: Diff, where: str, resolution, processed: list[Pos], trades: list[dict],
                             dispatched: dict, tol_k) -> None:
    """Invarianten 3 und 4 je Intervall: Ankauf = min(S, D), Reserve nur bis max(D - S, 0), ohne Nachfrage nichts."""
    key = (lambda x: normalize_interval(x)) if resolution == "QUARTER_HOUR" else (lambda x: None)
    groups: dict[Any, list[Pos]] = defaultdict(list)
    for p in processed:
        groups[p.interval_start if resolution == "QUARTER_HOUR" else None].append(p)
    tr_by: dict[Any, list[dict]] = defaultdict(list)
    for t in trades:
        tr_by[key(t.get("intervalStart"))].append(t)
    for k, ps in groups.items():
        w = f"{where} {'Intervall ' + k if k else 'Tag'}"
        S = sum((p.quantity for p in ps if p.type == "OFFER"), ZERO)
        Dm = sum((p.quantity for p in ps if p.type == "DEMAND"), ZERO)
        tr = tr_by.get(k, [])
        bought = _qty(tr, "PLATFORM_BUYS")
        diff.num(w, "Ankauf = min(S, D) (Invariante 3)", min(S, Dm), bought, tol_k)
        disp = D(dispatched.get(k, ZERO)) or ZERO
        diff.true(w, disp <= max(Dm - S, ZERO) + tol_k,
                  f"Reserve {disp} kWh größer als max(D - S, 0) = {max(Dm - S, ZERO)} (Invariante 4)")
        if Dm == 0:
            diff.true(w, not tr, "ohne Nachfrage gibt es weder Ankauf noch Verkauf (Invariante 4)")
        sold = _qty(tr, "PLATFORM_SELLS")
        diff.num(w, "Verkauf = Ankauf + Reserve (Mengenbilanz, Vorschlag)", bought + _qty(tr, "PLATFORM_SELLS", True),
                 sold, tol_k, ref=True)


# ======================================================================================================================
def verify_market_admin(sim: "Simulator", run: "MarketRun", diff: Diff) -> dict | None:
    """Mit Admin-Key: alle Positionen, Zuteilungen, Trades und /result laden, die Invarianten prüfen und zusätzlich mit
    dem Referenzbeispiel Gruppe 1 vergleichen (nicht verbindlich)."""
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
    fin = diff.market_final or {}
    indication = (fin.get("prices") or (run.market or {}).get("prices"))
    prices = Prices.from_api(indication or {})
    for p in positions:
        diff.true(f"Admin: Position {p.get('id')}", p.get("status") not in ("OPEN", "CLOSED"),
                  f"Status {p.get('status')} nach SETTLED")
        _check_rejection_reason(diff, f"Admin: Position {p.get('id')}", p)
    try:
        if fin.get("settledAt") and result.get("settledAt"):
            diff.eq("Admin: Tagesergebnis", "settledAt (wie im Markt)", parse_ts(fin["settledAt"]), parse_ts(result["settledAt"]))
    except (TypeError, ValueError, AttributeError):
        pass

    processed_raw = [p for p in positions if p.get("status") == "PROCESSED"]
    processed = [Pos.from_api(p) for p in processed_raw]
    by_id = {p.id: p for p in processed}

    # --- Zuteilungen: genau eine je PROCESSED-Position, nie mehr als eingereicht (Invarianten 1, 2)
    plat_alloc: dict[str, dict] = {}
    for a in allocs:
        pid = a.get("positionId")
        w = f"Admin: Zuteilung {pid}"
        diff.true(w, pid not in plat_alloc, "positionId doppelt")
        plat_alloc[pid] = a
        pos = by_id.get(pid)
        if pos is None:
            diff.fail(w, "gehört zu keiner PROCESSED-Position (Invariante 1)")
            continue
        got = D(a.get("allocatedKwh"))
        diff.eq(w, "energyCommunityId", pos.energy_community_id, a.get("energyCommunityId"))
        diff.true(w, got is not None and ZERO <= got <= pos.quantity,
                  f"allocatedKwh {got} außerhalb 0…{pos.quantity} (Invariante 2)")
    for pid in set(by_id) - set(plat_alloc):
        diff.fail(f"Admin: Zuteilung {pid}", "fehlt für eine PROCESSED-Position")

    # --- Trades: Betrag (Invariante 7), Mengen je Position = Zuteilung
    diff.true("Admin: Trades", len({t.get("id") for t in trades}) == len(trades), "Trade-IDs nicht eindeutig")
    tr_by_pos: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        tr_by_pos[t.get("positionId")].append(t)
        q, pr, amt = D(t.get("quantityKwh")), D(t.get("priceEurPerKwh")), D(t.get("amountEur"))
        if None not in (q, pr, amt):
            diff.true(f"Admin: Trade {t.get('id')}", abs(round_eur(q * pr) - amt) <= tol_e,
                      f"amountEur {amt} ≠ round(quantityKwh × priceEurPerKwh) = {round_eur(q * pr)} (Invariante 7)")
    for pid, a in plat_alloc.items():
        got = D(a.get("allocatedKwh")) or ZERO
        total = sum((D(t.get("quantityKwh")) or ZERO for t in tr_by_pos.get(pid, [])), ZERO)
        diff.num(f"Admin: Position {pid}", "Summe Trade-Mengen = Zuteilung", got, total, tol_k)
    for pid in set(tr_by_pos) - set(plat_alloc):
        diff.fail(f"Admin: Trade zu Position {pid}", "Trade ohne Zuteilung")

    # --- Tagesergebnis: Summen (Invariante 6), Reserve (4, 5), Marge (8), Durchschnittspreise
    if resolution == "QUARTER_HOUR":
        dispatched = {normalize_interval(i.get("intervalStart")): D((i.get("reserve") or {}).get("dispatchedKwh", 0))
                      for i in result.get("intervals") or []}
    else:
        dispatched = {None: D((result.get("reserve") or {}).get("dispatchedKwh", 0))}
    _check_totals_invariants(diff, "Admin:", resolution, processed, trades, dispatched, tol_k)

    w = "Admin: Tagesergebnis"
    diff.eq(w, "status", "SETTLED", result.get("status"))
    diff.eq(w, "resolution", resolution, result.get("resolution"))
    S = sum((p.quantity for p in processed if p.type == "OFFER"), ZERO)
    Dm = sum((p.quantity for p in processed if p.type == "DEMAND"), ZERO)
    diff.num(w, "totalOfferedKwh (S)", S, result.get("totalOfferedKwh"), tol_k)
    diff.num(w, "totalDemandKwh (D)", Dm, result.get("totalDemandKwh"), tol_k)
    diff.num(w, "totalPurchasedKwh = Summe PLATFORM_BUYS (Invariante 6)", _qty(trades, "PLATFORM_BUYS"),
             result.get("totalPurchasedKwh"), tol_k)
    diff.num(w, "totalSoldKwh = Summe PLATFORM_SELLS (Invariante 6)", _qty(trades, "PLATFORM_SELLS"),
             result.get("totalSoldKwh"), tol_k)
    purchase, sales = _amount(trades, "PLATFORM_BUYS"), _amount(trades, "PLATFORM_SELLS")
    diff.num(w, "purchaseCostEur = Summe Ankauf", purchase, result.get("purchaseCostEur"), tol_e)
    diff.num(w, "salesRevenueEur = Summe Verkauf", sales, result.get("salesRevenueEur"), tol_e)
    res = result.get("reserve") or {}
    cost = D(res.get("costEur"))
    if cost is not None and D(result.get("salesRevenueEur")) is not None and D(result.get("purchaseCostEur")) is not None:
        diff.num(w, "marginEur = salesRevenueEur - purchaseCostEur - reserve.costEur (Invariante 8)",
                 D(result["salesRevenueEur"]) - D(result["purchaseCostEur"]) - cost, result.get("marginEur"), tol_e)
    if resolution == "QUARTER_HOUR":  # Tagessumme der Reserve = Summe je Intervall von max(D - S, 0)
        per_iv: dict[Any, list[Pos]] = defaultdict(list)
        for x in processed:
            per_iv[x.interval_start].append(x)
        req_day = sum((max(sum((x.quantity for x in g if x.type == "DEMAND"), ZERO)
                           - sum((x.quantity for x in g if x.type == "OFFER"), ZERO), ZERO) for g in per_iv.values()), ZERO)
        _check_reserve_invariants(diff, f"{w} reserve", res, ZERO, req_day, _qty(trades, "PLATFORM_SELLS", True), tol_k,
                                  day_of_intervals=True)
    else:
        _check_reserve_invariants(diff, f"{w} reserve", res, S, Dm, _qty(trades, "PLATFORM_SELLS", True), tol_k)
    if res.get("status") in ("NOT_NEEDED", "COVERED"):  # Invariante 5: im Standardfall jeder Bedarf voll gedeckt
        short = [pid for pid, a in plat_alloc.items() if by_id.get(pid) and by_id[pid].type == "DEMAND"
                 and D(a.get("allocatedKwh")) != by_id[pid].quantity]
        diff.true(w, not short, f"{len(short)} Bedarfe nicht voll gedeckt, obwohl reserve.status "
                                f"{res.get('status')} (Invariante 5)")
    rp = result.get("prices") or {}
    for f, direction, qty, amt in (
            ("buyPriceEurPerKwh", "PLATFORM_BUYS", _qty(trades, "PLATFORM_BUYS"), purchase),
            ("sellPriceEurPerKwh", "PLATFORM_SELLS", _qty(trades, "PLATFORM_SELLS"), sales)):
        if qty == 0:
            diff.true(w, rp.get(f) is None, f"prices.{f} = {rp.get(f)}, ohne {direction} muss er null sein")
        else:
            diff.true(w, rp.get(f) is not None, f"prices.{f} ist null, obwohl es {direction}-Geschäfte gibt")
            diff.num(w, f"prices.{f} (Durchschnitt nach Menge, Vorschlag)", avg_price(amt, qty), rp.get(f), ref=True)
    disp_total = D(res.get("dispatchedKwh")) or ZERO
    if disp_total == 0:
        diff.true(w, rp.get("reservePriceEurPerKwh") is None,
                  f"prices.reservePriceEurPerKwh = {rp.get('reservePriceEurPerKwh')}, ohne Reserve muss er null sein")
    elif cost is not None:
        diff.num(w, "prices.reservePriceEurPerKwh (costEur / dispatchedKwh, Vorschlag)", avg_price(cost, disp_total),
                 rp.get("reservePriceEurPerKwh"), ref=True)
    if resolution == "QUARTER_HOUR":
        for i in result.get("intervals") or []:
            k = normalize_interval(i.get("intervalStart"))
            ps = [p for p in processed if p.interval_start == k]
            iS = sum((p.quantity for p in ps if p.type == "OFFER"), ZERO)
            iD = sum((p.quantity for p in ps if p.type == "DEMAND"), ZERO)
            itr = [t for t in trades if normalize_interval(t.get("intervalStart")) == k]
            wi = f"Admin: Intervall {k}"
            diff.num(wi, "totalOfferedKwh", iS, i.get("totalOfferedKwh"), tol_k)
            diff.num(wi, "totalDemandKwh", iD, i.get("totalDemandKwh"), tol_k)
            diff.num(wi, "totalPurchasedKwh", _qty(itr, "PLATFORM_BUYS"), i.get("totalPurchasedKwh"), tol_k)
            diff.num(wi, "totalSoldKwh", _qty(itr, "PLATFORM_SELLS"), i.get("totalSoldKwh"), tol_k)
            _check_reserve_invariants(diff, f"{wi} reserve", i.get("reserve") or {}, iS, iD,
                                      _qty(itr, "PLATFORM_SELLS", True), tol_k)
    elif result.get("intervals"):
        diff.fail(w, "intervals ist bei DAY gesetzt")
    if not result.get("ruleVersion"):
        sim.findings.info("verify", f"{d}: Tagesergebnis ohne ruleVersion (optional, empfohlen für Nachvollziehbarkeit)")

    # --- Vergleich mit dem Referenzbeispiel Gruppe 1 (nicht verbindlich)
    if indication is None:
        sim.findings.info("verify", f"{d}: Markt ohne Preisindikation – Vergleich mit dem Referenzbeispiel übersprungen")
        return {}
    ref = settle(processed, prices, resolution, dispatched)
    for ra in ref.allocations:
        pa = plat_alloc.get(ra.position.id) or {}
        wa = f"Admin: Zuteilung {ra.position.id}"
        diff.num(wa, "allocatedKwh", ra.allocated, pa.get("allocatedKwh"), tol_k, ref=True)
        diff.eq(wa, "reason", ra.reason, pa.get("reason"), ref=True)
    plat_tr = {(t.get("positionId"), t.get("direction"), bool(t.get("fromReserve"))): t for t in trades}
    diff.true("Admin: Trades", len(plat_tr) == len(trades), "mehrere Trades je Position, Richtung und Herkunft", ref=True)
    ref_tr = {(t.position.id, t.direction, t.from_reserve): t for t in ref.trades}
    for k, rt in ref_tr.items():
        pt = plat_tr.get(k)
        wt = f"Admin: Trade {k[0]} {k[1]}{' (Reserve)' if k[2] else ''}"
        if pt is None:
            diff.fail(wt, f"fehlt (erwartet {rt.quantity} kWh)", ref=True)
            continue
        diff.num(wt, "quantityKwh", rt.quantity, pt.get("quantityKwh"), tol_k, ref=True)
        diff.num(wt, "priceEurPerKwh", rt.price, pt.get("priceEurPerKwh"), ref=True)
        diff.num(wt, "amountEur", rt.amount, pt.get("amountEur"), tol_e, ref=True)
    for k in set(plat_tr) - set(ref_tr):
        diff.fail(f"Admin: Trade {k[0]} {k[1]}", f"unerwartet ({plat_tr[k].get('quantityKwh')} kWh)", ref=True)
    diff.num(w, "reserve.costEur (Reservepreis × Menge)", ref.reserve.cost, res.get("costEur"), tol_e, ref=True)
    if ref.reserve.required > 0:
        diff.num(w, "reserve.priceEurPerKwh", ref.reserve.price, res.get("priceEurPerKwh"), ref=True)
    return _ref_summary(ref)


def _check_reserve_invariants(diff: Diff, w: str, res: dict, S: Decimal, Dm: Decimal, reserve_sold: Decimal, tol_k,
                              day_of_intervals: bool = False) -> None:
    required, disp = D(res.get("requiredKwh")), D(res.get("dispatchedKwh"))
    label = "Summe der Intervalle" if day_of_intervals else "max(D - S, 0)"
    diff.num(w, f"requiredKwh = {label} (Invariante 4)", max(Dm - S, ZERO), required, tol_k)
    if required is None or disp is None:
        return
    diff.true(w, ZERO <= disp <= required + tol_k, f"dispatchedKwh {disp} außerhalb 0…{required}")
    if day_of_intervals:  # Tagesstatus aus den Intervallen, nicht aus den Summen ableitbar
        diff.num(w, "dispatchedKwh = Summe der Trades mit fromReserve (Vorschlag)", disp, reserve_sold, tol_k, ref=True)
        return
    if required == 0:
        expected = "NOT_NEEDED"
    elif disp >= required:
        expected = "COVERED"
    elif disp == 0:
        expected = "UNAVAILABLE"
    else:
        expected = "PARTIAL"
    diff.eq(w, "status", expected, res.get("status"))
    diff.num(w, "dispatchedKwh = Summe der Trades mit fromReserve (Vorschlag)", disp, reserve_sold, tol_k, ref=True)
    if disp == 0:
        diff.num(w, "costEur ohne Reserveeinsatz", ZERO, res.get("costEur"))
