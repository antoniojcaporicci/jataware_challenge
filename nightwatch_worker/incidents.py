"""Incidents: fleet list polling (step 5), list-first state + targeted detail (step 6), shared reconcile tick."""

from __future__ import annotations

import logging
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from nightwatch_worker.catalog import (
    CatalogCache,
    IncidentPlanningState,
    eligible_next_actions,
    incident_type_mappable,
)
from nightwatch_worker.http_client import ApiClient


def _norm_status(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


_OPENISH = frozenset(
    {
        "open",
        "active",
        "running",
        "pending",
        "new",
        "started",
        "in_progress",
        "acknowledged",
    }
)
_CLOSED_OK = frozenset(
    {
        "resolved",
        "closed",
        "completed",
        "done",
        "finished",
    }
)
_CLOSED_BAD = frozenset(
    {
        "expired",
        "failed",
        "cancelled",
        "canceled",
        "error",
    }
)


def _first_present(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _parse_expires_ts(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        s = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _optional_action_set(d: dict[str, Any], keys: tuple[str, ...]) -> frozenset[str] | None:
    for k in keys:
        if k not in d:
            continue
        raw = d[k]
        if raw is None:
            return frozenset()
        if not isinstance(raw, (list, tuple)):
            return frozenset()
        ids: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                ids.append(x.strip())
            elif isinstance(x, dict):
                aid = x.get("action_id") or x.get("id")
                if aid is not None:
                    ids.append(str(aid).strip())
        return frozenset(ids)
    return None


def _any_action_keys_present(d: dict[str, Any], key_groups: Iterable[tuple[str, ...]]) -> bool:
    for keys in key_groups:
        for k in keys:
            if k in d:
                return True
    return False


@dataclass
class IncidentListRow:
    """One row from GET /sessions/{{id}}/incidents (bulk list)."""

    incident_id: str
    incident_type: str
    raw_status: str
    normalized_status: str
    is_open: bool
    expires_at_ts: float | None
    completed_actions: frozenset[str] | None
    failed_actions: frozenset[str] | None
    in_flight_actions: frozenset[str] | None
    accepts_actions: bool | None
    actions_known: bool
    accepts_known: bool


@dataclass
class IncidentState:
    """Unified view for catalog planning (list ± detail). HTTP responses are source of truth."""

    incident_id: str
    incident_type: str
    completed_action_ids: frozenset[str]
    failed_action_ids: frozenset[str]
    in_flight_action_ids: frozenset[str]
    accepts_actions: bool
    finished: bool
    expires_at_ts: float | None
    status_token: str
    detail_truth: bool  # True after targeted GET merge (or fully specified list row)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ActionAttempt:
    incident_id: str
    action_id: str
    http_status: int
    ok: bool


@dataclass
class ReconcileTickResult:
    list_http_status: int
    list_error: str | None
    open_incidents: list[IncidentState]  # TTL order (soonest expiry first; unknown expiry last)
    targeted_detail_id: str | None
    detail_http_status: int | None
    action_attempts: list[ActionAttempt] = field(default_factory=list)


def _parse_list_item(obj: Any) -> IncidentListRow | None:
    if not isinstance(obj, dict):
        return None
    d = obj
    iid = _first_present(
        d,
        ("incident_id", "id", "incidentId"),
    )
    if iid is None:
        return None
    incident_id = str(iid).strip()
    if not incident_id:
        return None
    itype_raw = _first_present(
        d,
        ("incident_type", "type", "kind", "scenario", "scenario_type"),
    )
    incident_type = str(itype_raw).strip() if itype_raw is not None else ""

    raw_status = _first_present(
        d,
        ("status", "state", "phase", "lifecycle"),
    )
    raw_s = str(raw_status).strip() if raw_status is not None else ""
    ns = _norm_status(raw_s) if raw_s else ""

    if d.get("incident_finished") is True or d.get("finished") is True:
        is_open = False
    elif d.get("resolved") is True or d.get("expired") is True:
        is_open = False
    elif ns in _CLOSED_OK or ns in _CLOSED_BAD:
        is_open = False
    elif not ns:
        is_open = True
    elif ns in _OPENISH:
        is_open = True
    else:
        is_open = True

    exp_raw = _first_present(
        d,
        (
            "expires_at",
            "expiresAt",
            "expiry",
            "deadline",
            "ttl_expires_at",
            "expires",
        ),
    )
    ttl_sec = _first_present(d, ("ttl_seconds", "ttl_remaining", "seconds_to_expiry"))
    expires_at_ts = _parse_expires_ts(exp_raw)
    if expires_at_ts is None and ttl_sec is not None:
        try:
            expires_at_ts = time.time() + float(ttl_sec)
        except (TypeError, ValueError):
            pass

    completed_keys = ("completed_actions", "actions_completed", "completed", "done_actions")
    failed_keys = ("failed_actions", "actions_failed", "failed")
    inflight_keys = (
        "in_flight_actions",
        "running_actions",
        "pending_actions",
        "active_actions",
    )
    completed = _optional_action_set(d, completed_keys)
    failed = _optional_action_set(d, failed_keys)
    inflight = _optional_action_set(d, inflight_keys)
    actions_known = _any_action_keys_present(
        d,
        (completed_keys, failed_keys, inflight_keys),
    )

    accept_keys = (
        "accepts_actions",
        "can_submit_action",
        "actions_enabled",
        "accepting_actions",
    )
    acc_raw = _first_present(d, accept_keys)
    accepts: bool | None
    accepts_known = any(k in d for k in accept_keys)
    if isinstance(acc_raw, bool):
        accepts = acc_raw
    elif isinstance(acc_raw, str):
        accepts = acc_raw.strip().lower() in ("true", "1", "yes")
    else:
        accepts = None

    return IncidentListRow(
        incident_id=incident_id,
        incident_type=incident_type,
        raw_status=raw_s,
        normalized_status=ns,
        is_open=is_open,
        expires_at_ts=expires_at_ts,
        completed_actions=completed,
        failed_actions=failed,
        in_flight_actions=inflight,
        accepts_actions=accepts,
        actions_known=actions_known,
        accepts_known=accepts_known,
    )


def parse_incidents_list_payload(body: Any) -> list[IncidentListRow]:
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("incidents") or body.get("data") or body.get("items") or []
        if not isinstance(items, list):
            items = []
    else:
        items = []
    rows: list[IncidentListRow] = []
    for it in items:
        row = _parse_list_item(it)
        if row is not None:
            rows.append(row)
    return rows


def open_rows_ttl_order(rows: Iterable[IncidentListRow]) -> list[IncidentListRow]:
    """Open incidents with least time until expiry first; unknown expiry last."""

    open_r = [r for r in rows if r.is_open]
    open_r.sort(
        key=lambda r: (
            r.expires_at_ts is None,
            float("inf") if r.expires_at_ts is None else r.expires_at_ts,
            r.incident_id,
        )
    )
    return open_r


def needs_targeted_detail(row: IncidentListRow) -> bool:
    """True when bulk row is insufficient for safe planning (step 6)."""
    if not row.is_open:
        return False
    if not row.incident_type.strip():
        return True
    if not row.actions_known:
        return True
    if not row.accepts_known:
        return True
    if not row.normalized_status or row.normalized_status in _OPENISH:
        # transitioning / generic open — prefer detail when action fields were inferred missing
        if row.completed_actions is None or row.in_flight_actions is None:
            return True
    return False


def list_row_to_state(row: IncidentListRow, *, detail_truth: bool) -> IncidentState:
    completed = row.completed_actions if row.completed_actions is not None else frozenset()
    failed = row.failed_actions if row.failed_actions is not None else frozenset()
    inflight = row.in_flight_actions if row.in_flight_actions is not None else frozenset()
    accepts = row.accepts_actions if row.accepts_actions is not None else False
    finished = not row.is_open
    return IncidentState(
        incident_id=row.incident_id,
        incident_type=row.incident_type,
        completed_action_ids=completed,
        failed_action_ids=failed,
        in_flight_action_ids=inflight,
        accepts_actions=accepts,
        finished=finished,
        expires_at_ts=row.expires_at_ts,
        status_token=row.raw_status,
        detail_truth=detail_truth
        and row.actions_known
        and row.accepts_known,
        raw={},
    )


def parse_incident_detail_payload(incident_id: str, body: Any) -> IncidentState | None:
    if not isinstance(body, dict):
        return None
    d = body
    row = _parse_list_item({**d, "incident_id": incident_id})
    if row is None:
        return None
    st = list_row_to_state(row, detail_truth=True)
    st.raw = dict(d)
    return st


def merge_detail_over_list(list_row: IncidentListRow, detail: IncidentState) -> IncidentState:
    """Prefer detail fields; keep list expiry if detail omits it."""
    lr = list_row_to_state(list_row, detail_truth=False)
    return IncidentState(
        incident_id=lr.incident_id,
        incident_type=detail.incident_type or lr.incident_type,
        completed_action_ids=detail.completed_action_ids,
        failed_action_ids=detail.failed_action_ids,
        in_flight_action_ids=detail.in_flight_action_ids,
        accepts_actions=detail.accepts_actions,
        finished=detail.finished,
        expires_at_ts=detail.expires_at_ts if detail.expires_at_ts is not None else lr.expires_at_ts,
        status_token=detail.status_token or lr.status_token,
        detail_truth=True,
        raw=detail.raw,
    )


def to_planning_state(state: IncidentState) -> IncidentPlanningState | None:
    """Convert to catalog planner input; None if action snapshot not trustworthy."""
    if not state.detail_truth:
        return None
    return IncidentPlanningState(
        incident_type=state.incident_type,
        completed_action_ids=state.completed_action_ids,
        failed_action_ids=state.failed_action_ids,
        in_flight_action_ids=state.in_flight_action_ids,
        accepts_actions=state.accepts_actions,
        finished=state.finished,
    )


def to_planning_state_with_pending(
    state: IncidentState,
    pending: frozenset[str],
) -> IncidentPlanningState | None:
    """Like :func:`to_planning_state` but treat locally submitted actions as in-flight."""
    base = to_planning_state(state)
    if base is None:
        return None
    return IncidentPlanningState(
        incident_type=base.incident_type,
        completed_action_ids=base.completed_action_ids,
        failed_action_ids=base.failed_action_ids,
        in_flight_action_ids=base.in_flight_action_ids | pending,
        accepts_actions=base.accepts_actions,
        finished=base.finished,
    )


def prune_local_pending(
    states: Iterable[IncidentState],
    local_pending: dict[str, set[str]],
) -> None:
    """Drop pending entries once the API reflects an action as in-flight, done, or failed."""
    for s in states:
        iid = s.incident_id
        if iid not in local_pending:
            continue
        reflected = (
            s.in_flight_action_ids | s.completed_action_ids | s.failed_action_ids
        )
        still = {a for a in local_pending[iid] if a not in reflected}
        if still:
            local_pending[iid] = still
        else:
            del local_pending[iid]


def open_incidents_fingerprint(states: Iterable[IncidentState]) -> str:
    """Stable fingerprint for dead-man / stall detection (open incidents only)."""
    parts: list[str] = []
    for s in states:
        parts.append(
            ":".join(
                (
                    s.incident_id,
                    _norm_status(s.status_token),
                    str(int(s.accepts_actions)),
                    str(len(s.completed_action_ids)),
                    str(len(s.in_flight_action_ids)),
                    str(len(s.failed_action_ids)),
                )
            )
        )
    parts.sort()
    return "|".join(parts)


def _execute_planned_actions(
    *,
    client: ApiClient,
    session_id: str,
    log: logging.LoggerAdapter | None,
    states: list[IncidentState],
    catalog_cache: CatalogCache,
    local_pending: dict[str, set[str]],
    reject_backoff_until: dict[str, float],
    max_action_posts_per_tick: int,
) -> list[ActionAttempt]:
    snap = catalog_cache.snapshot
    if snap is None:
        return []
    prune_local_pending(states, local_pending)
    attempts: list[ActionAttempt] = []
    posts = 0
    now_b = time.monotonic()
    for state in states:
        if posts >= max_action_posts_per_tick:
            break
        if state.finished or not state.accepts_actions:
            continue
        iid = state.incident_id
        if iid in reject_backoff_until and now_b < reject_backoff_until[iid]:
            continue
        if not incident_type_mappable(snap, state.incident_type):
            continue
        pend = frozenset(local_pending.get(iid, ()))
        ps = to_planning_state_with_pending(state, pend)
        if ps is None:
            continue
        eligible = eligible_next_actions(snap, ps)
        for action_id in eligible:
            if posts >= max_action_posts_per_tick:
                break
            path = f"/sessions/{session_id}/incidents/{iid}/action"
            try:
                st_post, body = client.post_json(
                    path,
                    {"action_id": action_id},
                    rate_limited=True,
                )
            except urllib.error.URLError as e:
                if log:
                    log.warning(
                        "POST action network error incident_id=%s action_id=%s: %s",
                        iid,
                        action_id,
                        e,
                        extra={"incident_id": iid, "action_id": action_id},
                    )
                attempts.append(ActionAttempt(iid, action_id, 0, False))
                reject_backoff_until[iid] = time.monotonic() + 6.0
                posts += 1
                continue

            ok = 200 <= st_post < 300
            attempts.append(ActionAttempt(iid, action_id, st_post, ok))
            extra = {"incident_id": iid, "action_id": action_id}
            if ok:
                local_pending.setdefault(iid, set()).add(action_id)
                if log:
                    log.info(
                        "submitted action incident_id=%s action_id=%s http_status=%s",
                        iid,
                        action_id,
                        st_post,
                        extra=extra,
                    )
            elif 400 <= st_post < 500:
                reject_backoff_until[iid] = time.monotonic() + 8.0
                if log:
                    log.warning(
                        "action rejected incident_id=%s action_id=%s http_status=%s body=%s",
                        iid,
                        action_id,
                        st_post,
                        body,
                        extra=extra,
                    )
            else:
                reject_backoff_until[iid] = time.monotonic() + 5.0
                if log:
                    log.warning(
                        "action POST unexpected status incident_id=%s action_id=%s http_status=%s",
                        iid,
                        action_id,
                        st_post,
                        extra=extra,
                    )
            posts += 1
    return attempts


def reconcile_tick(
    client: ApiClient,
    session_id: str,
    log: logging.LoggerAdapter | None,
    *,
    catalog_cache: CatalogCache | None = None,
    fetch_targeted_detail: bool = True,
    detail_retry_5xx: int = 1,
    detail_retry_net: int = 1,
    execute_actions: bool = True,
    local_pending: dict[str, set[str]] | None = None,
    reject_backoff_until: dict[str, float] | None = None,
    max_action_posts_per_tick: int = 1,
) -> ReconcileTickResult:
    """Single shared tick: catalog refresh, bulk incidents GET, optional detail, optional action POSTs."""
    if catalog_cache is not None:
        catalog_cache.maybe_refresh(client, session_id, log=log)

    list_path = f"/sessions/{session_id}/incidents"
    list_err: str | None = None
    list_status = 0
    rows: list[IncidentListRow] = []
    try:
        list_status, body = client.get_json(
            list_path,
            rate_limited=True,
            retry_5xx_attempts=2,
            retry_network_attempts=2,
            retry_429_attempts=3,
        )
    except urllib.error.URLError as e:
        list_err = str(e)
        if log:
            log.warning("GET incidents failed: %s", list_err)
        return ReconcileTickResult(
            list_http_status=0,
            list_error=list_err,
            open_incidents=[],
            targeted_detail_id=None,
            detail_http_status=None,
            action_attempts=[],
        )

    if list_status != 200:
        list_err = f"http_status={list_status}"
        if log:
            log.warning("GET incidents unexpected %s", list_err)
        return ReconcileTickResult(
            list_http_status=list_status,
            list_error=list_err,
            open_incidents=[],
            targeted_detail_id=None,
            detail_http_status=None,
            action_attempts=[],
        )

    rows = parse_incidents_list_payload(body)
    ordered = open_rows_ttl_order(rows)
    targeted: str | None = None
    detail_st: int | None = None
    states: list[IncidentState] = []

    pick: IncidentListRow | None = None
    if fetch_targeted_detail:
        for r in ordered:
            if needs_targeted_detail(r):
                pick = r
                targeted = r.incident_id
                break

    detail_state: IncidentState | None = None
    if pick is not None:
        dpath = f"/sessions/{session_id}/incidents/{pick.incident_id}"
        try:
            detail_st, dbody = client.get_json(
                dpath,
                rate_limited=True,
                retry_5xx_attempts=detail_retry_5xx,
                retry_network_attempts=detail_retry_net,
                retry_429_attempts=3,
            )
        except urllib.error.URLError as e:
            if log:
                log.warning(
                    "GET incident detail failed incident_id=%s: %s",
                    pick.incident_id,
                    e,
                    extra={"incident_id": pick.incident_id, "action_id": "-"},
                )
            detail_st = 0
            dbody = None
        if detail_st == 200:
            detail_state = parse_incident_detail_payload(pick.incident_id, dbody)
            if log:
                log.info(
                    "refreshed incident detail for planning incident_id=%s",
                    pick.incident_id,
                    extra={"incident_id": pick.incident_id, "action_id": "-"},
                )
        elif log:
            log.warning(
                "GET incident detail http_status=%s incident_id=%s",
                detail_st,
                pick.incident_id,
                extra={"incident_id": pick.incident_id, "action_id": "-"},
            )

    detail_id_for_merge = detail_state.incident_id if detail_state else None
    for r in ordered:
        if detail_id_for_merge and r.incident_id == detail_id_for_merge and detail_state:
            states.append(merge_detail_over_list(r, detail_state))
        else:
            states.append(
                list_row_to_state(
                    r,
                    detail_truth=not needs_targeted_detail(r),
                )
            )

    if log:
        log.info(
            "incidents tick: total=%d open=%d detail_target=%s",
            len(rows),
            len(ordered),
            targeted or "-",
            extra={"incident_id": "-", "action_id": "-"},
        )

    attempts: list[ActionAttempt] = []
    if (
        execute_actions
        and catalog_cache is not None
        and catalog_cache.snapshot is not None
    ):
        lp = local_pending if local_pending is not None else {}
        bo = reject_backoff_until if reject_backoff_until is not None else {}
        attempts = _execute_planned_actions(
            client=client,
            session_id=session_id,
            log=log,
            states=states,
            catalog_cache=catalog_cache,
            local_pending=lp,
            reject_backoff_until=bo,
            max_action_posts_per_tick=max(1, int(max_action_posts_per_tick)),
        )

    return ReconcileTickResult(
        list_http_status=list_status,
        list_error=list_err,
        open_incidents=states,
        targeted_detail_id=targeted,
        detail_http_status=detail_st,
        action_attempts=attempts,
    )
