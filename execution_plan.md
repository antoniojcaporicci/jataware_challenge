# Execution plan: incident worker MVP

Polling baseline first; SSE as a stretch goal. Check boxes as you complete items.

## Ground rules

- [ ] Treat **HTTP GET responses** as source of truth for catalog, incidents, and incident detail.
- [ ] Enforce **≥5s between calls** to each rate-limited incident endpoint (per session), and design the loop so you **don’t starve** incidents close to expiry.
- [ ] One **shared `reconcile()`** (or `tick()`) path that both polling and (later) SSE will call.

### Critical risks (don’t trap the worker)

- **Catalog 5xx:** On catalog fetch failure, **keep using the last good cached catalog** so existing incidents still resolve. **Do not** block the whole loop waiting for catalog; only **withhold action on new incident types** that cannot be mapped without a fresh catalog.
- **Detail bottleneck:** Many open incidents × one-detail-every-5s **does not** scale as a primary refresh path. Prefer **bulk incident list** and **SSE** for fleet state; use **`GET …/incidents/{id}` sparingly** for incidents that are stuck, ambiguous, or mid-transition.
- **Action concurrency vs API capacity:** Parallelism limits must match **actual API** behavior; if throughput is low, a **global** max-concurrent-actions semaphore (not only per-incident) may be required to avoid **429/503**.
- **Zombie session:** If session status stays **running** but **no** stream events **and** **no** incident activity for **N** minutes, **alert and exit** (dead man’s switch) instead of idling forever.

## 1. Project skeleton

- [ ] Add a small entrypoint (e.g. `python -m nightwatch_worker` or `worker.py`) that loads `API_URL` / `API_TOKEN` / `SESSION_ID` (reuse `secrets.env` pattern from `challenge_http_cli.py` if helpful).
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
- [ ] On fetch failure: **back off** and retry **without blocking reconciliation** for incidents already mappable from cache; **continue using stale cache** for those. Only **delay or skip** planning for incident types that need a fresh catalog to map safely.
- [ ] Parse enough structure to map: **playbook steps**, **action metadata** (at least: `action_id`, **dependencies**, **serial vs parallel** per API shape — adjust after first real response).
- [ ] **Planner hook:** given catalog + incident state, compute **eligible next actions**.

## 5. Incidents (discovery)

- [ ] Poll `GET /sessions/{id}/incidents` on the rate-limited schedule.
- [ ] Maintain a set/list of **open** incidents (skip resolved/expired/finished per API fields).
- [ ] **TTL-aware ordering:** prioritize incidents with **least time until expiry** for detail refresh and action attempts.

## 6. Incident detail & state

- [ ] **Primary fleet state:** lean on **`GET …/sessions/{id}/incidents`** (bulk list) and, when enabled, **SSE** for timely updates — **not** per-incident detail for every open row.
- [ ] **Detail endpoint (targeted):** `GET …/incidents/{incident_id}` only where the list/stream is insufficient — e.g. **stuck**, **transitioning**, or **ambiguous**; stagger when many ids compete for the same endpoint budget.
- [ ] Derive: **completed / failed actions**, **whether incident accepts actions**, **current in-flight** if exposed in JSON.
- [ ] If detail is ambiguous, optional follow-up: `GET …/incidents/{id}/events` (only if needed — don’t build until detail proves insufficient).

## 7. Action rules (core correctness)

- [ ] **Dependencies:** never submit an action until prerequisites are satisfied (playbook order / deps).
- [ ] **Serial:** at most one serial action “in flight” per incident; no overlap violating API rules.
- [ ] **Parallel / throughput:** cap concurrent actions per **API-documented or observed** limits (e.g. **two parallel per incident** if that’s the rule). If the API is **low-throughput**, add a **global** `max concurrent POST …/action` (semaphore) so many incidents don’t trip **429/503** — tune from real behavior, not only a per-incident count.
- [ ] **Finished incidents:** no further `POST …/action`.
- [ ] Track **local in-flight** submissions if the API doesn’t immediately show them on GET (optimistic pending until detail confirms).

## 8. Executor

- [ ] `POST …/incidents/{id}/action` with `{ "action_id": "…" }` (and optional `notes` only if required).
- [ ] On **rejection** (4xx): refresh detail, **back off**, **don’t** spam; adjust plan (e.g. dependency race, capacity, finished).
- [ ] On success: refresh detail on next cycle (or immediately if rate gate allows and TTL is tight).

## 9. Main polling loop

- [ ] **Wake coordination:** use a **thread-safe queue** and/or **condition variable** so **one** path computes sleep until `min(next_scheduled_poll, …)` and **both** the timer and SSE can signal “run a check.” Avoid ad-hoc sleeps that race with stream wakeups.
- [ ] **Polling MVP:** roughly every **5s** (or your compliant interval), enqueue a **`Check`** (or wake the waiter) so **reconcile** runs on a steady baseline even without SSE.
- [ ] Single logical loop: compute **next wake time** = min(poll incidents, poll session, per-incident **targeted** detail slots, catalog refresh if any); wait until that time **or** an external wake.
- [ ] Each iteration: refresh session state → refresh catalog if stale or needed → list incidents → **reconcile** open incidents (bulk/list (+ SSE) first; **targeted** detail → plan → execute).
- [ ] **Dead man’s switch:** if session remains **running** but there are **no** relevant SSE events **and** **no** incident activity for **N** minutes (tunable), **log/alert and exit** — don’t idle forever.
- [ ] Exit when session is **complete** / `GET …/summary` succeeds (or documented terminal condition).
- [ ] Print or save **summary** once at the end.

## 10. Docs & handoff (short README)

- [ ] **How to run** (env vars, example command).
- [ ] **Assumptions** (JSON field names after first fetch; **stale catalog on failure** continues for known mappings; new types may wait for refresh; SSE/bulk list vs detail tradeoff).
- [ ] **Tradeoffs** (polling latency; known limitations).

---

## Stretch goal: SSE wakeup (after polling MVP is stable)

- [ ] Background task: `GET /sessions/{id}/stream` with `Accept: text/event-stream`; parse **event `event:` lines** (ignore data-only if insufficient).
- [ ] On `incident_*`, `action_*`, `catalog_updated`, `session_finished`: enqueue the **same `Check` / wake** as the polling ticker (shared queue or `notify` on the condition). Coalesce duplicates within a short window to avoid storms.
- [ ] Main loop waits on **`next_scheduled_poll` vs wake**, then calls the **same `reconcile()`** as polling-only mode.
- [ ] **SSE delay handling:** if event fires but GET list/detail doesn’t show the change yet, **retry with backoff** within rate limits.
- [ ] **Reconnect** on disconnect: exponential backoff, cap; never block HTTP rate gate indefinitely.
- [ ] Flag: `--no-sse` to force polling-only for debugging.

---

## Definition of done (MVP)

- [ ] Process runs **unattended** from start through **successful summary** on at least the **practice** scenario.
- [ ] No systematic violations of **serial/parallel** rules; stable under **5s** REST limits.
- [ ] Incidents are **resolved before expiry** on the scenarios you tested, or failures are clearly logged with cause.
