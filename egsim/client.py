"""HTTP-Client für die Handelsplattform. Wirft bei HTTP-Fehlern nicht, sondern liefert ApiResponse zurück."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import requests

from egsim import contract

log = logging.getLogger("egsim.http")


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # kürzeste Darstellung, z. B. 0.12 statt 0.11999999
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


@dataclass
class ApiResponse:
    method: str
    path: str
    status: int
    body: Any
    elapsed_ms: float
    error: str | None = None  # Transportfehler (Timeout, Verbindung)
    content_type: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def code(self) -> str | None:
        return self.body.get("code") if isinstance(self.body, dict) else None

    @property
    def detail_codes(self) -> set[str]:
        if not isinstance(self.body, dict):
            return set()
        return {d.get("code") for d in self.body.get("details") or [] if isinstance(d, dict) and d.get("code")}

    def short(self) -> str:
        if self.error:
            return f"{self.method} {self.path} -> {self.error}"
        extra = f" {self.code}" if self.code else ""
        return f"{self.method} {self.path} -> {self.status}{extra}"


class RequestStats:
    """Thread-sichere Statistik je Endpoint (Template) und Statuscode."""

    def __init__(self, log_size: int = 2000) -> None:
        self._lock = threading.Lock()
        self.log: deque[dict] = deque(maxlen=log_size)  # einzelne Requests für die UI
        self._seq = 0
        self.counts: dict[tuple[str, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.latencies: dict[tuple[str, str], list[float]] = defaultdict(list)
        self.transport_errors = 0
        self.validate_contract = True          # Antworten gegen die Schemas des Datenvertrags prüfen
        self._contract_seq = 0
        self.contract_log: list[dict] = []     # Abweichungen von Schema und Vertrag, je Antwort

    @property
    def contract_mark(self) -> int:
        with self._lock:
            return self._contract_seq

    def record_contract(self, method: str, template: str, status: int, issues: list[tuple[str, str]], request: str) -> None:
        with self._lock:
            for severity, message in issues:
                self._contract_seq += 1
                self.contract_log.append({"seq": self._contract_seq, "method": method, "endpoint": template,
                                          "status": status, "severity": severity, "message": message,
                                          "request": request})

    def contract_issues(self, since: int = 0) -> list[dict]:
        """Abweichungen seit `since`, gleiche Meldungen je Endpoint und Status zusammengefasst."""
        with self._lock:
            rows = [e for e in self.contract_log if e["seq"] > since]
        grouped: dict[tuple, dict] = {}
        for e in rows:
            key = (e["method"], e["endpoint"], e["status"], e["severity"], e["message"])
            g = grouped.setdefault(key, {**e, "count": 0})
            g["count"] += 1
        return list(grouped.values())

    def record(self, method: str, template: str, status: int, ms: float) -> None:
        with self._lock:
            self.counts[(method, template)][status] += 1
            self.latencies[(method, template)].append(ms)
            if status == 0:
                self.transport_errors += 1

    def record_detail(self, entry: dict) -> None:
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self.log.append(entry)

    def log_since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self.log if e["seq"] > seq]

    def summary(self) -> list[dict]:
        out = []
        with self._lock:
            for key in sorted(self.counts):
                lat = sorted(self.latencies[key])
                n = len(lat)
                out.append({
                    "method": key[0],
                    "endpoint": key[1],
                    "requests": n,
                    "statusCounts": {str(k): v for k, v in sorted(self.counts[key].items())},
                    "latencyMs": {
                        "p50": round(lat[n // 2], 1),
                        "p95": round(lat[min(n - 1, int(n * 0.95))], 1),
                        "max": round(lat[-1], 1),
                    },
                })
        return out


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        stats: RequestStats,
        timeout: float = 10.0,
        verify_tls: bool = True,
        api_key: str | None = None,
        token: str | None = None,
        label: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.stats = stats
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.api_key = api_key
        self.token = token
        self.label = label
        self._session = requests.Session()

    def with_auth(self, *, api_key: str | None = None, token: str | None = None, label: str | None = None) -> "ApiClient":
        return ApiClient(
            self.base_url, stats=self.stats, timeout=self.timeout, verify_tls=self.verify_tls,
            api_key=api_key, token=token, label=label if label is not None else self.label,
        )

    # ------------------------------------------------------------------ core
    def call(
        self,
        method: str,
        template: str,
        *,
        path_params: dict[str, str] | None = None,
        json_body: Any = None,
        raw_body: str | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        headers: dict[str, str] | None = None,
    ) -> ApiResponse:
        path = template.format(**(path_params or {}))
        h = {"Accept": "application/json"}
        if auth:
            if self.api_key:
                h["X-API-Key"] = self.api_key
            elif self.token:
                h["Authorization"] = f"Bearer {self.token}"
        if headers:
            h.update(headers)
        data = None
        if raw_body is not None:
            data = raw_body.encode()
            h["Content-Type"] = "application/json"
        elif json_body is not None:
            data = json.dumps(_to_jsonable(json_body)).encode()
            h["Content-Type"] = "application/json"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        t0 = time.perf_counter()
        try:
            r = self._session.request(
                method, self.base_url + path, data=data, params=clean_params,
                headers=h, timeout=self.timeout, verify=self.verify_tls,
            )
        except requests.RequestException as exc:
            ms = (time.perf_counter() - t0) * 1000
            self.stats.record(method, template, 0, ms)
            self._detail(method, path, clean_params, json_body, raw_body, 0, None, ms, str(exc))
            log.warning("[%s] %s %s -> Transportfehler: %s", self.label, method, path, exc)
            return ApiResponse(method, path, 0, None, ms, error=type(exc).__name__ + ": " + str(exc))
        ms = (time.perf_counter() - t0) * 1000
        body: Any = None
        if r.content:
            try:
                body = json.loads(r.content, parse_float=Decimal)
            except ValueError:
                body = r.text
        self.stats.record(method, template, r.status_code, ms)
        self._detail(method, path, clean_params, json_body, raw_body, r.status_code, body, ms, None)
        log.debug("[%s] %s %s -> %s (%.0f ms)", self.label, method, path, r.status_code, ms)
        if self.stats.validate_contract and not template.startswith("/_mock"):
            issues = contract.check_response(method, template, r.status_code, body, r.headers.get("Content-Type"))
            if issues:
                self.stats.record_contract(method, template, r.status_code, issues, f"{method} {path} -> {r.status_code}")
        return ApiResponse(method, path, r.status_code, body, ms, content_type=r.headers.get("Content-Type"))

    def _detail(self, method, path, params, json_body, raw_body, status, body, ms, error) -> None:
        req = _to_jsonable(json_body) if json_body is not None else raw_body
        if isinstance(req, dict) and "password" in req:
            req = {**req, "password": "***"}
        resp = _to_jsonable(body) if not isinstance(body, str) else body
        if isinstance(resp, dict):  # Secrets nie im Log
            resp = {k: ("***" if k in ("apiKey", "password", "token") else v) for k, v in resp.items()}
        text = json.dumps(resp, ensure_ascii=False) if resp is not None else ""
        if len(text) > 6000:
            resp = text[:6000] + " …(gekürzt)"
        self.stats.record_detail({
            "time": time.strftime("%H:%M:%S"), "actor": self.label, "method": method, "path": path,
            "query": params or None, "status": status, "ms": round(ms, 1), "request": req, "response": resp,
            "error": error,
        })

    # -------------------------------------------------------------- endpoints
    def health_ready(self) -> ApiResponse:
        return self.call("GET", "/health/ready", auth=False)

    def login(self, email: str, password: str) -> ApiResponse:
        return self.call("POST", "/auth/login", json_body={"email": email, "password": password}, auth=False)

    def logout(self) -> ApiResponse:
        return self.call("POST", "/auth/logout")

    def me(self) -> ApiResponse:
        return self.call("GET", "/me")

    def register(self, email: str, name: str, description: str | None = None) -> ApiResponse:
        body = {"email": email, "name": name}
        if description:
            body["description"] = description
        return self.call("POST", "/energy-communities", json_body=body, auth=False)

    def list_communities(self, status: str | None = None) -> ApiResponse:
        return self.call("GET", "/energy-communities", params={"status": status})

    def get_community(self, ec_id: str) -> ApiResponse:
        return self.call("GET", "/energy-communities/{id}", path_params={"id": ec_id})

    def update_community(self, ec_id: str, body: dict) -> ApiResponse:
        return self.call("PUT", "/energy-communities/{id}", path_params={"id": ec_id}, json_body=body)

    def delete_community(self, ec_id: str) -> ApiResponse:
        return self.call("DELETE", "/energy-communities/{id}", path_params={"id": ec_id})

    def set_status(self, ec_id: str, status: str) -> ApiResponse:
        return self.call("PUT", "/energy-communities/{id}/status", path_params={"id": ec_id}, json_body={"status": status})

    def issue_credentials(self, ec_id: str) -> ApiResponse:
        return self.call("POST", "/energy-communities/{id}/credentials", path_params={"id": ec_id})

    def submit_position(self, body: dict) -> ApiResponse:
        return self.call("POST", "/positions", json_body=body)

    def list_positions(self, **params: Any) -> ApiResponse:
        return self.call("GET", "/positions", params=params)

    def get_position(self, pos_id: str) -> ApiResponse:
        return self.call("GET", "/positions/{id}", path_params={"id": pos_id})

    def update_position(self, pos_id: str, body: dict) -> ApiResponse:
        return self.call("PUT", "/positions/{id}", path_params={"id": pos_id}, json_body=body)

    def cancel_position(self, pos_id: str) -> ApiResponse:
        return self.call("DELETE", "/positions/{id}", path_params={"id": pos_id})

    def get_market(self, d: str) -> ApiResponse:
        return self.call("GET", "/markets/{deliveryDate}", path_params={"deliveryDate": d})

    def close_market(self, d: str) -> ApiResponse:
        return self.call("POST", "/markets/{deliveryDate}/close", path_params={"deliveryDate": d})

    def market_result(self, d: str) -> ApiResponse:
        return self.call("GET", "/markets/{deliveryDate}/result", path_params={"deliveryDate": d})

    def allocations(self, d: str, **params: Any) -> ApiResponse:
        return self.call("GET", "/markets/{deliveryDate}/allocations", path_params={"deliveryDate": d}, params=params)

    def trades(self, d: str, **params: Any) -> ApiResponse:
        return self.call("GET", "/markets/{deliveryDate}/trades", path_params={"deliveryDate": d}, params=params)
