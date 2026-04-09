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

from challenge_http_cli import bearer_token, load_env_file
from nightwatch_worker.http_client import ApiClient
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

    sid = effective_session_id(
        SimpleNamespace(session_id=args.session_id)
    )
    _require_base_url()
    token = bearer_token()
    api_base = os.environ.get("API_URL", "").strip().rstrip("/")

    log = configure_logging(session_id=sid)
    client = ApiClient(api_base, token)

    verify_auth_optional(client, log, skip=args.skip_verify)
    wait_until_session_running(
        client,
        sid,
        log,
        poll_interval_sec=poll_interval,
        assume_active=args.assume_session_active,
        skip_session_start=args.skip_session_start,
    )

    log.info(
        "worker started (poll_interval=%.1fs); reconcile loop not yet implemented",
        poll_interval,
        extra={"incident_id": "-", "action_id": "-"},
    )

    try:
        while True:
            time.sleep(poll_interval)
            log.debug("poll tick (placeholder)")
    except KeyboardInterrupt:
        log.info("stopped by user", extra={"incident_id": "-", "action_id": "-"})
