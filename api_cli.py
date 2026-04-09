#!/usr/bin/env python3
"""
CLI for the candidate API (OpenAPI-aligned paths and POST bodies).

Loads the secrets env file when present (does not override existing environment variables).
session-create upserts SESSION_ID into that file on success. Requires API_URL and API_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def load_env_file(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key or key in os.environ:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            os.environ[key] = val


def upsert_env_var(path: str, key: str, value: str) -> None:
    """Replace KEY=… if present, else append. Preserves comments and other keys."""
    lines: list[str] = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        k, _, _ = line.partition("=")
        if k.strip() == key:
            new_lines.append(f"{key}={value}\n")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        new_lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def effective_session_id(args: argparse.Namespace) -> str:
    raw = getattr(args, "session_id", None)
    if raw:
        return str(raw).strip()
    env_sid = os.environ.get("SESSION_ID", "").strip()
    if env_sid:
        return env_sid
    sys.stderr.write(
        "Missing session id: pass it as the first argument, run session-create "
        "(updates SESSION_ID in your env file), or export SESSION_ID.\n"
    )
    sys.exit(2)


def base_url() -> str:
    url = os.environ.get("API_URL", "").strip().rstrip("/")
    if not url:
        sys.stderr.write("Missing API_URL (set in environment or secrets.env).\n")
        sys.exit(2)
    return url


def bearer_token() -> str:
    token = os.environ.get("API_TOKEN", "").strip()
    if not token:
        sys.stderr.write("Missing API_TOKEN (set in environment or secrets.env).\n")
        sys.exit(2)
    return token


def api_url(path: str) -> str:
    base = base_url().rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return base + p


def http_request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    accept: str | None = None,
) -> tuple[int, bytes]:
    h = {
        "Authorization": f"Bearer {bearer_token()}",
        **(headers or {}),
    }
    if accept:
        h["Accept"] = accept
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(api_url(path), data=body, method=method.upper(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def print_response(status: int, data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")
    if text.strip().startswith("{") or text.strip().startswith("["):
        try:
            parsed = json.loads(text)
            print(json.dumps(parsed, indent=2))
            return
        except json.JSONDecodeError:
            pass
    print(text, end="" if text.endswith("\n") else "\n")
    if status >= 400:
        sys.exit(1)


def cmd_verify(_: argparse.Namespace) -> None:
    status, data = http_request("GET", "/auth/verify")
    print_response(status, data)


def cmd_scenarios(_: argparse.Namespace) -> None:
    status, data = http_request("GET", "/scenarios")
    print_response(status, data)


def cmd_session_create(args: argparse.Namespace) -> None:
    if args.body:
        payload = json.loads(args.body.read())
    else:
        payload = {
            "session_mode": args.session_mode,
            "scenario_type": args.scenario_type,
        }
    body = json.dumps(payload).encode("utf-8")
    status, data = http_request("POST", "/sessions", body=body)
    if not args.no_save_session and status == 200:
        try:
            parsed = json.loads(data.decode("utf-8"))
            sid = parsed.get("session_id")
            if sid:
                path = args.env_file
                if os.path.isfile(path):
                    upsert_env_var(path, "SESSION_ID", sid)
                    os.environ["SESSION_ID"] = sid
                    print(f"(updated SESSION_ID in {path})", file=sys.stderr)
                else:
                    print(
                        f"(skip saving SESSION_ID: {path} does not exist yet)",
                        file=sys.stderr,
                    )
        except (json.JSONDecodeError, TypeError, OSError) as e:
            print(f"(could not save session id: {e})", file=sys.stderr)
    print_response(status, data)


def cmd_session_get(args: argparse.Namespace) -> None:
    status, data = http_request("GET", f"/sessions/{effective_session_id(args)}")
    print_response(status, data)


def cmd_session_start(args: argparse.Namespace) -> None:
    sid = effective_session_id(args)
    status, data = http_request("POST", f"/sessions/{sid}/start", body=b"{}")
    print_response(status, data)


def cmd_session_stop(args: argparse.Namespace) -> None:
    sid = effective_session_id(args)
    status, data = http_request("POST", f"/sessions/{sid}/stop", body=b"{}")
    print_response(status, data)


def cmd_catalog(args: argparse.Namespace) -> None:
    sid = effective_session_id(args)
    status, data = http_request("GET", f"/sessions/{sid}/catalog")
    print_response(status, data)


def cmd_incidents(args: argparse.Namespace) -> None:
    sid = effective_session_id(args)
    status, data = http_request("GET", f"/sessions/{sid}/incidents")
    print_response(status, data)


def cmd_incident(args: argparse.Namespace) -> None:
    sid = effective_session_id(args)
    status, data = http_request(
        "GET", f"/sessions/{sid}/incidents/{args.incident_id}"
    )
    print_response(status, data)


def cmd_incident_events(args: argparse.Namespace) -> None:
    sid = effective_session_id(args)
    status, data = http_request(
        "GET",
        f"/sessions/{sid}/incidents/{args.incident_id}/events",
    )
    print_response(status, data)


def cmd_action(args: argparse.Namespace) -> None:
    if args.body is not None:
        payload = json.loads(args.body.read())
    elif args.action_id:
        payload = {"action_id": args.action_id}
        if args.notes is not None:
            payload["notes"] = args.notes
    else:
        sys.stderr.write(
            "Provide a JSON body file (positional) or --action-id (optional --notes).\n"
        )
        sys.exit(2)
    body = json.dumps(payload).encode("utf-8")
    sid = effective_session_id(args)
    status, data = http_request(
        "POST",
        f"/sessions/{sid}/incidents/{args.incident_id}/action",
        body=body,
    )
    print_response(status, data)


def cmd_summary(args: argparse.Namespace) -> None:
    accept = "text/markdown" if args.markdown else "application/json"
    sid = effective_session_id(args)
    status, data = http_request(
        "GET",
        f"/sessions/{sid}/summary",
        accept=accept,
    )
    print_response(status, data)


def cmd_stream(args: argparse.Namespace) -> None:
    """Read the SSE stream until stdout is closed or max-bytes reached."""
    sid = effective_session_id(args)
    req = urllib.request.Request(
        api_url(f"/sessions/{sid}/stream"),
        headers={"Authorization": f"Bearer {bearer_token()}", "Accept": "text/event-stream"},
        method="GET",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        print_response(e.code, e.read())
        return
    try:
        remaining = args.max_bytes
        while remaining > 0:
            chunk = resp.read(min(8192, remaining))
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            remaining -= len(chunk)
    finally:
        resp.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Call the jataware challenge HTTP API.")
    p.add_argument(
        "--env-file",
        default="secrets.env",
        metavar="PATH",
        help="Load KEY=value pairs if file exists (default: secrets.env).",
    )
    p.add_argument(
        "--no-env-file",
        action="store_true",
        help="Do not load secrets env file (use shell-exported API_URL/API_TOKEN only).",
    )

    sub = p.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="GET /auth/verify")
    p_verify.set_defaults(func=cmd_verify)

    p_scen = sub.add_parser("scenarios", help="GET /scenarios")
    p_scen.set_defaults(func=cmd_scenarios)

    p_create = sub.add_parser(
        "session-create",
        help="POST /sessions (body: session_mode, scenario_type; see OpenAPI)",
    )
    p_create.add_argument(
        "--body",
        type=argparse.FileType("r"),
        default=None,
        metavar="FILE",
        help="JSON file (overrides --session-mode / --scenario-type)",
    )
    p_create.add_argument(
        "--session-mode",
        choices=["practice", "challenge"],
        default="practice",
        help="When not using --body (default: practice)",
    )
    p_create.add_argument(
        "--scenario-type",
        default="practice-starter",
        metavar="TYPE",
        help="When not using --body (default: practice-starter, per API example)",
    )
    p_create.add_argument(
        "--no-save-session",
        action="store_true",
        help="Do not upsert SESSION_ID into --env-file after success.",
    )
    p_create.set_defaults(func=cmd_session_create)

    def add_session_id(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "session_id",
            nargs="?",
            default=None,
            metavar="SESSION_ID",
            help="Defaults to SESSION_ID from env file or environment.",
        )

    for name, help_text, fn in [
        ("session-get", "GET /sessions/{id}", cmd_session_get),
        ("session-start", "POST /sessions/{id}/start", cmd_session_start),
        ("session-stop", "POST /sessions/{id}/stop", cmd_session_stop),
        ("catalog", "GET /sessions/{id}/catalog", cmd_catalog),
        ("incidents", "GET /sessions/{id}/incidents", cmd_incidents),
    ]:
        sp = sub.add_parser(name, help=help_text)
        add_session_id(sp)
        sp.set_defaults(func=fn)

    p_in = sub.add_parser("incident", help="GET /sessions/.../incidents/{incident_id}")
    add_session_id(p_in)
    p_in.add_argument("incident_id")
    p_in.set_defaults(func=cmd_incident)

    p_ev = sub.add_parser("incident-events", help="GET .../incidents/{id}/events")
    add_session_id(p_ev)
    p_ev.add_argument("incident_id")
    p_ev.set_defaults(func=cmd_incident_events)

    p_act = sub.add_parser(
        "action",
        help="POST .../incidents/{id}/action (body: action_id, optional notes)",
    )
    add_session_id(p_act)
    p_act.add_argument("incident_id")
    p_act.add_argument(
        "body",
        nargs="?",
        type=argparse.FileType("r"),
        default=None,
        help='JSON file, or omit and use --action-id (use "-" for stdin)',
    )
    p_act.add_argument(
        "--action-id",
        dest="action_id",
        default=None,
        metavar="ID",
        help="e.g. info (alternative to JSON body file)",
    )
    p_act.add_argument(
        "--notes",
        default=None,
        metavar="TEXT",
        help="With --action-id only",
    )
    p_act.set_defaults(func=cmd_action)

    p_sum = sub.add_parser("summary", help="GET /sessions/{id}/summary")
    add_session_id(p_sum)
    p_sum.add_argument(
        "--markdown",
        action="store_true",
        help="Request text/markdown instead of JSON",
    )
    p_sum.set_defaults(func=cmd_summary)

    p_stream = sub.add_parser(
        "stream",
        help="GET /sessions/{id}/stream (SSE; prints raw bytes until max-bytes)",
    )
    add_session_id(p_stream)
    p_stream.add_argument(
        "--max-bytes",
        type=int,
        default=1_000_000,
        metavar="N",
        help="Stop after reading this many bytes (default: 1000000)",
    )
    p_stream.set_defaults(func=cmd_stream)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.no_env_file and args.env_file:
        load_env_file(args.env_file)
    args.func(args)


if __name__ == "__main__":
    main()
