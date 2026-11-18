"""Szenario-Katalog: gezielte Randfälle mit festen Positionen und erwarteten Ergebnissen.

Jedes Intervall-Szenario bekommt im 15-Minuten-Markt eine eigene Viertelstunde (ab 10:00 Uhr Wiener Zeit).
Weil jede Viertelstunde für sich abgerechnet wird, beeinflussen sich die Szenarien nicht. Im Tagesmarkt braucht
jedes Szenario einen eigenen Liefertag.

Die Erwartungen sind von Hand aus dem Datenvertrag abgeleitet (unabhängig von der Referenzimplementierung).
Zusätzlich wird jedes Szenario mit der Referenzlogik nachgerechnet.
"""
from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from egsim.config import CommunityConfig
from egsim.settlement import ZERO, D, Pos, Prices, round_eur, settle
from egsim.timeutil import day_bounds_utc, format_utc, normalize_interval, now_utc, parse_ts, quarter_hours, \
    resolve_delivery_date

if TYPE_CHECKING:
    from egsim.client import ApiClient
    from egsim.simulator import SimEG, Simulator

log = logging.getLogger("egsim.scenarios")
ANY_DEMAND = ("FULLY_ALLOCATED", "RESERVE_SHORTAGE")


@dataclass
class SP:
    """Position eines Szenarios. price: Zahl, 'buy' oder 'buy+0.00001' (relativ zum Ankaufspreis des Markts)."""
    slot: str
    type: str
    qty: str
    price: str | None = None
    update_qty: str | None = None
    cancel: bool = False
    burst: int = 1  # > 1: dieselbe Position gleichzeitig so oft einreichen (genau eine darf angenommen werden)


@dataclass
class Exp:
    slot: str
    allocated: str | None = None
    reasons: tuple[str, ...] = ()
    status: str = "PROCESSED"
    trades: int | None = None
    amounts: dict[str, Callable[[Prices], Decimal]] = field(default_factory=dict)
    reserve_trade_if_full: bool = False
    rejection_reason: str | None = None
    # erwartete Trade-Mengen: BUYS, SELLS (aus Angeboten), SELLS_RESERVE (aus der Reserve)
    trade_qty: dict[str, str] = field(default_factory=dict)
    # trade_qty und amounts nur prüfen, wenn der Bedarf voll zugeteilt wurde (sonst hat das Kraftwerk nicht voll geliefert)
    full_only: bool = False


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    positions: list[SP] = field(default_factory=list)
    expect: list[Exp] = field(default_factory=list)
    kind: str = "interval"  # interval | market
    admin: bool = False
    suspend: str | None = None  # Slot, dessen EG vor Marktschluss gesperrt wird (eigene Wegwerf-EG)
    parallel: bool = False      # alle Positionen gleichzeitig einreichen (Reihenfolge von createdAt ist dann offen)
    total_trades: int | None = None  # Gesamtzahl der Trades im Intervall (nur mit Admin-Key prüfbar)
    # Erwartungen an das Tagesergebnis /result (nur mit Admin-Key): Feldpfad → Menge, z. B. "reserve.requiredKwh"
    result_qty: dict[str, str] = field(default_factory=dict)
    # Beträge im Tagesmarkt aus den Preisen des Markts (nur wenn das Kraftwerk die Reserve voll geliefert hat)
    money: Callable[[Prices], dict[str, Decimal]] | None = None

    @property
    def slots(self) -> list[str]:
        return sorted({p.slot for p in self.positions if p.slot != self.suspend})

    def info(self) -> dict:
        return {"id": self.id, "title": self.title, "description": self.description, "kind": self.kind,
                "admin": self.admin, "egs": len(self.slots) + (1 if self.suspend else 0), "parallel": self.parallel}


CATALOG: list[Scenario] = [
    Scenario("SZ-01", "Angebot = Bedarf",
             "Angebot und Bedarf gleich groß: beide voll zugeteilt, keine Reserve.",
             [SP("A", "OFFER", "50"), SP("B", "DEMAND", "50")],
             [Exp("A", "50", ("FULLY_ALLOCATED",), trades=1), Exp("B", "50", ("FULLY_ALLOCATED",), trades=1)]),
    Scenario("SZ-02", "Preisvorstellung = Ankaufspreis",
             "Preisvorstellung genau gleich dem Ankaufspreis ist noch handelbar (≤).",
             [SP("A", "OFFER", "40", price="buy"), SP("B", "DEMAND", "40")],
             [Exp("A", "40", ("FULLY_ALLOCATED",), trades=1), Exp("B", "40", ("FULLY_ALLOCATED",))]),
    Scenario("SZ-03", "Preisvorstellung knapp über Ankaufspreis",
             "0,00001 EUR über dem Ankaufspreis: PRICE_NOT_MET, der Bedarf kommt aus der Reserve.",
             [SP("A", "OFFER", "40", price="buy+0.00001"), SP("B", "DEMAND", "40")],
             [Exp("A", "0", ("PRICE_NOT_MET",), trades=0), Exp("B", reasons=ANY_DEMAND, reserve_trade_if_full=True)]),
    Scenario("SZ-04", "Rundungsrest nach Einreichzeit",
             "Drei gleiche Angebote à 5 kWh, Bedarf 10 kWh: je 3,333 kWh, der Rest von 0,001 kWh geht an das zuerst "
             "eingereichte Angebot.",
             [SP("A", "OFFER", "5"), SP("B", "OFFER", "5"), SP("C", "OFFER", "5"), SP("D", "DEMAND", "10")],
             [Exp("A", "3.334", ("PARTIALLY_ALLOCATED",), trades=1), Exp("B", "3.333", ("PARTIALLY_ALLOCATED",)),
              Exp("C", "3.333", ("PARTIALLY_ALLOCATED",)), Exp("D", "10", ("FULLY_ALLOCATED",))]),
    Scenario("SZ-05", "Teilzuteilung pro rata",
             "Angebote 100 und 60 kWh, Bedarf 120 kWh: 75 und 45 kWh (Beispiel aus dem Vertrag).",
             [SP("A", "OFFER", "100"), SP("B", "OFFER", "60"), SP("C", "DEMAND", "120")],
             [Exp("A", "75", ("PARTIALLY_ALLOCATED",)), Exp("B", "45", ("PARTIALLY_ALLOCATED",)),
              Exp("C", "120", ("FULLY_ALLOCATED",))]),
    Scenario("SZ-06", "Nur Bedarf",
             "Kein Angebot: der gesamte Bedarf kommt aus der Reserve (oder RESERVE_SHORTAGE, wenn das Kraftwerk "
             "nicht liefert).",
             [SP("A", "DEMAND", "12.5")],
             [Exp("A", reasons=ANY_DEMAND, reserve_trade_if_full=True)]),
    Scenario("SZ-07", "Nur Angebot",
             "Kein Bedarf: das Angebot wird nicht gebraucht (NOT_NEEDED, 0 kWh, kein Trade).",
             [SP("A", "OFFER", "20")],
             [Exp("A", "0", ("NOT_NEEDED",), trades=0)]),
    Scenario("SZ-08", "Kleinstmenge 0,001 kWh",
             "Kleinste erlaubte Menge: Trade über 0,001 kWh, Betrag gerundet (meist 0,00 €).",
             [SP("A", "OFFER", "0.001"), SP("B", "DEMAND", "0.001")],
             [Exp("A", "0.001", ("FULLY_ALLOCATED",), trades=1,
                  amounts={"PLATFORM_BUYS": lambda p: round_eur(Decimal("0.001") * p.buy)}),
              Exp("B", "0.001", ("FULLY_ALLOCATED",), trades=1,
                  amounts={"PLATFORM_SELLS": lambda p: round_eur(Decimal("0.001") * p.sell)})]),
    Scenario("SZ-09", "Maximalmenge 1.000.000 kWh",
             "Größte erlaubte Menge: prüft große Zahlen und Beträge ohne Rundungsfehler.",
             [SP("A", "OFFER", "1000000"), SP("B", "DEMAND", "1000000")],
             [Exp("A", "1000000", ("FULLY_ALLOCATED",),
                  amounts={"PLATFORM_BUYS": lambda p: round_eur(Decimal("1000000") * p.buy)}),
              Exp("B", "1000000", ("FULLY_ALLOCATED",),
                  amounts={"PLATFORM_SELLS": lambda p: round_eur(Decimal("1000000") * p.sell)})]),
    Scenario("SZ-10", "Kaufmännisches Runden der Beträge",
             "0,125 kWh: Beträge werden kaufmännisch auf Cent gerundet (z. B. 0,0175 € → 0,02 €).",
             [SP("A", "OFFER", "0.125"), SP("B", "DEMAND", "0.125")],
             [Exp("A", "0.125", ("FULLY_ALLOCATED",),
                  amounts={"PLATFORM_BUYS": lambda p: round_eur(Decimal("0.125") * p.buy)}),
              Exp("B", "0.125", ("FULLY_ALLOCATED",),
                  amounts={"PLATFORM_SELLS": lambda p: round_eur(Decimal("0.125") * p.sell)})]),
    Scenario("SZ-11", "Ändern und Stornieren vor Marktschluss",
             "Angebot 30 → 20 kWh geändert, ein Bedarf storniert: zählt nur der letzte Stand, stornierte Positionen "
             "bekommen keine Zuteilung.",
             [SP("A", "OFFER", "30", update_qty="20"), SP("B", "DEMAND", "20"), SP("C", "DEMAND", "10", cancel=True)],
             [Exp("A", "20", ("FULLY_ALLOCATED",)), Exp("B", "20", ("FULLY_ALLOCATED",)),
              Exp("C", status="CANCELLED", trades=0)]),
    Scenario("SZ-12", "Gesperrte EG bei Marktschluss",
             "Eine EG wird nach dem Einreichen gesperrt: ihre Position wird REJECTED und nicht gehandelt.",
             [SP("X", "OFFER", "10"), SP("A", "DEMAND", "10")],
             [Exp("X", status="REJECTED", trades=0, rejection_reason="ENERGY_COMMUNITY_NOT_ACTIVE"),
              Exp("A", reasons=ANY_DEMAND, reserve_trade_if_full=True)],
             admin=True, suspend="X"),
    Scenario("SZ-13", "Unterangebot",
             "Handelbares Angebot 30 kWh, Bedarf 50 kWh: das Angebot wird ganz gekauft, die Fehlmenge von 20 kWh kommt "
             "aus der Reserve (Pflichtszenario „Unterangebot“).",
             [SP("A", "OFFER", "30"), SP("B", "DEMAND", "50")],
             [Exp("A", "30", ("FULLY_ALLOCATED",), trades=1),
              Exp("B", reasons=ANY_DEMAND, reserve_trade_if_full=True, full_only=True,
                  trade_qty={"SELLS": "30", "SELLS_RESERVE": "20"})],
             result_qty={"totalOfferedKwh": "30", "tradableOfferedKwh": "30", "totalDemandKwh": "50",
                         "totalPurchasedKwh": "30", "reserve.requiredKwh": "20"}),
    Scenario("SZ-14", "Beispielablauf aus dem Vertrag",
             "Angebote 100 kWh (Preisvorstellung 0,02 unter dem Ankaufspreis) und 50 kWh (0,02 darüber, wird nicht "
             "gehandelt), Bedarf 130 kWh: Reserve 30 kWh. Mit den Preisen des Vertrags (0,10 / 0,14 / 0,20) ergibt das "
             "Einkauf 10,00, Verkauf 18,20, Reserve 6,00 und eine Marge von 2,20 EUR.",
             [SP("A", "OFFER", "100", price="buy-0.02"), SP("B", "OFFER", "50", price="buy+0.02"),
              SP("C", "DEMAND", "130")],
             [Exp("A", "100", ("FULLY_ALLOCATED",), trades=1,
                  amounts={"PLATFORM_BUYS": lambda p: round_eur(Decimal("100") * p.buy)}),
              Exp("B", "0", ("PRICE_NOT_MET",), trades=0),
              Exp("C", reasons=ANY_DEMAND, reserve_trade_if_full=True, full_only=True,
                  trade_qty={"SELLS": "100", "SELLS_RESERVE": "30"},
                  amounts={"PLATFORM_SELLS": lambda p: round_eur(Decimal("100") * p.sell) + round_eur(Decimal("30") * p.sell)})],
             result_qty={"totalOfferedKwh": "150", "tradableOfferedKwh": "100", "totalDemandKwh": "130",
                         "totalPurchasedKwh": "100", "reserve.requiredKwh": "30"},
             money=lambda p: {
                 "purchaseCostEur": round_eur(Decimal("100") * p.buy),
                 "salesRevenueEur": round_eur(Decimal("100") * p.sell) + round_eur(Decimal("30") * p.sell),
                 "reserve.costEur": round_eur(Decimal("30") * p.reserve),
                 "marginEur": round_eur(Decimal("100") * p.sell) + round_eur(Decimal("30") * p.sell)
                 - round_eur(Decimal("100") * p.buy) - round_eur(Decimal("30") * p.reserve)}),
    Scenario("SZ-15", "Reserve wird auf mehrere Bedarfe verteilt",
             "Handelbares Angebot 30 kWh, nicht handelbares 20 kWh (Preisvorstellung über dem Ankaufspreis, erhöht die "
             "Reserve), Bedarfe 30 und 20 kWh: Reserve 20 kWh pro rata, also 12 und 8 kWh. Jeder Bedarf bekommt einen "
             "Trade aus Angeboten (18 und 12 kWh) und einen aus der Reserve.",
             [SP("A", "OFFER", "30"), SP("B", "OFFER", "20", price="buy+0.01"), SP("C", "DEMAND", "30"),
              SP("D", "DEMAND", "20")],
             [Exp("A", "30", ("FULLY_ALLOCATED",), trades=1), Exp("B", "0", ("PRICE_NOT_MET",), trades=0),
              Exp("C", reasons=ANY_DEMAND, full_only=True, trades=2, trade_qty={"SELLS": "18", "SELLS_RESERVE": "12"}),
              Exp("D", reasons=ANY_DEMAND, full_only=True, trades=2, trade_qty={"SELLS": "12", "SELLS_RESERVE": "8"})],
             result_qty={"totalOfferedKwh": "50", "tradableOfferedKwh": "30", "totalDemandKwh": "50",
                         "totalPurchasedKwh": "30", "reserve.requiredKwh": "20"}),
    Scenario("SZ-16", "Doppelter Request (gleichzeitig)",
             "Dieselbe EG reicht dasselbe Angebot gleichzeitig fünfmal ein: genau eine Position wird angenommen, die "
             "übrigen vier bekommen 409 POSITION_ALREADY_EXISTS. Danach gibt es nur eine Zuteilung und einen Trade.",
             [SP("A", "OFFER", "20", burst=5), SP("B", "DEMAND", "20")],
             [Exp("A", "20", ("FULLY_ALLOCATED",), trades=1), Exp("B", "20", ("FULLY_ALLOCATED",), trades=1)],
             total_trades=2),
    Scenario("SZ-17", "Parallele Einreichung ohne Überbuchung",
             "Zwei Angebote à 30 kWh und zwei Bedarfe (25 und 15 kWh) werden gleichzeitig von vier EGs eingereicht. "
             "Gekauft werden nur 40 kWh (je 20 kWh pro Angebot), die Summen müssen aufgehen.",
             [SP("A", "OFFER", "30"), SP("B", "OFFER", "30"), SP("C", "DEMAND", "25"), SP("D", "DEMAND", "15")],
             [Exp("A", "20", ("PARTIALLY_ALLOCATED",), trades=1), Exp("B", "20", ("PARTIALLY_ALLOCATED",), trades=1),
              Exp("C", "25", ("FULLY_ALLOCATED",), trades=1), Exp("D", "15", ("FULLY_ALLOCATED",), trades=1)],
             parallel=True, total_trades=4,
             result_qty={"totalOfferedKwh": "60", "tradableOfferedKwh": "60", "totalDemandKwh": "40",
                         "totalPurchasedKwh": "40", "totalSoldKwh": "40", "reserve.requiredKwh": "0"}),
    Scenario("SZ-20", "Alle Viertelstunden des Liefertags",
             "Eine EG reicht in jeder Viertelstunde eine Position ein (96, an Umstellungstagen 92/100) und storniert "
             "sie wieder. Braucht einen 15-Minuten-Markt.", kind="market"),
    Scenario("SZ-21", "Lasttest",
             "Alle EGs reichen gleichzeitig 16 Positionen ab 15:00 Uhr ein und stornieren sie wieder. Prüft auf "
             "Serverfehler und misst die Latenz. Braucht einen 15-Minuten-Markt.", kind="market"),
]
BY_ID = {s.id: s for s in CATALOG}


def catalog_info() -> list[dict]:
    return [s.info() for s in CATALOG]


# ======================================================================================================================
@dataclass
class _Placed:
    sc: Scenario
    delivery: str
    interval: str | None
    prices: Prices
    pos: dict[str, dict] = field(default_factory=dict)       # slot -> {"eg", "id", "body"}
    messages: list[str] = field(default_factory=list)
    invalid: bool = False


def _price(spec: str | None, prices: Prices) -> Decimal | None:
    if spec is None:
        return None
    if spec.startswith("buy"):
        rest = spec[3:]  # "", "+0.02" oder "-0.02"
        return (prices.buy or ZERO) + (D(rest) if rest else ZERO)
    return D(spec)


def _slot_egs(sim: "Simulator", needed: int) -> list["SimEG"]:
    """Bereite EGs in Konfigurationsreihenfolge; mit Admin-Key werden fehlende Szenario-EGs angelegt."""
    from egsim.simulator import SimEG
    ready = [e for e in sim.egs if e.ready]
    n = 0
    while len(ready) < needed and sim._can_provision():
        n += 1
        name = f"EGSim Szenario {chr(64 + len(ready) + 1)}"
        if any(c.name == name for c in sim.cfg.communities):
            name = f"{name} ({uuid.uuid4().hex[:4]})"
        c = CommunityConfig(uid=uuid.uuid4().hex[:12], name=name, description="Von EGSim für Szenarien angelegt")
        sim.cfg.communities.append(c)
        eg = SimEG(key=name, cfg=c)
        sim.egs.append(eg)
        if sim.provision(eg) and sim._try_connect(eg, quiet=False):
            ready.append(eg)
        else:
            break
    return ready


def _throwaway_eg(sim: "Simulator", label: str) -> "SimEG | None":
    from egsim.simulator import SimEG
    name = f"EGSim {label} {uuid.uuid4().hex[:4]}"
    c = CommunityConfig(uid=uuid.uuid4().hex[:12], name=name, description="Von EGSim für ein Szenario angelegt")
    eg = SimEG(key=name, cfg=c)
    if sim.provision(eg) and sim._try_connect(eg, quiet=False):
        sim.provisioned.remove(c.uid)  # nicht in die Konfiguration übernehmen
        return eg
    return None


def run_scenarios(sim: "Simulator") -> None:
    selected = [BY_ID[i] for i in sim.cfg.scenarios if i in BY_ID]
    if not selected:
        sim.findings.error("scenarios", "Keine Szenarien ausgewählt")
        return
    unknown = [i for i in sim.cfg.scenarios if i not in BY_ID]
    if unknown:
        sim.findings.warning("scenarios", f"Unbekannte Szenarien ignoriert: {', '.join(unknown)}")
    queue = [s for s in selected if s.kind == "interval"]
    market_level = [s for s in selected if s.kind == "market"]
    for s in list(queue):
        if s.admin and not sim.admin:
            _result(sim, s, None, None, "SKIP", ["braucht den Admin-Key der Test-Plattform"])
            queue.remove(s)
    needed = max([len(s.slots) for s in queue] + [1])
    slots = _slot_egs(sim, needed)
    if not slots:
        for s in queue + market_level:
            _result(sim, s, None, None, "SKIP", ["keine EG angemeldet"])
        return

    dates = [resolve_delivery_date(x).isoformat() for x in sim.cfg.scenario_dates] if sim.cfg.scenario_dates else sim._dates()
    for d in dates:
        sim._check_stop()
        if not queue and not market_level:
            break
        reader = slots[0].client
        r = reader.get_market(d)
        if r.status != 200:
            sim.findings.warning("scenarios", f"{d}: Markt nicht abrufbar ({r.status} {r.code or ''})", r.short())
            continue
        m = r.body
        closes = parse_ts(m["closesAt"]) if m.get("closesAt") else None
        if m.get("status") != "OPEN" or (closes and now_utc() >= closes):
            sim.findings.warning("scenarios", f"{d}: Markt ist nicht offen ({m.get('status')}) – Liefertag übersprungen")
            continue
        from egsim.simulator import MarketRun
        run = MarketRun(d, market=m)
        sim.markets.append(run)
        prices = Prices.from_api(m.get("prices") or {})
        res = m.get("resolution")
        if res == "QUARTER_HOUR":
            start = day_bounds_utc(resolve_delivery_date(d))[0] + timedelta(hours=10)
            take = queue[:20]  # 10:00–15:00 Uhr; der Lasttest nutzt 15:00–19:00
            placed = [_Placed(s, d, format_utc(start + timedelta(minutes=15 * i)), prices) for i, s in enumerate(take)]
        else:
            take = queue[:1]
            placed = [_Placed(s, d, None, prices) for s in take]
        queue = queue[len(take):]

        for p in placed:
            sim._check_stop()
            _place(sim, p, slots)
        if res == "QUARTER_HOUR" and market_level:
            used = {pos["eg"].id for p in placed for pos in p.pos.values()}
            for s in market_level:
                _market_scenario(sim, s, d, slots, [e for e in sim.egs if e.ready and e.id not in used])
            market_level = []
        for p in placed:
            if p.sc.suspend and p.pos.get(p.sc.suspend):
                eg = p.pos[p.sc.suspend]["eg"]
                st = sim.admin.set_status(eg.id, "SUSPENDED")
                if st.status != 200:
                    p.messages.append(f"EG sperren fehlgeschlagen: {st.status} {st.code or ''}")
                    p.invalid = True
        if any(not p.invalid for p in placed):
            sim._finish_market(run, reader)
        for p in placed:
            _evaluate(sim, p, run.final_status)

    for s in queue:
        _result(sim, s, None, None, "SKIP", ["kein offener Markt mehr frei – weitere Liefertage oder einen "
                                             "15-Minuten-Markt eintragen"])
    for s in market_level:
        _result(sim, s, None, None, "SKIP", ["braucht einen offenen 15-Minuten-Markt"])


def _place(sim: "Simulator", p: _Placed, slots: list["SimEG"]) -> None:
    sc = p.sc
    mapping = {slot: slots[i] for i, slot in enumerate(sc.slots)}
    if sc.suspend:
        eg = _throwaway_eg(sim, "gesperrt")
        if not eg:
            p.invalid = True
            p.messages.append("Wegwerf-EG zum Sperren konnte nicht angelegt werden")
            return
        mapping[sc.suspend] = eg
    if sim.admin:
        q = {"deliveryDate": p.delivery}
        if p.interval:
            q["intervalStart"] = p.interval
        lst = sim.admin.list_positions(**q)
        foreign = [x for x in (lst.body or []) if isinstance(x, dict) and x.get("status") != "CANCELLED"] \
            if lst.status == 200 else []
        if foreign:
            p.messages.append(f"{len(foreign)} andere Positionen im Intervall – Erwartungen gelten nur für einen "
                              "leeren Markt")
            p.invalid = True
    log.info("%s %s: %s %s", sc.id, sc.title, p.delivery, p.interval or "")
    def body_of(sp: SP) -> dict:
        body: dict = {"deliveryDate": p.delivery, "type": sp.type, "quantityKwh": D(sp.qty)}
        if p.interval:
            body["intervalStart"] = p.interval
        price = _price(sp.price, p.prices)
        if price is not None:
            body["priceExpectationEurPerKwh"] = price
        return body

    def submit(sp: SP) -> list:
        eg, body = mapping[sp.slot], body_of(sp)
        if sp.burst > 1:  # gleichzeitig dieselbe Position mehrfach
            with ThreadPoolExecutor(sp.burst) as ex:
                return list(ex.map(lambda _: eg.client.submit_position(body), range(sp.burst)))
        return [eg.client.submit_position(body)]

    if sc.parallel:  # alle Positionen gleichzeitig, die Reihenfolge von createdAt ist dem Zufall überlassen
        with ThreadPoolExecutor(len(sc.positions)) as ex:
            answers = list(ex.map(submit, sc.positions))
    else:
        answers = []
        for sp in sc.positions:
            answers.append(submit(sp))
            time.sleep(0.03)  # eindeutige Reihenfolge von createdAt

    for sp, rs in zip(sc.positions, answers):
        eg, body = mapping[sp.slot], body_of(sp)
        created = [r for r in rs if r.status == 201]
        if sp.burst > 1:
            wrong = [r for r in rs if r.status != 201 and not (r.status == 409 and r.code == "POSITION_ALREADY_EXISTS")]
            if len(created) != 1:
                p.messages.append(f"{sp.slot}: {len(created)} von {sp.burst} gleichzeitigen Requests angenommen, "
                                  "erwartet genau 1 (Idempotenz verletzt)")
            if wrong:
                p.messages.append(f"{sp.slot}: {len(wrong)} doppelte Requests nicht mit 409 POSITION_ALREADY_EXISTS "
                                  f"abgelehnt ({', '.join(f'{r.status} {r.code or r.error or chr(45)}' for r in wrong[:3])})")
            q = {"deliveryDate": p.delivery, "status": "OPEN"}
            if p.interval:
                q["intervalStart"] = p.interval
            lst = eg.client.list_positions(**q)
            if lst.status == 200 and isinstance(lst.body, list) and len(lst.body) != 1:
                p.messages.append(f"{sp.slot}: {len(lst.body)} offene Positionen für denselben Slot, erwartet genau 1")
        if not created:
            first = rs[0]
            p.invalid = True
            p.messages.append(f"{sp.slot} ({eg.key}): Einreichen {first.status} {first.code or ''}"
                              + (" – Intervall schon belegt, anderen Liefertag nutzen"
                                 if first.code == "POSITION_ALREADY_EXISTS" else ""))
            continue
        p.pos[sp.slot] = {"eg": eg, "id": created[0].body.get("id"), "body": body}
    for sp in sc.positions:
        pos = p.pos.get(sp.slot)
        if not pos:
            continue
        cl = pos["eg"].client
        if sp.update_qty:
            body = {"quantityKwh": D(sp.update_qty)}
            r = cl.update_position(pos["id"], body)
            if r.status != 200:
                p.messages.append(f"{sp.slot}: Ändern {r.status} {r.code or ''}")
                p.invalid = True
        if sp.cancel:
            r = cl.cancel_position(pos["id"])
            if r.status != 200:
                p.messages.append(f"{sp.slot}: Stornieren {r.status} {r.code or ''}")
                p.invalid = True


def _evaluate(sim: "Simulator", p: _Placed, market_status: str | None) -> None:
    sc = p.sc
    if not p.pos:
        _result(sim, sc, p.delivery, p.interval, "FAIL", p.messages or ["keine Position eingereicht"])
        return
    if market_status != "SETTLED":
        _result(sim, sc, p.delivery, p.interval, "PENDING",
                p.messages + [f"Markt ist {market_status} – Ergebnis gibt es erst nach dem Settlement. Wartezeit "
                              "erhöhen oder mit Admin-Key „Markt schließen“ aktivieren."])
        return
    msgs = list(p.messages)
    q = {"intervalStart": p.interval} if p.interval else {}

    def fetch(client: "ApiClient", what: str) -> list[dict]:
        r = client.allocations(p.delivery, **q) if what == "alloc" else client.trades(p.delivery, **q)
        return r.body if r.status == 200 and isinstance(r.body, list) else []

    allocs: dict[str, dict] = {}
    trades: dict[str, list[dict]] = {}
    positions: dict[str, dict] = {}
    for slot, pos in p.pos.items():
        cl = sim.admin or pos["eg"].client
        g = cl.get_position(pos["id"])
        positions[slot] = g.body if g.status == 200 and isinstance(g.body, dict) else {}
        if slot == sc.suspend and not sim.admin:
            continue
        for a in fetch(cl, "alloc"):
            if a.get("positionId") == pos["id"]:
                allocs[slot] = a
        trades[slot] = [t for t in fetch(cl, "trades") if t.get("positionId") == pos["id"]]

    rows = []
    for e in sc.expect:
        pos = p.pos.get(e.slot)
        if not pos:
            continue
        w = f"{e.slot} ({pos['eg'].key})"
        status = positions.get(e.slot, {}).get("status")
        a = allocs.get(e.slot)
        tr = trades.get(e.slot, [])
        rows.append({"slot": e.slot, "eg": pos["eg"].key, "type": pos["body"]["type"],
                     "quantityKwh": str(D(positions.get(e.slot, {}).get("quantityKwh") or pos["body"]["quantityKwh"])),
                     "status": status, "allocatedKwh": str(a.get("allocatedKwh")) if a else None,
                     "reason": a.get("reason") if a else None, "trades": len(tr)})
        if status != e.status:
            msgs.append(f"{w}: Status {status}, erwartet {e.status}")
        if e.status != "PROCESSED":
            if a:
                msgs.append(f"{w}: Zuteilung vorhanden, obwohl {e.status}")
        elif not a:
            msgs.append(f"{w}: keine Zuteilung")
        else:
            if e.allocated is not None and D(a.get("allocatedKwh")) != D(e.allocated):
                msgs.append(f"{w}: allocatedKwh {a.get('allocatedKwh')}, erwartet {e.allocated}")
            if e.reasons and a.get("reason") not in e.reasons:
                msgs.append(f"{w}: reason {a.get('reason')}, erwartet {' oder '.join(e.reasons)}")
            if e.reserve_trade_if_full and a.get("reason") == "FULLY_ALLOCATED" \
                    and not any(t.get("fromReserve") for t in tr):
                msgs.append(f"{w}: voll zugeteilt ohne Reserve-Trade (fromReserve)")
        if e.rejection_reason is not None:
            got_reason = positions.get(e.slot, {}).get("rejectionReason")
            if got_reason != e.rejection_reason:
                msgs.append(f"{w}: rejectionReason {got_reason!r}, erwartet {e.rejection_reason}")
        # bei full_only gelten Trade-Anzahl, -Mengen und -Beträge nur, wenn der Bedarf voll zugeteilt wurde
        detail = not e.full_only or (a is not None and a.get("reason") == "FULLY_ALLOCATED")
        if detail and e.trades is not None and len(tr) != e.trades:
            msgs.append(f"{w}: {len(tr)} Trades, erwartet {e.trades}")
        if detail:
            for key, exp_qty in e.trade_qty.items():
                direction = "PLATFORM_BUYS" if key == "BUYS" else "PLATFORM_SELLS"
                from_reserve = key.endswith("RESERVE")
                got_qty = sum((D(t.get("quantityKwh")) or ZERO for t in tr
                               if t.get("direction") == direction and bool(t.get("fromReserve")) == from_reserve), ZERO)
                if got_qty != D(exp_qty):
                    msgs.append(f"{w}: Trade-Menge {key} {got_qty} kWh, erwartet {exp_qty} kWh")
            for direction, fn in e.amounts.items():
                exp = fn(p.prices)
                got = sum((D(t.get("amountEur")) or ZERO for t in tr if t.get("direction") == direction), ZERO)
                if got != exp:
                    msgs.append(f"{w}: Betrag {direction} {got} €, erwartet {exp} €")

    if sim.admin and not p.invalid:
        _check_totals(sim, p, msgs)

    # Nachrechnung mit der Referenzlogik (gelieferte Reserve = Summe der Reserve-Trades)
    processed = [Pos.from_api(x) for x in positions.values() if x.get("status") == "PROCESSED"]
    if processed and not p.invalid:
        dispatched = sum((D(t.get("quantityKwh")) or ZERO for ts in trades.values() for t in ts if t.get("fromReserve")), ZERO)
        res = "QUARTER_HOUR" if p.interval else "DAY"
        ref = settle(processed, p.prices, res, {normalize_interval(p.interval): dispatched})
        slot_of = {pos["id"]: s for s, pos in p.pos.items()}
        for ra in ref.allocations:
            s = slot_of.get(ra.position.id)
            a = allocs.get(s)
            if a and (D(a.get("allocatedKwh")) != ra.allocated or a.get("reason") != ra.reason):
                msgs.append(f"Nachrechnung {s}: Plattform {a.get('allocatedKwh')}/{a.get('reason')}, "
                            f"Referenz {ra.allocated}/{ra.reason}")
        for rt in ref.trades:
            s = slot_of.get(rt.position.id)
            match = [t for t in trades.get(s, []) if t.get("direction") == rt.direction
                     and bool(t.get("fromReserve")) == rt.from_reserve]
            if not match:
                msgs.append(f"Nachrechnung {s}: Trade {rt.direction}{' (Reserve)' if rt.from_reserve else ''} "
                            f"über {rt.quantity} kWh fehlt")
            elif D(match[0].get("quantityKwh")) != rt.quantity or D(match[0].get("amountEur")) != rt.amount:
                msgs.append(f"Nachrechnung {s}: Trade {match[0].get('quantityKwh')} kWh / {match[0].get('amountEur')} €, "
                            f"Referenz {rt.quantity} kWh / {rt.amount} €")
    result = "FAIL" if msgs else "PASS"
    _result(sim, sc, p.delivery, p.interval, result, msgs, rows)


def _path(src: dict, key: str):
    for part in key.split("."):
        src = src.get(part) if isinstance(src, dict) else None
    return src


def _check_totals(sim: "Simulator", p: _Placed, msgs: list[str]) -> None:
    """Mit Admin-Key: Gesamtzahl der Trades im Intervall und Tagesergebnis (/result) des Szenarios prüfen."""
    sc = p.sc
    if sc.total_trades is not None:
        q = {"intervalStart": p.interval} if p.interval else {}
        r = sim.admin.trades(p.delivery, **q)
        if r.status == 200 and isinstance(r.body, list):
            if len(r.body) != sc.total_trades:
                msgs.append(f"{len(r.body)} Trades im Intervall, erwartet {sc.total_trades} (doppelte oder fehlende Handelsabschlüsse)")
        else:
            msgs.append(f"Trades (Admin): HTTP {r.status}")
    if not sc.result_qty and not sc.money:
        return
    r = sim.admin.market_result(p.delivery)
    if r.status != 200 or not isinstance(r.body, dict):
        msgs.append(f"Tagesergebnis /result: HTTP {r.status}")
        return
    src = r.body
    if p.interval:
        src = next((i for i in r.body.get("intervals") or []
                    if normalize_interval(i.get("intervalStart")) == normalize_interval(p.interval)), None)
        if src is None:
            msgs.append("Tagesergebnis: Intervall fehlt in intervals")
            return
    for key, exp in sc.result_qty.items():
        got = _path(src, key)
        if got is None or D(got) != D(exp):
            msgs.append(f"Tagesergebnis {key}: {got}, erwartet {exp}")
    required, dispatched = D(_path(src, "reserve.requiredKwh")), D(_path(src, "reserve.dispatchedKwh"))
    if dispatched is not None and required is not None and dispatched > required:
        msgs.append(f"Tagesergebnis: Reserve geliefert ({dispatched}) größer als benötigt ({required})")
    # Beträge gibt es nur auf Tagesebene (DAY); nur prüfbar, wenn das Kraftwerk die Reserve voll geliefert hat
    if sc.money and not p.interval and dispatched is not None and dispatched == required:
        for key, exp in sc.money(p.prices).items():
            got = _path(src, key)
            if got is None or D(got) != exp:
                msgs.append(f"Tagesergebnis {key}: {got}, erwartet {exp}")


def _market_scenario(sim: "Simulator", sc: Scenario, d: str, slots: list["SimEG"], spare: list["SimEG"]) -> None:
    ivs = quarter_hours(resolve_delivery_date(d))
    if sc.id == "SZ-20":
        eg = _throwaway_eg(sim, "Viertelstunden") if sim._can_provision() else (spare[0] if spare else None)
        if not eg:
            _result(sim, sc, d, None, "SKIP", ["braucht eine EG, die in keinem anderen Szenario dieses Liefertags "
                                               "steckt (oder den Admin-Key für eine Wegwerf-EG)"])
            return
        bodies = [{"deliveryDate": d, "intervalStart": format_utc(t), "type": "DEMAND", "quantityKwh": Decimal("0.001")}
                  for t in ivs]
        created, errors, _ = _bulk(sim, [(eg, b) for b in bodies])
        msgs = [f"{len(ivs)} Viertelstunden am {d}, {len(created)} angenommen"]
        msgs += errors[:10]
        ok = len(created) == len(ivs)
        if not ok and not errors:
            msgs.append("nicht alle Positionen angenommen")
        _result(sim, sc, d, None, "PASS" if ok else "FAIL", msgs if not ok else [],
                [{"info": msgs[0]}])
        return
    # SZ-21 Lasttest
    start = day_bounds_utc(resolve_delivery_date(d))[0] + timedelta(hours=15)
    load_ivs = [format_utc(start + timedelta(minutes=15 * i)) for i in range(16)]
    jobs = [(eg, {"deliveryDate": d, "intervalStart": iv, "type": "DEMAND", "quantityKwh": Decimal("1.5")})
            for eg in slots for iv in load_ivs]
    t0 = time.perf_counter()
    created, errors, lat = _bulk(sim, jobs, concurrency=max(8, sim.cfg.concurrency))
    secs = time.perf_counter() - t0
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
    info = (f"{len(jobs)} Positionen von {len(slots)} EGs in {secs:.1f} s, {len(created)} angenommen, "
            f"p95 {p95:.0f} ms" if p95 is not None else f"{len(jobs)} Positionen")
    server_errors = [e for e in errors if ": 5" in e or "Transportfehler" in e]
    _result(sim, sc, d, None, "FAIL" if server_errors else "PASS", server_errors[:10] + ([info] if server_errors else []),
            [{"info": info}])


def _bulk(sim: "Simulator", jobs: list, concurrency: int | None = None) -> tuple[list, list[str], list[float]]:
    """Positionen parallel einreichen und danach wieder stornieren."""
    created: list[tuple] = []
    errors: list[str] = []
    latencies: list[float] = []

    def submit(job):
        eg, body = job
        r = eg.client.submit_position(body)
        latencies.append(r.elapsed_ms)
        if r.status == 201:
            created.append((eg, r.body.get("id")))
        else:
            errors.append(f"{eg.key} {body.get('intervalStart')}: {r.status or 'Transportfehler'} {r.code or r.error or ''}")

    with ThreadPoolExecutor(concurrency or sim.cfg.concurrency) as ex:
        list(ex.map(submit, jobs))

    def cancel(item):
        eg, pid = item
        r = eg.client.cancel_position(pid)
        if r.status != 200:
            errors.append(f"{eg.key}: Stornieren {r.status} {r.code or ''}")

    with ThreadPoolExecutor(concurrency or sim.cfg.concurrency) as ex:
        list(ex.map(cancel, created))
    return created, errors, latencies


def _result(sim: "Simulator", sc: Scenario, d: str | None, interval: str | None, result: str,
            messages: list[str], rows: list[dict] | None = None) -> None:
    entry = {"id": sc.id, "title": sc.title, "description": sc.description, "deliveryDate": d, "intervalStart": interval,
             "result": result, "messages": messages, "positions": rows or []}
    sim.scenario_results.append(entry)
    note = "; ".join(messages[:3]) if messages else None
    sim.check_results.append({"id": sc.id, "title": f"Szenario: {sc.title}", "phase": "scenario",
                              "result": {"PENDING": "SKIP"}.get(result, result), "expected": "wie beschrieben",
                              "actualStatus": None, "actualCode": None, "note": note, "request": None})
    lvl = {"FAIL": "error", "SKIP": "info", "PENDING": "warning"}.get(result)
    if lvl:
        sim.findings.add(lvl, "scenario", f"{sc.id} {sc.title}: {result}" + (f" – {note}" if note else ""))
    log.info("%s %s: %s", sc.id, sc.title, result)
