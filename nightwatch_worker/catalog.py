"""Session catalog: fetch, in-memory cache, stale-on-failure, parsing, planning."""

from __future__ import annotations

import logging
import time
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Mapping

from nightwatch_worker.http_client import ApiClient

# Backoff for catalog GET failures (non-blocking for reconcile: only schedules next attempt).
_INITIAL_CATALOG_BACKOFF_SEC = 0.5
_MAX_CATALOG_BACKOFF_SEC = 60.0


@dataclass(frozen=True)
class ActionMeta:
    """Global action definition subset used for dependency and concurrency rules."""

    action_id: str
    dependencies: frozenset[str]
    parallel: bool  # False => serial (no overlap with other serial work per incident API rules)


@dataclass(frozen=True)
class CatalogSnapshot:
    """Parsed catalog at one point in time."""

    actions: Mapping[str, ActionMeta]
    playbooks: Mapping[str, tuple[str, ...]]  # incident_type -> ordered action ids

    def playbook(self, incident_type: str) -> tuple[str, ...] | None:
        t = incident_type.strip()
        if not t:
            return None
        if t in self.playbooks:
            return self.playbooks[t]
        if "_" in t:
            base, suf = t.rsplit("_", 1)
            if suf.isdigit() and base in self.playbooks:
                return self.playbooks[base]
        return None

    def action_meta(self, action_id: str) -> ActionMeta | None:
        return self.actions.get(action_id)


@dataclass
class IncidentPlanningState:
    """Minimal incident view for :func:`eligible_next_actions`."""

    incident_type: str
    completed_action_ids: frozenset[str] = field(default_factory=frozenset)
    failed_action_ids: frozenset[str] = field(default_factory=frozenset)
    in_flight_action_ids: frozenset[str] = field(default_factory=frozenset)
    accepts_actions: bool = True
    finished: bool = False


@dataclass
class CatalogRefreshResult:
    """Outcome of a non-blocking refresh attempt."""

    success: bool
    http_status: int
    snapshot: CatalogSnapshot | None
    last_fetched_at: float | None  # time.time() of last successful fetch; None if never ok
    used_stale: bool  # True if we returned cache after a failed or skipped fetch
    error_message: str | None = None


def _as_str_id(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _dependency_set(raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str) and raw.strip():
        return frozenset([raw.strip()])
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for x in raw:
            s = _as_str_id(x)
            if s:
                out.append(s)
        return frozenset(out)
    return frozenset()


def _parallel_from_obj(obj: dict[str, Any]) -> bool:
    """Infer parallel vs serial; default False (serial) when ambiguous — safer for API rules."""
    if "parallel" in obj:
        return bool(obj["parallel"])
    if "serial" in obj:
        return not bool(obj["serial"])
    mode = obj.get("mode") or obj.get("execution_mode") or obj.get("execution")
    if isinstance(mode, str):
        n = mode.strip().lower()
        if n in ("parallel", "concurrent"):
            return True
        if n in ("serial", "exclusive", "sequential"):
            return False
    return False


def _parse_action(obj: dict[str, Any]) -> ActionMeta | None:
    aid = _as_str_id(obj.get("action_id") or obj.get("id"))
    if not aid:
        return None
    deps = _dependency_set(obj.get("dependencies"))
    deps |= _dependency_set(obj.get("depends_on"))
    deps |= _dependency_set(obj.get("requires"))
    return ActionMeta(action_id=aid, dependencies=deps, parallel=_parallel_from_obj(obj))


def _normalize_playbook_steps(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str) and raw.strip():
        return (raw.strip(),)
    if not isinstance(raw, (list, tuple)):
        return ()
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
        elif isinstance(item, dict):
            aid = _as_str_id(item.get("action_id") or item.get("id"))
            if aid:
                ids.append(aid)
    return tuple(ids)


def _merge_playbook_dict(raw: dict[str, Any], into: dict[str, tuple[str, ...]]) -> None:
    for inc_type, steps in raw.items():
        key = str(inc_type).strip()
        if not key:
            continue
        norm = _normalize_playbook_steps(steps)
        if norm:
            into[key] = norm


def _extract_actions_dict(actions_obj: dict[str, Any]) -> dict[str, ActionMeta]:
    """Shape: ``"actions": { "action_id": { "execution": "parallel", ... } }``."""
    out: dict[str, ActionMeta] = {}
    for aid_raw, spec in actions_obj.items():
        aid = _as_str_id(aid_raw)
        if not aid:
            continue
        if not isinstance(spec, dict):
            spec = {}
        deps = _dependency_set(spec.get("dependencies")) | _dependency_set(
            spec.get("depends_on")
        )
        out[aid] = ActionMeta(
            action_id=aid,
            dependencies=deps,
            parallel=_parallel_from_obj(spec),
        )
    return out


def _merge_resolution_dependencies_into_actions(
    payload: dict[str, Any],
    actions: dict[str, ActionMeta],
) -> None:
    """``depends_on`` on ``resolution_actions`` entries supplements global action metadata."""

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for sk in (
                "resolution_actions",
                "playbook_steps",
                "ordered_actions",
                "steps",
            ):
                raw = obj.get(sk)
                if isinstance(raw, list):
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        aid = _as_str_id(item.get("action_id") or item.get("id"))
                        if not aid:
                            continue
                        step_deps = _dependency_set(item.get("depends_on")) | _dependency_set(
                            item.get("dependencies")
                        )
                        prev = actions.get(aid)
                        parallel = prev.parallel if prev is not None else _parallel_from_obj(item)
                        base = prev.dependencies if prev is not None else frozenset()
                        if step_deps or prev is not None:
                            actions[aid] = ActionMeta(
                                action_id=aid,
                                dependencies=base | step_deps,
                                parallel=parallel,
                            )
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    walk(payload)


def _extract_playbooks_from_type_catalog(
    catalog_block: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    """``catalog``: slug -> ``{ incident_type, resolution_actions: [...] }``."""
    out: dict[str, tuple[str, ...]] = {}
    for outer_key, entry in catalog_block.items():
        if not isinstance(entry, dict):
            continue
        steps: tuple[str, ...] = ()
        for rk in (
            "resolution_actions",
            "playbook_steps",
            "ordered_actions",
            "steps",
        ):
            raw = entry.get(rk)
            if isinstance(raw, list):
                steps = _normalize_playbook_steps(raw)
                if steps:
                    break
        if not steps:
            continue
        type_keys: set[str] = set()
        ik = _as_str_id(entry.get("incident_type"))
        if ik:
            type_keys.add(ik)
        ok = _as_str_id(outer_key)
        if ok:
            type_keys.add(ok)
        for tk in type_keys:
            out[tk] = steps
    return out


def _extract_playbooks(payload: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for key in ("playbooks", "incident_playbooks", "incident_types", "types"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            _merge_playbook_dict(raw, out)
            break
    for key in ("playbook_index", "by_type"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            _merge_playbook_dict(raw, out)
    nested = payload.get("catalog")
    if isinstance(nested, dict):
        out.update(_extract_playbooks_from_type_catalog(nested))
        _merge_playbook_dict(_extract_playbooks(nested), out)
    return out


def _extract_actions(payload: dict[str, Any]) -> dict[str, ActionMeta]:
    out: dict[str, ActionMeta] = {}
    raw_actions = payload.get("actions")
    if isinstance(raw_actions, dict):
        out.update(_extract_actions_dict(raw_actions))
    elif isinstance(raw_actions, list):
        for item in raw_actions:
            if isinstance(item, dict):
                meta = _parse_action(item)
                if meta is not None:
                    out[meta.action_id] = meta
    if not isinstance(raw_actions, dict) and not (
        isinstance(raw_actions, list) and raw_actions
    ):
        for key in ("global_actions", "action_definitions", "definitions"):
            raw = payload.get(key)
            if not isinstance(raw, list):
                continue
            for item in raw:
                if isinstance(item, dict):
                    meta = _parse_action(item)
                    if meta is not None:
                        out[meta.action_id] = meta
            if raw:
                break
    nested = payload.get("catalog")
    if isinstance(nested, dict):
        for aid, meta in _extract_actions(nested).items():
            out.setdefault(aid, meta)
    _merge_resolution_dependencies_into_actions(payload, out)
    return out


def parse_catalog_body(data: Any) -> CatalogSnapshot:
    """Turn GET /sessions/{{id}}/catalog JSON into a :class:`CatalogSnapshot`."""
    if not isinstance(data, dict):
        return CatalogSnapshot(actions={}, playbooks={})
    actions = _extract_actions(data)
    playbooks = _extract_playbooks(data)
    return CatalogSnapshot(actions=actions, playbooks=playbooks)


def incident_type_mappable(snapshot: CatalogSnapshot | None, incident_type: str) -> bool:
    """True if cached catalog lists a playbook for this incident type."""
    if snapshot is None:
        return False
    return snapshot.playbook(incident_type.strip()) is not None


def eligible_next_actions(
    snapshot: CatalogSnapshot | None,
    state: IncidentPlanningState,
) -> list[str]:
    """Return action ids that may be submitted next for this incident (dependency + serial/parallel)."""
    if state.finished or not state.accepts_actions:
        return []
    if snapshot is None:
        return []
    book = snapshot.playbook(state.incident_type)
    if book is None:
        return []

    completed = state.completed_action_ids
    failed = state.failed_action_ids
    inflight = state.in_flight_action_ids

    def meta_for(aid: str) -> ActionMeta:
        m = snapshot.action_meta(aid)
        if m is None:
            return ActionMeta(action_id=aid, dependencies=frozenset(), parallel=False)
        return m

    order = {aid: i for i, aid in enumerate(book)}
    candidates: list[str] = []
    for aid in book:
        if aid in completed or aid in failed or aid in inflight:
            continue
        m = meta_for(aid)
        if not completed.issuperset(m.dependencies):
            continue
        candidates.append(aid)
    if not candidates:
        return []
    candidates.sort(key=lambda a: order.get(a, 10**9))

    serial_in_flight = any(not meta_for(aid).parallel for aid in inflight)
    parallel_in_flight = sum(1 for aid in inflight if meta_for(aid).parallel)

    first = candidates[0]
    if not meta_for(first).parallel:
        if serial_in_flight or inflight:
            return []
        return [first]

    if serial_in_flight:
        return []

    out: list[str] = []
    for aid in candidates:
        if not meta_for(aid).parallel:
            break
        if parallel_in_flight + len(out) >= 2:
            break
        out.append(aid)
    return out


@dataclass
class CatalogCache:
    """In-memory catalog with last fetch time and non-blocking refresh + backoff on errors."""

    retry_5xx_attempts: int = 2
    retry_network_attempts: int = 2

    _snapshot: CatalogSnapshot | None = field(init=False, default=None)
    _last_fetched_at: float | None = field(init=False, default=None)
    _backoff_sec: float = field(init=False, default=_INITIAL_CATALOG_BACKOFF_SEC)
    _next_attempt_monotonic: float = field(init=False, default=0.0)

    @property
    def snapshot(self) -> CatalogSnapshot | None:
        return self._snapshot

    @property
    def last_fetched_at(self) -> float | None:
        return self._last_fetched_at

    def maybe_refresh(
        self,
        client: ApiClient,
        session_id: str,
        *,
        force: bool = False,
        log: logging.LoggerAdapter | None = None,
        now_monotonic: float | None = None,
    ) -> CatalogRefreshResult:
        """Fetch catalog if due; never blocks on backoff — skips network until ``next_attempt``."""
        mono = time.monotonic() if now_monotonic is None else float(now_monotonic)
        path = f"/sessions/{session_id}/catalog"

        if not force and mono < self._next_attempt_monotonic and self._snapshot is not None:
            return CatalogRefreshResult(
                success=True,
                http_status=200,
                snapshot=self._snapshot,
                last_fetched_at=self._last_fetched_at,
                used_stale=True,
            )

        if not force and mono < self._next_attempt_monotonic:
            return CatalogRefreshResult(
                success=False,
                http_status=0,
                snapshot=None,
                last_fetched_at=None,
                used_stale=False,
                error_message="catalog fetch skipped (backoff, no cache yet)",
            )

        try:
            status, body = client.get_json(
                path,
                rate_limited=False,
                retry_5xx_attempts=self.retry_5xx_attempts,
                retry_network_attempts=self.retry_network_attempts,
            )
        except urllib.error.URLError as e:
            self._schedule_retry(mono)
            msg = f"network error: {e}"
            if log:
                log.warning("catalog GET failed (%s); using stale cache=%s", msg, self._snapshot is not None)
            return CatalogRefreshResult(
                success=False,
                http_status=0,
                snapshot=self._snapshot,
                last_fetched_at=self._last_fetched_at,
                used_stale=self._snapshot is not None,
                error_message=msg,
            )

        if status == 200 and isinstance(body, dict):
            self._snapshot = parse_catalog_body(body)
            self._last_fetched_at = time.time()
            self._backoff_sec = _INITIAL_CATALOG_BACKOFF_SEC
            self._next_attempt_monotonic = 0.0

            # Optional: persist the raw catalog as a markdown file after each successful retrieval
            # (handy for inspecting real API shapes, debugging parsers, or attaching to bug reports).
            # Uses a single stable filename so session changes or tests do not accumulate catalog-*.md.
            # Leave commented out for normal runs if you do not want files in cwd.
            #
            # import json
            # from pathlib import Path

            # _path = Path("catalog.md")
            # _path.write_text(
            #     "# Session catalog\n\n"
            #     f"Session id: `{session_id}`\n\n"
            #     "## Raw JSON\n\n```json\n"
            #     + json.dumps(body, indent=2)
            #     + "\n```\n",
            #     encoding="utf-8",
            # )

            if log:
                log.info(
                    "catalog refreshed (%d types, %d actions)",
                    len(self._snapshot.playbooks),
                    len(self._snapshot.actions),
                )
            return CatalogRefreshResult(
                success=True,
                http_status=status,
                snapshot=self._snapshot,
                last_fetched_at=self._last_fetched_at,
                used_stale=False,
            )

        self._schedule_retry(mono)
        msg = f"http_status={status}"
        if log:
            log.warning(
                "catalog GET failed (%s); using stale cache=%s",
                msg,
                self._snapshot is not None,
            )
        return CatalogRefreshResult(
            success=False,
            http_status=status,
            snapshot=self._snapshot,
            last_fetched_at=self._last_fetched_at,
            used_stale=self._snapshot is not None,
            error_message=msg,
        )

    def _schedule_retry(self, mono: float) -> None:
        wait = min(self._backoff_sec, _MAX_CATALOG_BACKOFF_SEC)
        self._next_attempt_monotonic = mono + wait
        self._backoff_sec = min(self._backoff_sec * 2, _MAX_CATALOG_BACKOFF_SEC)
