# shortener

A URL shortener service. It takes a long URL, returns a short code that redirects
to it, and records each redirect so the link's owner can see how often and when it
was visited. It also carries the reliability features a public redirect path needs:
a health check, per-caller rate limiting, optional per-link expiry, and click
recording that cannot slow down or break a redirect.

It is a prototype: single instance, single SQLite file, no authentication. See
[Known limits](#known-limits) before pointing anything real at it.

## Setup

From a fresh checkout, in the repository root. Python 3.13 or newer is required;
substitute whatever name your system gives that interpreter for `python3.13`.

```bash
python3.13 --version    # must report 3.13 or newer
python3.13 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install "fastapi>=0.115" "uvicorn[standard]>=0.32" "sqlalchemy>=2.0" "pydantic>=2.9" "pydantic-settings>=2.6" "httpx>=0.28" "pytest>=8.3" "pytest-asyncio>=0.24"
```

Run the service. It must be started from `target/`, because the `shortener`
package is not installed — it is imported from the working directory.

```bash
cd target
../.venv/bin/uvicorn shortener.main:app --host 127.0.0.1 --port 8000
```

The datastore is created on first start. Check it is up:

```bash
curl -i http://127.0.0.1:8000/health
```

Run the acceptance suite (from the repository root, with the service stopped —
the tests start their own app against their own temporary database):

```bash
.venv/bin/pytest target/tests
```

These commands were last run end to end in an empty virtual environment on
Python 3.13 with nothing preinstalled: the service started, `/health` returned
`{"status":"ok","datastore":true}`, and the suite reported 86 passed.

### Configuration

Every setting is read from an environment variable with the `SHORTENER_` prefix.
Defaults are sized for one region, ~10k links/day and peak 100 redirects/second.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHORTENER_DATABASE_URL` | `sqlite:///./shortener.sqlite3` | SQLAlchemy URL for the datastore |
| `SHORTENER_BASE_URL` | `http://localhost:8000` | Origin short URLs are built from, and the host creation refuses to point at |
| `SHORTENER_RATE_LIMIT_REQUESTS` | `120` | Requests allowed per caller per window |
| `SHORTENER_RATE_LIMIT_WINDOW_SECONDS` | `60.0` | Length of the rate-limit window |
| `SHORTENER_ANALYTICS_QUEUE_MAXSIZE` | `10000` | Bound on the in-process click queue |
| `SHORTENER_ANALYTICS_FLUSH_INTERVAL_SECONDS` | `1.0` | How often the queue drains to the datastore |
| `SHORTENER_ANALYTICS_BATCH_SIZE` | `200` | Clicks written per drain batch |
| `SHORTENER_BLOCKED_HOSTS` | empty | Extra hosts creation refuses, beyond the service's own |

If you change `SHORTENER_BASE_URL`, short URLs in responses change with it; the
stored long URLs do not.

## API

The service exposes exactly six endpoints:

```
POST /api/links
GET /api/links/{code}
DELETE /api/links/{code}
GET /api/links/{code}/analytics
GET /health
GET /{code}
```

| Endpoint | Success | Purpose |
| --- | --- | --- |
| `POST /api/links` | 201 | Create a short link |
| `GET /api/links/{code}` | 200 | Link metadata and total clicks |
| `DELETE /api/links/{code}` | 204 | Retire a code |
| `GET /api/links/{code}/analytics` | 200 | Clicks over a window, with breakdowns |
| `GET /health` | 200 | Datastore reachability |
| `GET /{code}` | 301 | The redirect path |

All request and response bodies are JSON. All timestamps are ISO 8601 with a UTC
offset.

### POST /api/links

Create a short link.

```json
{ "url": "https://example.com/some/long/path?a=1", "expires_at": null }
```

`url` must be an absolute `http` or `https` URL. `expires_at` is optional; there
is no service-wide default expiry.

`201 Created`:

```json
{
  "code": "aB3xY7z",
  "short_url": "http://localhost:8000/aB3xY7z",
  "long_url": "https://example.com/some/long/path?a=1",
  "created_at": "2026-08-02T10:00:00+00:00",
  "expires_at": null
}
```

`400 invalid_url` if the URL is missing, relative, not `http`/`https`, or points
at this service's own host.

Submitting the same long URL twice mints two different codes. Deduplicating would
merge the analytics of two campaigns that meant to track separately.

### GET /{code}

The redirect path. Answers `301 Moved Permanently` with `Location` set to the
stored long URL, byte for byte — nothing normalises the URL, so trailing slashes,
query order and percent-encoding survive intact.

`404 not_found` if the code was never issued. `410 gone` if it was issued and has
since been deleted or expired.

The click is queued for recording after the redirect is decided; a failure in
recording is logged and never turned into an error response.

### GET /api/links/{code}

Link metadata, including for codes that have been deleted or expired.

`200 OK`:

```json
{
  "code": "aB3xY7z",
  "short_url": "http://localhost:8000/aB3xY7z",
  "long_url": "https://example.com/some/long/path?a=1",
  "created_at": "2026-08-02T10:00:00+00:00",
  "expires_at": null,
  "deleted_at": null,
  "total_clicks": 42
}
```

`404 not_found` if the code was never issued. State is carried on the fields:
`deleted_at` non-null means retired, `expires_at` in the past means expired.

### DELETE /api/links/{code}

Retire a code. `204 No Content`, with no body.

Idempotent: deleting an already-deleted code still answers `204`. `404 not_found`
only if the code was never issued.

The delete is soft. The row survives with `deleted_at` set, the code is never
reissued, redirects answer `410`, and the link's analytics stay readable — reusing
a code would attribute new clicks to an old link's history.

### GET /api/links/{code}/analytics

Click counts for a link over a window.

Query parameters:

| Parameter | Required | Meaning |
| --- | --- | --- |
| `start` | yes | Window start, inclusive |
| `end` | yes | Window end, exclusive |
| `interval` | no | Bucket width: `hour` or `day` (default `day`) |

The window is half-open, `[start, end)`, so adjacent windows neither overlap nor
drop a click. `200 OK`:

```json
{
  "code": "aB3xY7z",
  "total_clicks": 42,
  "start": "2026-07-01T00:00:00+00:00",
  "end": "2026-08-01T00:00:00+00:00",
  "interval": "day",
  "series": [ { "start": "2026-07-01T00:00:00+00:00", "count": 3 } ],
  "referrers": [ { "value": "https://news.example.com/", "count": 20 } ],
  "devices": [ { "value": "mobile", "count": 25 } ],
  "browsers": [ { "value": "chrome", "count": 30 } ]
}
```

Everything is scoped to the requested window, so `series` sums to `total_clicks`.

`400 invalid_query` if `interval` is not `hour` or `day`, or if the window would
produce more than 366 buckets. `404 not_found` if the code was never issued, with
no analytics data disclosed.

Breakdowns return the top 100 values by count plus one `other` row summing the
rest. Both bounds exist so a query over a multi-year window cannot become a load
source the service points at itself.

### GET /health

`200 OK` while the datastore the redirect path needs is reachable:

```json
{ "status": "ok", "datastore": true }
```

`503 storage_unavailable` otherwise, so a load balancer stops sending traffic to
an instance that cannot redirect.

Health deliberately does *not* check the analytics recorder. A failing recorder is
a healthy state for this service: redirects still work.

## Errors

Every non-2xx response uses one envelope, so a client parses one shape:

```json
{ "code": "invalid_url", "message": "url must be an absolute http or https URL", "details": null }
```

| `code` | HTTP | Cause |
| --- | --- | --- |
| `invalid_url` | 400 | Missing, relative, non-http(s), or self-referential URL |
| `invalid_request` | 400 | The request body or query failed schema validation |
| `invalid_query` | 400 | An analytics window or interval the API refuses to serve |
| `not_found` | 404 | The short code was never issued |
| `gone` | 410 | The code was issued and is deleted or expired |
| `rate_limited` | 429 | Caller's request budget is spent; `Retry-After` header set |
| `code_collision` | 500 | Code minting exhausted its retries |
| `storage_unavailable` | 503 | The datastore is unreachable |
| `analytics_unavailable` | 503 | The analytics store is unreachable |

`analytics_unavailable` never appears on the redirect path — there it is logged and
swallowed.

## Behaviour worth knowing before you rely on it

**Redirects are 301, not 302.** A short link is a permanent identifier, and letting
browsers and intermediaries cache it is the point. The cost is real and is not
worked around: a cached redirect never reaches the service, so **click counts
under-report actual traffic**, and a deleted link stays followable for any client
that already cached it. If you need exact counts, this is the decision to revisit.

**Short codes are 7 random base62 characters** (about 3.5×10¹² of them). Random
rather than sequential so the link namespace is not enumerable. Codes are minted
optimistically and retried against the datastore's primary key until a small
attempt budget is spent; uniqueness is the database's invariant, not a
read-then-write that can race.

**Analytics are eventually consistent.** Clicks go through a bounded in-process
queue drained in batches, and are guaranteed visible within **60 seconds**. This is
what keeps redirect latency inside its budget and keeps redirects working when the
analytics store is not. Two consequences: a count read immediately after a click
may be low, and clicks still queued when the process dies are lost. The queue is
bounded on purpose — an unbounded one trades a latency budget for memory it never
returns.

**Rate limiting is per client IP, in process.** 120 requests per 60 seconds by
default; over budget gets `429` with `Retry-After`. Other callers are unaffected.
In-process because a shared limiter would add a second dependency to the redirect
path. The cost: run two instances and each grants a full budget.

**Analytics record referrer and user-agent only.** No IP storage, no geolocation,
no unique-visitor deduplication — which keeps the service clear of personal-data
retention obligations. A click is any successful redirect, crawlers included; the
browser and device breakdowns are how you see crawler traffic separately.

## Known limits

- **No authentication and no multi-tenancy.** Every endpoint is anonymous,
  including delete and analytics. Anyone who guesses a code can delete the link or
  read its analytics. This is prototype scope, and it is the first thing to fix
  before public exposure.
- **No custom aliases.** Codes are always generated.
- **No URL reputation screening.** Creation rejects only URLs pointing at this
  service's own host, plus whatever `SHORTENER_BLOCKED_HOSTS` lists. Malware and
  phishing screening is out of scope.
- **Single instance, single datastore.** No sharding, no replication, no cache
  tier. Rate-limit budgets and the click queue are per process.
- **Expired links are not swept.** An expired row stays in the datastore and is
  detected at read time.
