"""Kommandozeile: egsim run | egsim mock | egsim plan"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys

from egsim import CONTRACT_VERSION, __version__
from egsim.config import ConfigError, load_config


def _setup_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose >= 2 else logging.INFO if verbose >= 1 else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if verbose < 2:
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    from egsim.report import format_summary, write_report
    from egsim.simulator import Simulator

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    if args.base_url:
        cfg.base_url = args.base_url
    if args.date:
        cfg.delivery_dates = args.date
    if args.close:
        cfg.close_market = True
    if args.no_checks:
        cfg.checks = False
    if args.seed is not None:
        cfg.seed = args.seed
    if args.report is not None:
        cfg.report_file = args.report or None
    if cfg.close_market and not cfg.has_admin:
        print("Konfigurationsfehler: Markt schließen braucht Admin-Zugang", file=sys.stderr)
        return 2

    report = Simulator(cfg).run()
    path = write_report(report, cfg.report_file)
    print(format_summary(report))
    if path:
        print(f"Bericht: {path}")
    t = report["totals"]
    if cfg.fail_on_findings and (t["errors"] or t["checksFailed"]):
        return 1
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Zeigt, welche Positionen erzeugt würden – ohne Plattform."""
    from egsim.profiles import plan_positions, select_intervals
    from egsim.timeutil import quarter_hours, resolve_delivery_date

    try:
        cfg = load_config(args.config, for_run=False)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    seed = args.seed if args.seed is not None else (cfg.seed or 0)
    d = resolve_delivery_date(args.date or cfg.delivery_dates[0])
    all_iv = quarter_hours(d) if args.resolution == "QUARTER_HOUR" else None
    rows = []
    for c in cfg.communities:
        for n in range(1, c.count + 1):
            key = c.name if c.count == 1 else f"{c.name} #{n}"
            rng = random.Random(f"{seed}:{key}:{d.isoformat()}")
            ivs = select_intervals(all_iv, cfg.quarter_hour_intervals, rng) if all_iv else None
            for p in plan_positions(c, args.resolution, ivs, all_iv, rng):
                rows.append({"community": key, **p.body(d.isoformat())})
    print(json.dumps(rows, indent=2, default=str, ensure_ascii=False))
    return 0


def cmd_mock(args: argparse.Namespace) -> int:
    from egsim.mockserver import serve
    serve(args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="egsim", description=f"Dummy-Energiegemeinschaften für die Handelsplattform-API "
                                                          f"(Datenvertrag {CONTRACT_VERSION})")
    p.add_argument("--version", action="version", version=f"egsim {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=1, help="-v Info (Standard), -vv Debug inkl. Requests")
    p.add_argument("-q", "--quiet", action="store_true", help="nur Zusammenfassung")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Simulation gegen eine Plattform ausführen")
    r.add_argument("-c", "--config", default="config.yaml")
    r.add_argument("--base-url", help="überschreibt platform.baseUrl")
    r.add_argument("--date", action="append", help="Liefertag (mehrfach möglich): today, tomorrow, +N, YYYY-MM-DD")
    r.add_argument("--close", action="store_true", help="Markt nach dem Einreichen schließen (Admin nötig)")
    r.add_argument("--no-checks", action="store_true", help="keine Negativ-/Vertragstests")
    r.add_argument("--seed", type=int)
    r.add_argument("--report", help="Pfad für den JSON-Bericht ('' = keiner)")
    r.set_defaults(func=cmd_run)

    pl = sub.add_parser("plan", help="geplante Positionen anzeigen (ohne Plattform)")
    pl.add_argument("-c", "--config", default="config.yaml")
    pl.add_argument("--date")
    pl.add_argument("--resolution", choices=["DAY", "QUARTER_HOUR"], default="DAY")
    pl.add_argument("--seed", type=int)
    pl.set_defaults(func=cmd_plan)

    m = sub.add_parser("mock", help="In-Memory-Mock der Plattform starten (zum Testen des Simulators)")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8080)
    m.add_argument("--admin-api-key", default="admin-key")
    m.add_argument("--admin-email", default="admin@platform.example")
    m.add_argument("--admin-password", default="admin-password")
    m.add_argument("--market", action="append",
                   help="Markt anlegen: DATUM[:DAY|QUARTER_HOUR[:ANKAUF:VERKAUF:RESERVE]], z. B. tomorrow:DAY:0.10:0.14:0.20 "
                        "(Standard: tomorrow:DAY)")
    m.add_argument("--closes-in-minutes", type=float, default=120, help="closesAt = jetzt + N Minuten")
    m.add_argument("--reserve-capacity", type=str, default=None,
                   help="max. Reserve je Intervall in kWh (Standard: unbegrenzt)")
    m.add_argument("--settlement-delay", type=float, default=0, help="Settlement asynchron nach N Sekunden (202)")
    m.set_defaults(func=cmd_mock)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(0 if args.quiet else args.verbose)
    return args.func(args)
