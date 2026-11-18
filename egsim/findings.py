"""Sammelt Auffälligkeiten (Abweichungen vom Datenvertrag) während eines Laufs."""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass

log = logging.getLogger("egsim")


@dataclass
class Finding:
    severity: str  # error | warning | info
    area: str      # z. B. provisioning, submit, verify, check
    message: str
    request: str | None = None


class Findings:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[Finding] = []

    def add(self, severity: str, area: str, message: str, request: str | None = None) -> None:
        f = Finding(severity, area, message, request)
        with self._lock:
            self.items.append(f)
        lvl = {"error": logging.ERROR, "warning": logging.WARNING}.get(severity, logging.INFO)
        log.log(lvl, "[%s] %s%s", area, message, f" ({request})" if request else "")

    def error(self, area: str, message: str, request: str | None = None) -> None:
        self.add("error", area, message, request)

    def warning(self, area: str, message: str, request: str | None = None) -> None:
        self.add("warning", area, message, request)

    def info(self, area: str, message: str, request: str | None = None) -> None:
        self.add("info", area, message, request)

    def count(self, severity: str) -> int:
        return sum(1 for f in self.items if f.severity == severity)

    def as_list(self) -> list[dict]:
        return [asdict(f) for f in self.items]
