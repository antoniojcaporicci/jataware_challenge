# jataware_challenge

## Prod Alerts Detection

This repo holds a **long-running worker** that detects incidents as they appear in the nightwatch.jata.lol service. It relies on a (somewhat flaky) catalog to decide what to do next, and resolves those incidents before they expire.

- **Lifecycle** — creates or reuses a session, starts it when permitted, and runs until the session completes or is stopped.
- **Incident detection** — keeps an **SSE** connection to the session stream for low-latency notifications, while using **HTTP as the source of truth** for incident and catalog state (including after SSE wake-ups).
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
```

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
