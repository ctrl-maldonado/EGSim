"""Erzeugt egsim/contract.json aus contract/Datenvertrag.yaml.

Nur für Entwickler (braucht PyYAML). Der Simulator selbst liest nur die JSON-Datei und braucht kein PyYAML.

    pip install pyyaml
    python tools/gen_contract.py            # schreibt egsim/contract.json
    python tools/gen_contract.py --check    # prüft zusätzlich alle Beispiele des Vertrags gegen seine Schemas

Wird eine neue Vertragsversion freigegeben: YAML in contract/ ersetzen und dieses Skript erneut ausführen.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "contract" / "Datenvertrag.yaml"
DST = ROOT / "egsim" / "contract.json"
METHODS = ("get", "post", "put", "delete")


def _resolve_examples(node, components):
    """Ersetzt {$ref: '#/components/examples/X'} durch den Wert des Beispiels."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/examples/"):
            return components["examples"][ref.rsplit("/", 1)[1]]
        return {k: _resolve_examples(v, components) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_examples(v, components) for v in node]
    return node


def build(doc: dict) -> dict:
    responses: dict[str, dict] = {}
    examples: list[dict] = []
    for path, item in doc["paths"].items():
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            per_status: dict[str, dict | None] = {}
            for status, resp in op.get("responses", {}).items():
                content = (resp.get("content") or {}).get("application/json")
                per_status[str(status)] = content["schema"] if content else None
                for name, ex in ((content or {}).get("examples") or {}).items():
                    examples.append({"op": f"{method.upper()} {path}", "status": str(status), "name": name,
                                     "value": _resolve_examples(ex, doc["components"]).get("value")})
            responses[f"{method.upper()} {path}"] = {
                "responses": per_status,
                "roles": op.get("x-roles", []),
                "public": op.get("security") == [],
            }
    return {
        "version": doc["info"]["version"],
        "schemas": doc["components"]["schemas"],
        "operations": responses,
        "_examples": examples,
    }


def main() -> int:
    doc = yaml.safe_load(SRC.read_text(encoding="utf-8"))
    out = build(doc)
    examples = out.pop("_examples")
    DST.write_text(json.dumps(out, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{DST.relative_to(ROOT)}: Vertrag {out['version']}, {len(out['schemas'])} Schemas, "
          f"{len(out['operations'])} Operationen")
    if "--check" not in sys.argv:
        return 0
    sys.path.insert(0, str(ROOT))
    from egsim import contract
    contract.cache_clear()
    bad = 0
    for ex in examples:
        value = ex["value"]
        if value is None or (isinstance(value, dict) and any(isinstance(v, str) and v.startswith("<") for v in value.values())):
            continue  # Platzhalter wie <Token>
        status = int(ex["status"])
        issues = contract.check_response(*ex["op"].split(" ", 1), status, _as_decimal(value), "application/json")
        for sev, msg in issues:
            bad += 1
            print(f"  Beispiel {ex['op']} {ex['status']} '{ex['name']}': {sev} {msg}")
    print("Beispiele geprüft:", "alle konform" if not bad else f"{bad} Abweichungen")
    return 1 if bad else 0


def _as_decimal(v):
    from decimal import Decimal
    if isinstance(v, float):
        return Decimal(repr(v))
    if isinstance(v, dict):
        return {k: _as_decimal(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_as_decimal(x) for x in v]
    return v


if __name__ == "__main__":
    raise SystemExit(main())
