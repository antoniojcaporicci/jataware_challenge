# jataware_challenge

## Prod Alerts Detection

This repo holds a **long-running worker** that detects incidents as they appear in the nightwatch.jata.lol service. It relies on a (somewhat flaky) catalog to decide what to do next, and resolves those incidents before they expire.

- **Lifecycle** — creates or reuses a session, starts it when permitted, and runs until the session completes or is stopped.
- **Incident detection (today)** — **polling**: bulk **`GET …/incidents`** on a rate limit–aware schedule, plus targeted **`GET …/incidents/{id}`** when the list row is not enough to plan. **SSE** to **`GET …/stream`** is the next extension (same reconcile path; see [Next steps: SSE](#next-steps-sse-on-top-of-polling)).
- **Catalog** — loads the session catalog on demand, retries through short failures, and **re-applies** playbooks when the catalog changes mid-run.
- **Action execution** — for each open incident, decides what to run next from the playbook and global action definitions: **honors dependencies**, never overlaps **serial** work illegally, caps **parallel** work at what the API allows, and **backs off** when actions are rejected. Polling respects **per-endpoint rate limits** so refreshes stay under the documented ceiling.
- **Outcome** — when the run ends, fetches the session **summary** (for logs or a final artifact).

Implementation details and endpoint specifics live in the **API reference** section below and in source once added.

## Conventional commits

This repo uses **[Conventional Commits 1.0.0](https://www.conventionalcommits.org/)**.

Examples:

- `feat: handle catalog_updated SSE and refresh playbooks`
- `fix: throttle GET incident polling to respect per-endpoint limits`
- `docs: clarify SSE vs HTTP truth in README`

Rules of thumb: imperative subject (`add`, `fix`), one logical change per commit, optional scope (`feat(sse): …`).

## Running tests

From the repository root, with **Python 3** (stdlib `unittest` only; no extra packages or API credentials required):

```bash
python3 -m unittest discover -s tests -v
```

To run a single module, for example:

```bash
python3 -m unittest tests.test_http_client -v
python3 -m unittest tests.test_worker_cli -v
python3 -m unittest tests.test_session_lifecycle -v
```

## Running the worker (MVP)

The worker takes a **session id** (`SESSION_ID` or `--session-id`). On startup it **`POST /sessions/{id}/start`** once (unless `--skip-session-start`), then polls **`GET /sessions/{id}`** until **running**. If that GET shows **finished**, it **`POST /sessions`** to create a **new** session by default (same defaults as `challenge_http_cli session-create`; override with `--session-mode` / `--scenario-type`), updates **`SESSION_ID` in the env file** when one was loaded, **`POST …/start`** on the new id, and polls again. **`--exit-if-session-finished`** exits successfully instead of creating a session. If start returns **403**, start the session elsewhere—the worker keeps polling until GET shows an active state.

After loading `secrets.env` (or exporting `API_URL` / `API_TOKEN` yourself), it runs **`GET /auth/verify`** on startup (skip with `--skip-verify`). Use **`--assume-session-active`** to skip both start and session polling (for example in tests without a reachable API).

In the main loop, **`reconcile_tick`** refreshes the catalog (unless startup skipped it), **`GET`s bulk incidents** on the per-endpoint rate gate, keeps **open** rows sorted by **soonest expiry first**, and issues **at most one** targeted **`GET …/incidents/{id}`** per tick when list fields are insufficient for planning. **`--skip-incident-polling`** disables list/detail polling (e.g. offline tests). See `python3 -m nightwatch_worker --help` for all flags.

```bash
set -a && source secrets.env && set +a
python3 -m nightwatch_worker
```

### Troubleshooting: no `submitted action` logs

Successful submits log a line like: `submitted action incident_id=… action_id=… http_status=…`. If you never see that, work through this list:

1. **Catalog parsed** — After `catalog refreshed`, counts should be non-zero when the API returns types/actions (e.g. `catalog refreshed (1 types, 2 actions)`). If you see **`(0 types, 0 actions)`** while the session has incidents, the catalog JSON shape may differ from what `nightwatch_worker/catalog.py` expects: capture **`GET /sessions/{id}/catalog`** and extend the parser.
2. **Playbook matches incident type** — Planning needs a playbook for the incident’s `incident_type`. Unknown or renamed types, or IDs like `cache_drift_2` when the catalog only defines `cache_drift`, are handled via **`CatalogSnapshot.playbook()`** fallbacks; if the server uses another naming pattern, extend that lookup or the parser’s type keys.
3. **`detail_truth` / planning** — Eligible actions require trustworthy action state (`to_planning_state`). If list rows omit completed/in-flight/accepts fields, the worker fetches **detail** first; ensure detail returns those fields so `detail_truth` becomes true.
4. **`accepts_actions` / finished** — If the API reports the incident is not accepting actions or is finished, the executor will not POST.
5. **Backoff after rejections** — A recent **4xx** on `POST …/action` sets per-incident backoff; watch for **`action rejected`** / **`POST action`** warnings.
6. **Rate limits** — **429** on incident GETs can delay fresh state; the client uses a **shared incident bucket** and **GET 429 retries**. **`--max-action-posts-per-tick`** caps POSTs per tick (default `1`).

For offline verification, run **`python3 -m unittest discover -s tests -v`** (includes catalog and reconcile/action tests).

### Next steps: SSE on top of polling

The worker already exposes **`_WakeCoordinator.notify_check()`** for a second producer. Planned work:

1. **Background thread** — `GET /sessions/{id}/stream` with `Accept: text/event-stream`; parse **`event:`** lines (and `data:` if needed).
2. **Wake the main loop** — On `incident_*`, `action_*`, `catalog_updated`, `session_finished`, call **`notify_check()`** (same **`reconcile_tick`** path as the timer). **Coalesce** duplicate events in a short window to avoid storms.
3. **Timing** — After an SSE wake, still treat **HTTP as source of truth**; if list/detail lags the event, **retry with backoff** within the incident rate gate.
4. **Resilience** — **Reconnect** the stream on disconnect (exponential backoff, capped); do not starve the shared incident HTTP budget.
5. **CLI** — e.g. **`--no-sse`** for polling-only debugging; optional **`--sse-reconnect-max-sec`** tuning.

See `execution_plan.md` (stretch goal section and **Next steps**) for the checklist aligned with implementation.

## Connecting to the API (secrets, not in git)

### 1. Configure environment

**Do not put tokens in the README, in source code, or in any committed file.**

1. Copy the example file and fill in real values **locally**:

   ```bash
   cp secrets.env.example secrets.env
   ```

2. Edit `secrets.env` and set at least:

   - `API_URL` — base URL of the API (no trailing slash is typical; your client should normalize).
   - `API_TOKEN` — bearer token issued for the run.

3. Load them in your shell before running the service:

   ```bash
   set -a
   source secrets.env
   set +a
   ```

   Or use your language’s dotenv loader **only in local/dev** — still keep secrets out of git.

### 2. Verify authentication

```bash
curl -sS "$API_URL/auth/verify" \
  -H "Authorization: Bearer $API_TOKEN"
```

Expected shape includes JSON with at least `"api_version": "v2"` in responses (plus verify-specific fields from the server).

### 3. Session lifecycle (setup phase)

Typical flow (details in API reference below):

1. `POST /sessions` — create a session.
2. `POST /sessions/{session_id}/start` — start when the token permits; if this returns forbidden, the session must be started outside this worker.
3. Poll `GET /sessions/{session_id}` until the simulation is active if needed.
4. Run the main loop until `GET /sessions/{session_id}/summary` (or SSE `session_finished`) indicates completion.

---

## API reference

### System rules and limitations

- **HTTP is the source of truth** — SSE is a **live wake-up channel**, not a second state model.
- **SSE vs REST timing** — there can be **delay** between an SSE event and the same incident being visible on the incidents API.
- **Catalog** — may be **temporarily unavailable**; may **change during a run**.
- **Incident API rate limit** — **one request every 5 seconds per session and per endpoint** (design polling and refreshes around this).
- **Incident TTL** — starts when the incident **appears**; the service must drive resolution before expiry.
- **When an action is runnable** — all **dependencies** satisfied, **no conflicting serial work** in flight, incident still has **capacity**, incident **not finished**.
- **Serial actions** — must run **alone** (no overlapping serial work per the API rules).
- **Parallelism** — at most **two parallel actions per incident** at once.
- **Finished incidents** — reject further actions.

### Authentication

All requests:

```http
Authorization: Bearer <token>
```

JSON bodies/responses include (at minimum) an API version field:

```json
{
  "api_version": "v2"
}
```

### Process view: polling baseline

While the run is active, loop roughly:

| Step | Endpoint |
|------|----------|
| Auth check | `GET /auth/verify` |
| Create session | `POST /sessions` |
| Start | `POST /sessions/{session_id}/start` |
| Catalog | `GET /sessions/{session_id}/catalog` |
| Incidents list | `GET /sessions/{session_id}/incidents` |
| Incident detail | `GET /sessions/{session_id}/incidents/{incident_id}` |
| Submit action | `POST /sessions/{session_id}/incidents/{incident_id}/action` |
| Outcome | `GET /sessions/{session_id}/summary` |

Respect the **5-second per-endpoint** limit when polling.

### Process view: SSE (preferred wake-up)

Subscribe to:

| Endpoint | Notes |
|----------|--------|
| `GET /sessions/{session_id}/stream` | SSE |

Example event names (wake-ups; always confirm state over HTTP):

- `incident_started`
- `action_completed`
- `action_failed`
- `incident_resolved`
- `incident_expired`
- `catalog_updated`
- `session_finished`

Core work loop still uses HTTP: e.g. **`GET …/incidents/{incident_id}`** and **`POST …/action`**, and **`GET …/catalog`** when the catalog may have changed.

### Endpoints (inventory)

| Method | Path |
|--------|------|
| `GET` | `/auth/verify` |
| `POST` | `/sessions` |
| `GET` | `/sessions/{session_id}` |
| `POST` | `/sessions/{session_id}/start` |
| `POST` | `/sessions/{session_id}/stop` |
| `GET` | `/sessions/{session_id}/catalog` — global action definitions plus dependency-only playbooks |
| `GET` | `/sessions/{session_id}/stream` — SSE |
| `GET` | `/sessions/{session_id}/incidents` |
| `GET` | `/sessions/{session_id}/incidents/{incident_id}` |
| `GET` | `/sessions/{session_id}/incidents/{incident_id}/events` — simplified event history (action lifecycle / state) |
| `POST` | `/sessions/{session_id}/incidents/{incident_id}/action` |
| `GET` | `/sessions/{session_id}/summary` |

### Summary / report

```bash
curl -sS "$API_URL/sessions/$SESSION_ID/summary" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Accept: text/markdown"
```

(JSON is also available with an appropriate `Accept` header per server behavior.)

---

## Security note

Bearer tokens are secrets. **Never commit `secrets.env` or `.env` with real values.** If a token is exposed (e.g. in chat or a screenshot), **assume it is compromised** and rotate it with whoever issued it.
