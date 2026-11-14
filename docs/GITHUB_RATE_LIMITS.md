# GitHub API rate limits

The app talks to GitHub through the server user's `gh` login (REST, GraphQL and `gh pr …`). All of those calls go through one
place, `backend/app/gh_limits.py`, which keeps them within GitHub's limits, waits and resumes by itself, and shows what
it is doing. Long-running GitHub work (Repo Insights reports and runs) is recorded in a durable queue
(`backend/app/gh_queue.py`) and continues after a restart.

## Architecture and integration points

| Caller | Calls | Priority |
|---|---|---|
| `github_hub._run_gh` / `gh_graphql` / `gh_rest` / `gh_job_log` / `_gh_files` | PR Review: the queue, PR tabs, comments, reviews, CI logs, people | interactive (your page); mutations critical |
| `github_cache` revalidate | the check right after a cached PR tab opens | interactive, *no waiting* (the cached copy stays) |
| `github_cache` prefetch | PRs of the review list made ready to open | background, *no waiting* (stops; the next list refresh queues again) |
| `review_priority` | PR details for the priority agent | background |
| `insights` (reports, Make, Score now, the daily scheduler) | issues / PRs / RFC searches, PR diffs | background (waits for the quota) |
| `development.service` | `gh pr view / list / close / reopen / create / checks` | interactive; close / reopen / create critical |
| `commits` (wiki Commit panel) | `gh pr create` | critical |
| `social_login`, `github_hub.gh_owner` | `gh api user` (who the server's login is; cached 10 min) | interactive |

**Explicit exceptions** (not managed): `scripts/doctor.sh` / `backend/app/doctor.py` (one `gh api user` when you run the
check); `gh auth status` in the wiki Commit panel (now cached 5 min); `git fetch` / `git clone` over HTTPS (not API calls);
`gh` commands the agents (OpenCode, Codex) run themselves inside their sessions (outside the app; they share the quota, which
the app sees in GitHub's numbers on its next call); PR Development targets on another host (ssh) run `gh` with that host's
login, but are still counted against this server's identity (conservative).

## How a call goes

1. **Priority** comes from the caller's context: *critical* (your mutations: submit a review, post a comment, close a PR,
   create a PR), *interactive* (a page you wait for), *background* (prefetch, priority agent, Insights, scheduler).
2. **Before the call** (`acquire`) the limiter looks at the bucket of the call's resource (`core`, `graphql`, `search`, …) for
   the server's GitHub identity:

   | Remaining | Background | Interactive | Critical |
   |---|---|---|---|
   | ≥ `warn` (20%) | runs | runs | runs |
   | < `warn` | spread out evenly until the reset (at most `max_gap` 30 s apart) | runs | runs |
   | < `suspend` (5%) | waits for the reset | runs | runs |
   | ≤ `reserve` (2%, at least 20) | waits | waits if the reset is ≤ `max_interactive_wait` (90 s), else **429 GH_RATE_LIMITED** with the reset time | runs |
   | 0 | waits | as above | as above |

   Mutations are also spaced ≥ `mutation_gap` (1 s) apart (GitHub's advice against secondary limits). A secondary limit
   blocks the resource for everyone until it ends.
3. **The call** runs `gh api -i …`, so every response (each page of a paginated call) brings GitHub's
   `X-RateLimit-Limit / Remaining / Used / Reset / Resource`, `Retry-After` and `Date` headers. They are authoritative: the
   bucket is set from them (the limit is never assumed to be 5000). The reset time is moved onto this host's clock using the
   difference between GitHub's `Date` and the local time (this server was 3.5 minutes behind GitHub when this was written).
   Calls without headers (`gh pr …`, CI logs) are counted as estimates; an estimate older than `refresh_after` (120 s) is
   checked with `gh api rate_limit` (free) before the next call.
4. **A failed answer** is classified from gh's error and GitHub's error object (never from the data, so a PR titled
   "rate limit" is not a limit):
   - *primary* (`API rate limit exceeded`, GraphQL `RATE_LIMITED`, 403/429 with `remaining: 0`): background work waits
     until the reset plus `margin` (3 s), then retries; a person gets a 429 at once unless GitHub's stated reset is
     ≤ 90 s away.
   - *secondary* (`secondary rate limit`, 429, `Retry-After`): `Retry-After` when given, else ≥ `secondary_min` (60 s)
     doubling per repeat up to `backoff_cap` (15 min); a person waits only for a short stated `Retry-After`.
   - *auth* (401, bad credentials, not logged in): no retry; state FAILED ("run `gh auth login`").
   - *transient* (5xx, time-outs, network): reads retried 1× (interactive) / 3× (background) with backoff.
   - anything else: returned to the caller as before.

   All waits get up to `jitter` (20%) extra so retries do not line up; at most `max_retries` (4) retries — never a loop.
5. **Mutations**: a rate-limited answer means GitHub rejected the call (nothing was done), so it may be sent again. An
   unclear outcome (time-out, 5xx, network) is never sent again blindly: the caller's *reconcile* check asks GitHub
   whether it happened (`gh pr create`: is there an open PR for the branch?; close / reopen: is the PR in that state?). If it
   did, that result is used; if it cannot be told, the call fails with `GH_UNCERTAIN` ("check on GitHub before trying
   again"). `gh pr create` answering "already exists" adopts the existing PR — no second PR.
6. **Identical reads in flight** (same `gh` arguments) run once; the other callers get the same answer.

## Sharing the quota

The buckets and secondary-limit blocks are written to `<state_dir>/github-rate.json` (under a file lock) and read before each
call, so all app processes of a data folder — the server and its background jobs, several servers on the same folder —
spend one quota. Jobs inside one server share the in-process limiter. Processes with other data folders still coordinate
through GitHub's own numbers (each sees what the others spent in the headers of its next answer).

## Pause, resume and restarts

- A waiting call blocks only its own work: one Insights job waits while the rest of the app (local analysis, wiki, terminals,
  agents, other jobs) goes on.
- Repo Insights reports and *Make* / *Score now* runs are recorded in the durable queue (kv `github_pending_ops`) with their
  parameters and progress (the days done). At start-up the server resumes them by itself; finished building blocks (daily
  summaries, week / month blocks, the report id) are reused, so nothing is done twice and nothing is posted to GitHub (these
  jobs only read). An operation is resumed at most 5 times, then dropped as failed.
- The daily scheduler catches up missed days on its own (up to a week).
- The prefetch queue and the review list are not kept: they are rebuilt from the next list refresh.
- PR files are fetched again only when the PR's head commit changed (a memo, seeded from the on-disk PR cache after a
  restart) — before, every PR open re-paged the files over REST.

## Status and monitoring

- `GET /api/v1/github/rate` (any signed-in user): `state` — `RUNNING`, `THROTTLED` (below `warn`, or background spread out),
  `WAITING_FOR_RATE_LIMIT` (something waits for a reset or a secondary limit), `RESUMING` (just went on), `FAILED`
  (the gh login is broken) — with the buckets (limit, remaining, reset), the GitHub identity, the clock skew, what waits and
  when it goes on (`next_retry_at`), the queued jobs, and metrics: requests per resource and per operation
  (`top_consumers`), throttled / waited / rate-limited counts, retries, seconds blocked, deduplicated reads, cache hit rate.
- In the app: a chip in the Reviews list and on the Repo Insights page when the state is not RUNNING (hover: what is left,
  what waits, queued jobs); an Insights job's progress says "waiting for GitHub: … goes on by itself at HH:MM (UTC+8)".
- The server log (`waw.github.rate`): one line per call — operation, endpoint (no tokens, no headers), identity (login),
  resource, priority, status, latency, remaining, reset; a warning for each wait and retry.
- Slack: the app has no Slack channel configured, so transitions are shown in the app and the log only.

## Configuration

Defaults suit a 5000/h login. Override in `config.toml`:

```toml
[github.rate]
warn = 0.20                  # background requests spread out below this share of a quota
suspend = 0.05               # background requests wait for the reset below this share
reserve = 0.02               # kept for critical operations (your mutations) …
reserve_min = 20             # … at least this many requests
max_interactive_wait = 90    # seconds a person may wait for a reset before getting a 429
max_background_wait = 3900   # seconds background work may wait
max_retries = 4
secondary_min = 60           # seconds, a secondary limit without Retry-After
backoff_cap = 900            # seconds
mutation_gap = 1.0           # seconds between mutations
```

## Troubleshooting

- *"GitHub API limit: … it can go on at HH:MM"* on a page: the quota is at the reserve; your own actions still work, the
  page works again at that time (or sooner when other work stops). `gh api rate_limit` shows the numbers.
- Chip *GitHub: not working*: run `gh auth status` / `gh auth login` as the server user.
- Many calls by one operation: `GET /api/v1/github/rate` → `metrics.top_consumers`.
- An Insights report says *interrupted*: it was not in the queue (made before this version) — make it again.

## Known limitations

- `gh` CLI calls by agents inside their sessions are not throttled by the app.
- Webhooks are not used (the server has no public address); the review list and PR tabs are refreshed by polling, kept low
  by the caches (review list 120 s, PR tab 60 s, on-disk PR cache, the files memo).
- ETag / conditional requests are not used: `gh` handles pagination, and the head-commit memo already avoids the
  repeated file listings that dominated; GraphQL has no conditional requests.
- User-initiated mutations are not queued for later: when the quota is gone, they still have the reserve; if even that is
  gone, the page says when to try again (review drafts stay saved locally).
