# Execution plan: incident worker MVP

Polling baseline first; SSE as a stretch goal. Check boxes as you complete items.

## Ground rules

- [ ] Treat **HTTP GET responses** as source of truth for catalog, incidents, and incident detail.
- [ ] Enforce **≥5s between calls** to each rate-limited incident endpoint (per session), and design the loop so you **don’t starve** incidents close to expiry.
- [ ] One **shared `reconcile()`** (or `tick()`) path that both polling and (later) SSE will call.

## 1. Project skeleton

- [ ] Add a small entrypoint (e.g. `python -m nightwatch_worker` or `worker.py`) that loads `API_URL` / `API_TOKEN` / `SESSION_ID` (reuse `secrets.env` pattern from `api_cli.py` if helpful).
- [ ] CLI flags: `--session-id`, optional `--env-file`, `--no-env-file`; optional `--poll-interval` capped so you stay compliant with the 5s rule.
- [ ] Basic structured logging to stdout (session id, incident id, action id, errors).

## 2. HTTP layer

- [ ] Thin wrapper for `GET`/`POST` with bearer auth and JSON encode/decode.
- [ ] **Per-endpoint rate gate** (last-call timestamp per path pattern or per logical endpoint family) — polling schedules the *next* allowed time, doesn’t busy-loop.
- [ ] Retry/backoff only where appropriate (e.g. catalog 5xx / transient network); don’t retry tight loops on 4xx action errors.

## 3. Session lifecycle

- [ ] Optional: `GET /auth/verify` on startup.
- [ ] **Assume existing session** for MVP path: read `SESSION_ID` from env; document optional future: create/start via API.
- [ ] Poll `GET /sessions/{id}` until session is **running** (or defined “active”) or terminal; handle `session_finished` / failure states cleanly.

## 4. Catalog

- [ ] `GET /sessions/{id}/catalog`; cache in memory with “last fetched at.”
- [ ] On fetch failure: **back off** and retry without wedging the whole loop (still respect rate limits if catalog shares limits).
- [ ] Parse enough structure to map: **playbook steps**, **action metadata** (at least: `action_id`, **dependencies**, **serial vs parallel** per API shape — adjust after first real response).
- [ ] **Planner hook:** given catalog + incident state, compute **eligible next actions**.

## 5. Incidents (discovery)

- [ ] Poll `GET /sessions/{id}/incidents` on the rate-limited schedule.
- [ ] Maintain a set/list of **open** incidents (skip resolved/expired/finished per API fields).
- [ ] **TTL-aware ordering:** prioritize incidents with **least time until expiry** for detail refresh and action attempts.

## 6. Incident detail & state

- [ ] For incidents that need decisions, `GET …/incidents/{incident_id}` (stagger if many ids compete for the same endpoint budget).
- [ ] Derive: **completed / failed actions**, **whether incident accepts actions**, **current in-flight** if exposed in JSON.
- [ ] If detail is ambiguous, optional follow-up: `GET …/incidents/{id}/events` (only if needed — don’t build until detail proves insufficient).

## 7. Action rules (core correctness)

- [ ] **Dependencies:** never submit an action until prerequisites are satisfied (playbook order / deps).
- [ ] **Serial:** at most one serial action “in flight” per incident; no overlap violating API rules.
- [ ] **Parallel:** at most **two** concurrent parallel actions per incident; don’t exceed capacity.
- [ ] **Finished incidents:** no further `POST …/action`.
- [ ] Track **local in-flight** submissions if the API doesn’t immediately show them on GET (optimistic pending until detail confirms).

## 8. Executor

- [ ] `POST …/incidents/{id}/action` with `{ "action_id": "…" }` (and optional `notes` only if required).
- [ ] On **rejection** (4xx): refresh detail, **back off**, **don’t** spam; adjust plan (e.g. dependency race, capacity, finished).
- [ ] On success: refresh detail on next cycle (or immediately if rate gate allows and TTL is tight).

## 9. Main polling loop

- [ ] Single loop: compute **next wake time** = min(poll incidents, poll session, per-incident detail slots, catalog refresh if any).
- [ ] Each iteration: refresh session state → refresh catalog if stale or needed → list incidents → **reconcile** open incidents (detail → plan → execute).
- [ ] Exit when session is **complete** / `GET …/summary` succeeds (or documented terminal condition).
- [ ] Print or save **summary** once at the end.

## 10. Docs & handoff (short README)

- [ ] **How to run** (env vars, example command).
- [ ] **Assumptions** (JSON field names after first fetch; behavior if catalog mid-run — for polling-only MVP, note “refetch catalog periodically or on failure” until SSE).
- [ ] **Tradeoffs** (polling latency; known limitations).

---

## Stretch goal: SSE wakeup (after polling MVP is stable)

- [ ] Background task: `GET /sessions/{id}/stream` with `Accept: text/event-stream`; parse **event `event:` lines** (ignore data-only if insufficient).
- [ ] On `incident_*`, `action_*`, `catalog_updated`, `session_finished`: enqueue a **wake** (coalesce duplicates within a short window to avoid storms).
- [ ] Main loop waits on **`next_scheduled_poll` vs wake**, then calls the **same `reconcile()`** as polling.
- [ ] **SSE delay handling:** if event fires but GET list/detail doesn’t show the change yet, **retry with backoff** within rate limits.
- [ ] **Reconnect** on disconnect: exponential backoff, cap; never block HTTP rate gate indefinitely.
- [ ] Flag: `--no-sse` to force polling-only for debugging.

---

## Definition of done (MVP)

- [ ] Process runs **unattended** from start through **successful summary** on at least the **practice** scenario.
- [ ] No systematic violations of **serial/parallel** rules; stable under **5s** REST limits.
- [ ] Incidents are **resolved before expiry** on the scenarios you tested, or failures are clearly logged with cause.
