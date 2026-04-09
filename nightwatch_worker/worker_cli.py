#!/usr/bin/env python3
"""
Long-running **nightwatch worker**: CLI entrypoint, logging, and the future reconcile loop.

For **one-off HTTP calls** to the API (verify, session-create, catalog, incidents, …), use
``challenge_http_cli.py`` at the repo root instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from types import SimpleNamespace

from challenge_http_cli import bearer_token, load_env_file, upsert_env_var
from nightwatch_worker.catalog import CatalogCache
from nightwatch_worker.http_client import ApiClient
from nightwatch_worker.incidents import reconcile_tick
from nightwatch_worker.session_lifecycle import (
    verify_auth_optional,
    wait_until_session_running,
)


MIN_POLL_INTERVAL_SEC = 5.0


class _StructuredFormatter(logging.Formatter):
    """Fills optional record fields so the format string always has defined values."""

    def format(self, record: logging.LogRecord) -> str:
        for key, default in (
            ("session_id", "-"),
            ("incident_id", "-"),
            ("action_id", "-"),
        ):
            if not hasattr(record, key):
                setattr(record, key, default)
        return super().format(record)


def configure_logging(*, session_id: str) -> logging.LoggerAdapter:
    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _StructuredFormatter(
            fmt=(
                "%(levelname)s [session=%(session_id)s] "
                "%(message)s [incident=%(incident_id)s action=%(action_id)s]"
            )
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    base = logging.getLogger("nightwatch_worker")
    return logging.LoggerAdapter(base, {"session_id": session_id})


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

    log = configure_logging(session_id=initial_sid)
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
        log.info("skipping initial catalog fetch (--skip-initial-catalog)")
    else:
        catalog_cache = CatalogCache()
        cat_result = catalog_cache.maybe_refresh(client, sid, force=True, log=log)
        if cat_result.success:
            pass
        elif cat_result.used_stale and cat_result.snapshot is not None:
            log.warning(
                "initial catalog fetch failed; continuing with cached snapshot from earlier in process"
            )
        else:
            log.warning(
                "initial catalog fetch failed (%s); continuing without catalog until next refresh",
                cat_result.error_message or f"http_status={cat_result.http_status}",
            )

    catalog_loaded = bool(
        catalog_cache is not None and catalog_cache.snapshot is not None
    )
    log.info(
        "worker started (poll_interval=%.1fs, catalog_loaded=%s, incident_polling=%s)",
        poll_interval,
        catalog_loaded,
        not args.skip_incident_polling,
        extra={"incident_id": "-", "action_id": "-"},
    )

    try:
        while True:
            if args.skip_incident_polling:
                time.sleep(poll_interval)
                log.debug("poll tick (incident polling skipped)")
                continue
            t0 = time.monotonic()
            reconcile_tick(
                client,
                sid,
                log,
                catalog_cache=catalog_cache,
                fetch_targeted_detail=True,
            )
            sleep_left = max(0.0, poll_interval - (time.monotonic() - t0))
            time.sleep(sleep_left)
    except KeyboardInterrupt:
        log.info("stopped by user", extra={"incident_id": "-", "action_id": "-"})
