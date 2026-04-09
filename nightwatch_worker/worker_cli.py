#!/usr/bin/env python3
"""
Long-running **nightwatch worker**: CLI entrypoint, logging, and the future reconcile loop.

For **one-off HTTP calls** to the API (verify, session-create, catalog, incidents, …), use
``challenge_http_cli.py`` at the repo root instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

from challenge_http_cli import bearer_token, load_env_file, upsert_env_var
from nightwatch_worker.catalog import CatalogCache
from nightwatch_worker.http_client import ApiClient
from nightwatch_worker.incidents import (
    open_incidents_fingerprint,
    reconcile_tick,
)
from nightwatch_worker.session_lifecycle import (
    SessionPhase,
    fetch_session_summary,
    main_loop_session_phase,
    verify_auth_optional,
    wait_until_session_running,
)


MIN_POLL_INTERVAL_SEC = 5.0


class _WakeCoordinator:
    """Thread-safe wait that can be signaled from another thread (e.g. future SSE wakeup)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._signaled = False

    def notify_check(self) -> None:
        with self._cv:
            self._signaled = True
            self._cv.notify_all()

    def wait_until(self, deadline_monotonic: float) -> None:
        with self._cv:
            while not self._signaled:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(timeout=remaining)
            self._signaled = False


class _StructuredFormatter(logging.Formatter):
    """Fill optional fields so ``session_id`` / ``incident_id`` always render in the format."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "session_id"):
            setattr(record, "session_id", "-")
        if not hasattr(record, "incident_id"):
            setattr(record, "incident_id", "-")
        return super().format(record)


def configure_logging(
    *, session_id: str, level: int = logging.INFO
) -> logging.LoggerAdapter:
    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _StructuredFormatter(
            fmt=(
                "%(levelname)s [session=%(session_id)s] "
                "[incident_id=%(incident_id)s] %(message)s"
            ),
        )
    )
    handler.setLevel(level)
    root.addHandler(handler)
    root.setLevel(level)
    base = logging.getLogger("nightwatch_worker")
    base.setLevel(level)
    ctx = {"session_id": session_id, "incident_id": "-"}
    # Python 3.13+ LoggerAdapter drops per-call ``extra`` unless merge_extra=True.
    try:
        return logging.LoggerAdapter(base, ctx, merge_extra=True)
    except TypeError:
        return logging.LoggerAdapter(base, ctx)


def effective_session_id(args: argparse.Namespace) -> str:
    raw = getattr(args, "session_id", None)
    if raw:
        return str(raw).strip()
    env_sid = os.environ.get("SESSION_ID", "").strip()
    if env_sid:
        return env_sid
    sys.stderr.write(
        "Missing session id: pass --session-id, set SESSION_ID in secrets.env, or export it.\n"
    )
    sys.exit(2)


def _require_base_url() -> None:
    url = os.environ.get("API_URL", "").strip()
    if not url:
        sys.stderr.write("Missing API_URL (set in environment or secrets.env).\n")
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Nightwatch incident worker (long-running process). "
            "For raw API subcommands use challenge_http_cli.py."
        )
    )
    p.add_argument(
        "--env-file",
        default="secrets.env",
        metavar="PATH",
        help="Load KEY=value pairs if file exists (default: secrets.env).",
    )
    p.add_argument(
        "--no-env-file",
        action="store_true",
        help="Do not load secrets env (use shell-exported API_URL/API_TOKEN only).",
    )
    p.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        metavar="ID",
        help="Session id (default: SESSION_ID from env or secrets file).",
    )
    p.add_argument(
        "--poll-interval",
        type=float,
        default=MIN_POLL_INTERVAL_SEC,
        metavar="SEC",
        help=(
            f"Seconds between placeholder poll ticks (default: {MIN_POLL_INTERVAL_SEC}); "
            f"minimum {MIN_POLL_INTERVAL_SEC} to stay within per-endpoint limits."
        ),
    )
    p.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip GET /auth/verify on startup.",
    )
    p.add_argument(
        "--assume-session-active",
        action="store_true",
        help=(
            "Skip polling GET /sessions/{id} until running (use when API unreachable, e.g. tests)."
        ),
    )
    p.add_argument(
        "--skip-session-start",
        action="store_true",
        help="Do not POST /sessions/{id}/start on startup (poll session state only).",
    )
    p.add_argument(
        "--skip-initial-catalog",
        action="store_true",
        help=(
            "Skip GET /sessions/{id}/catalog immediately after the session is active "
            "(for offline tests or when the API is unreachable)."
        ),
    )
    p.add_argument(
        "--exit-if-session-finished",
        action="store_true",
        help=(
            "If GET /sessions/{id} is already finished, exit successfully instead of "
            "POST /sessions to create a new session (default: create a new session)."
        ),
    )
    p.add_argument(
        "--session-mode",
        choices=["practice", "challenge"],
        default="practice",
        help="When creating a session after a finished one (default: practice).",
    )
    p.add_argument(
        "--scenario-type",
        default="practice-starter",
        metavar="TYPE",
        help=(
            "When creating a session after a finished one (default: practice-starter)."
        ),
    )
    p.add_argument(
        "--skip-incident-polling",
        action="store_true",
        help=(
            "Do not poll GET /sessions/{id}/incidents each tick (for offline / no-API tests)."
        ),
    )
    p.add_argument(
        "--max-action-posts-per-tick",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Cap POST …/action calls per reconcile tick (default: 1). "
            "Raise only if the API tolerates higher throughput; each POST honors the 5s gate."
        ),
    )
    p.add_argument(
        "--dead-man-minutes",
        type=float,
        default=25.0,
        metavar="MIN",
        help=(
            "If there are open incidents and their HTTP-derived fingerprint does not change "
            "for this long, log and exit (0 disables)."
        ),
    )
    p.add_argument(
        "--summary-json",
        action="store_true",
        help="Fetch GET …/summary as JSON instead of markdown.",
    )
    p.add_argument(
        "--log-level",
        choices=["INFO", "DEBUG"],
        default="INFO",
        help="Log verbosity: DEBUG includes POST …/action response bodies (default: INFO).",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not args.no_env_file and args.env_file:
        load_env_file(args.env_file)

    poll_interval = max(MIN_POLL_INTERVAL_SEC, float(args.poll_interval))
    if poll_interval != float(args.poll_interval):
        sys.stderr.write(
            f"poll-interval raised to minimum {MIN_POLL_INTERVAL_SEC}s (API rate limits).\n"
        )

    initial_sid = effective_session_id(
        SimpleNamespace(session_id=args.session_id)
    )
    _require_base_url()
    token = bearer_token()
    api_base = os.environ.get("API_URL", "").strip().rstrip("/")

    log_level = getattr(logging, str(args.log_level))
    log = configure_logging(session_id=initial_sid, level=log_level)
    client = ApiClient(api_base, token)

    verify_auth_optional(client, log, skip=args.skip_verify)
    sid = wait_until_session_running(
        client,
        initial_sid,
        log,
        poll_interval_sec=poll_interval,
        assume_active=args.assume_session_active,
        skip_session_start=args.skip_session_start,
        recreate_if_finished=not args.exit_if_session_finished,
        session_mode=args.session_mode,
        scenario_type=args.scenario_type,
    )
    if (
        sid != initial_sid
        and not args.no_env_file
        and args.env_file
        and os.path.isfile(args.env_file)
    ):
        upsert_env_var(args.env_file, "SESSION_ID", sid)
        os.environ["SESSION_ID"] = sid

    catalog_cache: CatalogCache | None = None
    if args.skip_initial_catalog:
        log.info(
            "skipping initial catalog fetch (--skip-initial-catalog)",
            extra={"incident_id": "-"},
        )
    else:
        catalog_cache = CatalogCache()
        cat_result = catalog_cache.maybe_refresh(client, sid, force=True, log=log)
        if cat_result.success:
            pass
        elif cat_result.used_stale and cat_result.snapshot is not None:
            log.warning(
                "initial catalog fetch failed; continuing with cached snapshot from earlier in process",
                extra={"incident_id": "-"},
            )
        else:
            log.warning(
                "initial catalog fetch failed (%s); continuing without catalog until next refresh",
                cat_result.error_message or f"http_status={cat_result.http_status}",
                extra={"incident_id": "-"},
            )

    catalog_loaded = bool(
        catalog_cache is not None and catalog_cache.snapshot is not None
    )
    log.info(
        "worker started (poll_interval=%.1fs, catalog_loaded=%s, incident_polling=%s)",
        poll_interval,
        catalog_loaded,
        not args.skip_incident_polling,
        extra={"incident_id": "-"},
    )

    poll_session_in_loop = not args.assume_session_active
    wake = _WakeCoordinator()
    local_pending: dict[str, set[str]] = {}
    reject_backoff: dict[str, float] = {}
    last_open_fp: str | None = None
    last_progress_mono = time.monotonic()
    dead_man_sec = float(args.dead_man_minutes) * 60.0 if args.dead_man_minutes > 0 else 0.0

    try:
        while True:
            tick_start = time.monotonic()

            if poll_session_in_loop:
                phase, raw_status = main_loop_session_phase(client, sid, log=log)
                if phase == SessionPhase.TERMINAL_BAD:
                    log.error(
                        "session reached terminal failure during run (status=%r)",
                        raw_status,
                        extra={"incident_id": "-"},
                    )
                    sys.exit(1)
                if phase == SessionPhase.TERMINAL_OK:
                    st_sum, body_sum = fetch_session_summary(
                        client,
                        sid,
                        log=log,
                        prefer_markdown=not args.summary_json,
                    )
                    log.info(
                        "session finished (status=%r); summary http_status=%s",
                        raw_status,
                        st_sum,
                        extra={"incident_id": "-"},
                    )
                    if isinstance(body_sum, str) and body_sum.strip():
                        print(body_sum.rstrip())
                    elif isinstance(body_sum, (dict, list)):
                        print(json.dumps(body_sum, indent=2))
                    return

            if args.skip_incident_polling:
                wake.wait_until(tick_start + poll_interval)
                continue

            tick_result = reconcile_tick(
                client,
                sid,
                log,
                catalog_cache=catalog_cache,
                fetch_targeted_detail=True,
                execute_actions=True,
                local_pending=local_pending,
                reject_backoff_until=reject_backoff,
                max_action_posts_per_tick=max(1, int(args.max_action_posts_per_tick)),
            )

            fp = open_incidents_fingerprint(tick_result.open_incidents)
            if tick_result.open_incidents:
                if fp != last_open_fp:
                    last_progress_mono = time.monotonic()
                    last_open_fp = fp
                for att in tick_result.action_attempts:
                    if att.ok:
                        last_progress_mono = time.monotonic()
                if dead_man_sec > 0:
                    stall = time.monotonic() - last_progress_mono
                    if stall > dead_man_sec:
                        log.error(
                            "dead man: no progress on open incidents for %.0fs — exiting",
                            stall,
                            extra={
                                "incident_id": ",".join(
                                    s.incident_id for s in tick_result.open_incidents
                                )
                                or "-"
                            },
                        )
                        sys.exit(1)
            else:
                last_open_fp = None
                last_progress_mono = time.monotonic()

            wake.wait_until(tick_start + poll_interval)
    except KeyboardInterrupt:
        log.info("stopped by user", extra={"incident_id": "-"})
