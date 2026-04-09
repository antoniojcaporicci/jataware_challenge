"""Thin HTTP client: bearer auth, JSON helpers, per-endpoint rate gate, selective retries."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


MIN_INCIDENT_ENDPOINT_INTERVAL_SEC = 5.0

_INCIDENTS_LIST = re.compile(r"^/sessions/[^/]+/incidents$")
_INCIDENT_DETAIL = re.compile(r"^/sessions/[^/]+/incidents/[^/]+$")
_INCIDENT_EVENTS = re.compile(r"^/sessions/[^/]+/incidents/[^/]+/events$")
_INCIDENT_ACTION = re.compile(r"^/sessions/[^/]+/incidents/[^/]+/action$")


def incident_rate_family(method: str, path: str) -> str | None:
    """Return a stable key for incident API rate limiting, or None if not rate-limited."""
    p = path.split("?", 1)[0]
    m = method.upper()
    if m == "GET" and _INCIDENTS_LIST.match(p):
        return "GET:/sessions/.../incidents"
    if m == "GET" and _INCIDENT_DETAIL.match(p):
        return "GET:/sessions/.../incidents/{id}"
    if m == "GET" and _INCIDENT_EVENTS.match(p):
        return "GET:/sessions/.../incidents/{id}/events"
    if m == "POST" and _INCIDENT_ACTION.match(p):
        return "POST:/sessions/.../incidents/{id}/action"
    return None


def _full_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return base + p


@dataclass
class RateGate:
    """Tracks last call time per rate key; sleep until the next slot is allowed."""

    interval_sec: float = MIN_INCIDENT_ENDPOINT_INTERVAL_SEC
    _last: dict[str, float] = field(default_factory=dict)

    def seconds_until_allowed(self, key: str) -> float:
        """Seconds to wait before a call on ``key`` would be allowed (0 if now is ok)."""
        last = self._last.get(key)
        if last is None:
            return 0.0
        elapsed = time.monotonic() - last
        return max(0.0, self.interval_sec - elapsed)

    def wait_until_allowed(self, key: str) -> None:
        wait = self.seconds_until_allowed(key)
        if wait > 0:
            time.sleep(wait)
        self._last[key] = time.monotonic()

    def record_immediate(self, key: str) -> None:
        """Mark ``key`` as just used (for tests or external scheduling)."""
        self._last[key] = time.monotonic()


class ApiClient:
    """GET/POST with bearer token, JSON bodies, incident endpoint rate gate, optional GET retries."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        min_incident_interval_sec: float = MIN_INCIDENT_ENDPOINT_INTERVAL_SEC,
        timeout_sec: float = 120.0,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._timeout = timeout_sec
        self._opener = opener or urllib.request.build_opener()
        self._gate = RateGate(interval_sec=min_incident_interval_sec)

    @property
    def rate_gate(self) -> RateGate:
        return self._gate

    def seconds_until_incident_slot(self, method: str, path: str) -> float:
        key = incident_rate_family(method, path)
        if key is None:
            return 0.0
        return self._gate.seconds_until_allowed(key)

    def _apply_rate_gate(self, method: str, path: str, *, rate_limited: bool) -> None:
        if not rate_limited:
            return
        key = incident_rate_family(method, path)
        if key is not None:
            self._gate.wait_until_allowed(key)

    def _open(self, req: urllib.request.Request) -> tuple[int, bytes]:
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        accept: str | None = None,
    ) -> tuple[int, bytes]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            **(extra_headers or {}),
        }
        if accept:
            headers["Accept"] = accept
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(
            _full_url(self._base_url, path),
            data=body,
            method=method.upper(),
            headers=headers,
        )
        return self._open(req)

    @staticmethod
    def _decode_json_body(data: bytes) -> Any:
        if not data:
            return None
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        return json.loads(text)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | list[Any] | None = None,
        rate_limited: bool = True,
        accept: str = "application/json",
        retry_get_5xx_attempts: int = 0,
        retry_get_network_attempts: int = 0,
        retry_backoff_sec: float = 0.5,
    ) -> tuple[int, Any]:
        """Issue a request; decode JSON response when body is non-empty JSON.

        Retries apply only to **GET** when ``retry_*_attempts`` > 0: 5xx responses and
        ``URLError`` (transient network). **POST** is never retried here. **4xx** responses
        are never retried.
        """
        m = method.upper()
        body_bytes: bytes | None = None
        if json_body is not None:
            if m not in ("POST", "PUT", "PATCH"):
                raise ValueError("json_body is only valid for POST/PUT/PATCH")
            body_bytes = json.dumps(json_body).encode("utf-8")

        self._apply_rate_gate(m, path, rate_limited=rate_limited)

        remain_5xx = retry_get_5xx_attempts
        remain_net = retry_get_network_attempts
        backoff = retry_backoff_sec
        last_status = 0
        last_data = b""
        while True:
            try:
                status, raw = self._request_raw(
                    m, path, body=body_bytes, accept=accept
                )
            except urllib.error.URLError:
                if m != "GET" or remain_net <= 0:
                    raise
                remain_net -= 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue

            last_status, last_data = status, raw
            if m == "GET" and status >= 500 and remain_5xx > 0:
                remain_5xx -= 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            break

        parsed: Any
        try:
            parsed = self._decode_json_body(last_data)
        except json.JSONDecodeError:
            parsed = last_data.decode("utf-8", errors="replace")

        return last_status, parsed

    def get_json(
        self,
        path: str,
        *,
        rate_limited: bool = True,
        retry_5xx_attempts: int = 0,
        retry_network_attempts: int = 0,
        retry_backoff_sec: float = 0.5,
    ) -> tuple[int, Any]:
        return self.request_json(
            "GET",
            path,
            rate_limited=rate_limited,
            retry_get_5xx_attempts=retry_5xx_attempts,
            retry_get_network_attempts=retry_network_attempts,
            retry_backoff_sec=retry_backoff_sec,
        )

    def post_json(
        self,
        path: str,
        payload: dict[str, Any] | list[Any],
        *,
        rate_limited: bool = True,
    ) -> tuple[int, Any]:
        return self.request_json(
            "POST",
            path,
            json_body=payload,
            rate_limited=rate_limited,
        )
