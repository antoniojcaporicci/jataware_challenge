"""Session lifecycle: optional auth verify and poll until GET /sessions/{id} is active or terminal."""

from __future__ import annotations

import logging
import sys
import time
import urllib.error
from enum import Enum, auto
from typing import Any

from nightwatch_worker.http_client import ApiClient


class SessionPhase(Enum):
    """How the worker should treat session GET payload."""

    RUNNING = auto()
    PENDING = auto()
    TERMINAL_OK = auto()
    TERMINAL_BAD = auto()
    UNKNOWN = auto()


def _normalize_status_token(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


# API field names vary; these cover common shapes and can be extended after first live responses.
_STATUS_KEYS = ("status", "session_status", "state", "phase", "lifecycle")

_RUNNING = frozenset(
    {
        "running",
        "active",
        "in_progress",
        "live",
        "started",
        "simulation_running",
    }
)
_PENDING = frozenset(
    {
        "pending",
        "created",
        "idle",
        "waiting",
        "not_started",
        "scheduled",
        "queued",
        "initializing",
        "starting",
    }
)
_TERMINAL_OK = frozenset(
    {
        "completed",
        "finished",
        "done",
        "summary_ready",
        "closed",
        "stopped",
    }
)
_TERMINAL_BAD = frozenset(
    {
        "failed",
        "error",
        "cancelled",
        "canceled",
        "aborted",
        "expired",
        "timeout",
        "timed_out",
    }
)


def session_status_from_payload(data: Any) -> tuple[SessionPhase, str]:
    """Classify a GET /sessions/{{id}} JSON body. Returns (phase, raw status or '')."""
    if not isinstance(data, dict):
        return SessionPhase.UNKNOWN, ""
    if data.get("session_finished") is True:
        v = data.get("status") or data.get("session_status") or "session_finished"
        return SessionPhase.TERMINAL_OK, str(v) if v is not None else "session_finished"
    raw = ""
    for key in _STATUS_KEYS:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            raw = v.strip()
            break
    if not raw:
        return SessionPhase.UNKNOWN, ""
    n = _normalize_status_token(raw)
    if n in _RUNNING:
        return SessionPhase.RUNNING, raw
    if n in _PENDING:
        return SessionPhase.PENDING, raw
    if n in _TERMINAL_OK:
        return SessionPhase.TERMINAL_OK, raw
    if n in _TERMINAL_BAD:
        return SessionPhase.TERMINAL_BAD, raw
    return SessionPhase.UNKNOWN, raw


def verify_auth_optional(
    client: ApiClient,
    log: logging.LoggerAdapter,
    *,
    skip: bool,
) -> None:
    if skip:
        log.info("skipping GET /auth/verify (--skip-verify)")
        return
    try:
        st, body = client.get_json(
            "/auth/verify",
            rate_limited=False,
            retry_network_attempts=3,
            retry_5xx_attempts=3,
        )
    except urllib.error.URLError as e:
        log.error("auth verify failed: network error: %s", e)
        sys.exit(1)
    if st != 200:
        log.error("auth verify failed: http_status=%s body=%s", st, body)
        sys.exit(1)
    log.info("auth verify ok")


def create_session(
    client: ApiClient,
    log: logging.LoggerAdapter,
    *,
    session_mode: str = "practice",
    scenario_type: str = "practice-starter",
) -> str:
    """POST /sessions; return new ``session_id`` (exits process on hard failure)."""
    payload = {"session_mode": session_mode, "scenario_type": scenario_type}
    try:
        st, body = client.post_json("/sessions", payload, rate_limited=False)
    except urllib.error.URLError as e:
        log.error("POST /sessions network error: %s", e)
        sys.exit(1)
    if st not in (200, 201):
        log.error("POST /sessions failed: http_status=%s body=%s", st, body)
        sys.exit(1)
    if not isinstance(body, dict):
        log.error("POST /sessions unexpected response body: %s", body)
        sys.exit(1)
    sid = body.get("session_id") or body.get("id")
    if not sid:
        log.error("POST /sessions response missing session_id: %s", body)
        sys.exit(1)
    new_id = str(sid).strip()
    log.info("created new session (session_id=%s)", new_id)
    return new_id


def attempt_session_start(
    client: ApiClient,
    session_id: str,
    log: logging.LoggerAdapter,
) -> None:
    """POST /sessions/{id}/start once. Non-fatal on most errors; polling will confirm state."""
    path = f"/sessions/{session_id}/start"
    try:
        st, body = client.post_json(path, {}, rate_limited=False)
    except urllib.error.URLError as e:
        log.warning(
            "POST session/start network error: %s — will poll GET /sessions/%s",
            e,
            session_id,
        )
        return

    if st in (200, 201, 204):
        log.info("session start accepted (http_status=%s)", st)
        return
    if st == 403:
        log.warning(
            "session start forbidden (403); this token may not start the session — "
            "start it elsewhere, then the worker will proceed once GET shows running"
        )
        return
    if st == 409:
        log.info("session start conflict (409); treating as already started — polling state")
        return
    if 400 <= st < 500:
        log.warning(
            "session start returned http_status=%s body=%s — polling session state",
            st,
            body,
        )
        return
    log.warning(
        "session start returned http_status=%s — polling session state",
        st,
    )


def wait_until_session_running(
    client: ApiClient,
    session_id: str,
    log: logging.LoggerAdapter,
    *,
    poll_interval_sec: float,
    assume_active: bool,
    skip_session_start: bool,
    recreate_if_finished: bool = False,
    session_mode: str = "practice",
    scenario_type: str = "practice-starter",
) -> str:
    if assume_active:
        log.info(
            "assuming session already active (--assume-session-active); skipping session poll"
        )
        return session_id

    current_id = session_id

    if skip_session_start:
        log.info("skipping POST /sessions/{id}/start (--skip-session-start)")
    else:
        attempt_session_start(client, current_id, log)

    path = f"/sessions/{current_id}"
    while True:
        try:
            st, body = client.get_json(
                path,
                rate_limited=False,
                retry_network_attempts=3,
                retry_5xx_attempts=3,
            )
        except urllib.error.URLError as e:
            log.warning(
                "GET session network error: %s; retrying in %.1fs",
                e,
                poll_interval_sec,
            )
            time.sleep(poll_interval_sec)
            continue

        if st == 404:
            log.error("session not found (404): %s", path)
            sys.exit(2)
        if st != 200:
            log.warning(
                "GET session unexpected http_status=%s; retrying in %.1fs",
                st,
                poll_interval_sec,
            )
            time.sleep(poll_interval_sec)
            continue

        phase, raw = session_status_from_payload(body)
        if phase == SessionPhase.RUNNING:
            log.info("session is active (status=%r); entering main loop", raw)
            return current_id
        if phase == SessionPhase.TERMINAL_OK:
            if not recreate_if_finished:
                log.info("session already finished (status=%r); exiting", raw)
                sys.exit(0)
            log.info(
                "session already finished (status=%r); creating a new session",
                raw,
            )
            current_id = create_session(
                client,
                log,
                session_mode=session_mode,
                scenario_type=scenario_type,
            )
            if hasattr(log, "extra") and isinstance(log.extra, dict):
                log.extra["session_id"] = current_id
            path = f"/sessions/{current_id}"
            if skip_session_start:
                log.info(
                    "skipping POST /sessions/{id}/start for new session (--skip-session-start)"
                )
            else:
                attempt_session_start(client, current_id, log)
            continue
        if phase == SessionPhase.TERMINAL_BAD:
            log.error("session in terminal failure state (status=%r); exiting", raw)
            sys.exit(1)
        if phase == SessionPhase.UNKNOWN:
            keys = list(body.keys())[:15] if isinstance(body, dict) else []
            log.warning(
                "session status unknown or missing (keys=%s); retrying in %.1fs",
                keys,
                poll_interval_sec,
            )
        else:
            log.info(
                "session not yet active (status=%r); polling again in %.1fs",
                raw,
                poll_interval_sec,
            )
        time.sleep(poll_interval_sec)
