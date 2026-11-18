"""Bericht als JSON speichern und als Kurzfassung auf der Konsole ausgeben."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


def write_report(report: dict, path_template: str | None) -> Path | None:
    if not path_template:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = Path(path_template.format(timestamp=ts, runId=report.get("runId", "")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=_default), encoding="utf-8")
    return p


def format_summary(report: dict) -> str:
    t = report["totals"]
    lines = [
        "",
        "=" * 72,
        f"EGSim {report['egsimVersion']} · Datenvertrag {report['contractVersion']} · {report['baseUrl']}",
        f"Lauf {report['runId']} · Seed {report['seed']}",
        "-" * 72,
        f"EGs: {sum(1 for c in report['communities'] if c['ready'])}/{len(report['communities'])} einsatzbereit",
    ]
    for m in report["markets"]:
        lines.append(f"Markt {m['deliveryDate']} ({m['resolution'] or '?'}): Start {m['marketStatusAtStart'] or '-'}, "
                     f"Ende {m['marketStatusAtEnd'] or '-'}")
        lines.append(f"  Positionen {m['positionsSubmitted']}/{m['positionsPlanned']} angenommen, "
                     f"{m['updates']} geändert, {m['cancellations']} storniert · "
                     f"Angebot {m['offersKwh']} kWh, Bedarf {m['demandKwh']} kWh")
        v = m.get("verification")
        if v:
            lines.append(f"  Ergebnisprüfung ({'vollständig' if v.get('mode') == 'full' else 'EG-Sicht'}): {v['status']} "
                         f"({v.get('comparisons', 0)} Vergleiche, {v.get('mismatches', 0)} Abweichungen)")
            ref = v.get("reference")
            if ref:
                lines.append(f"  Referenz: Einkauf {ref['purchaseCostEur']} €, Verkauf {ref['salesRevenueEur']} €, "
                             f"Reserve {ref['reserveDispatchedKwh']} kWh / {ref['reserveCostEur']} €, Marge {ref['marginEur']} €")
    for sc in report.get("scenarios") or []:
        mark = {"PASS": "✓", "FAIL": "✗"}.get(sc["result"], "·")
        lines.append(f"  {mark} {sc['id']} {sc['title']}: {sc['result']}"
                     + (f" – {sc['messages'][0]}" if sc["messages"] and sc["result"] != "PASS" else ""))
    lines.append(f"Checks: {t['checksPassed']} bestanden, {t['checksFailed']} fehlgeschlagen, {t['checksSkipped']} übersprungen")
    for c in report["checks"]:
        if c["result"] == "FAIL":
            lines.append(f"  ✗ {c['id']} {c['title']}: erwartet {c['expected']}, erhalten "
                         f"{c['actualStatus']} {c['actualCode'] or ''}{' – ' + c['note'] if c.get('note') else ''}")
    lines.append(f"Requests: {t['requests']} · Fehler: {t['errors']} · Warnungen: {t['warnings']}")
    errs = [f for f in report["findings"] if f["severity"] == "error" and f["area"] != "check"]
    for f in errs[:15]:
        lines.append(f"  ! [{f['area']}] {f['message']}")
    if len(errs) > 15:
        lines.append(f"  … {len(errs) - 15} weitere im Bericht")
    lines.append("=" * 72)
    return "\n".join(lines)
