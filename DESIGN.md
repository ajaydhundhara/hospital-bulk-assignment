# Design: Hospital Bulk Processing API

Status: **v0.3.0, submitted for the assignment** (2026-11-17). This document describes
the design as built, the patterns it uses, every decision taken (with the reasoning and
the measurements behind it), how far it can scale, and what is deliberately deferred.

Related docs: `README.md` (usage, endpoints, config), `MANUAL_TESTING.md` (scenarios).

**Reading guide:** §0 is the one-page summary. §2–§4 describe the system as it is; §5
lists every decision; §6–§7 assess it and lay out the scaling path; §8 keeps the full
discussion behind each decision as it happened, including the measurements of the real
upstream (§8.5).

---

## 0. Summary

**Problem.** Create up to 20 hospitals from a CSV through an upstream API as one batch,
then activate the batch. The upstream is the hard part: **~5.9 s per create** (measured),
**no idempotency key**, and it sleeps when idle (free Render instance).

**Key decisions**

| Decision | Why | Ref |
| --- | --- | --- |
| Send creates **10 at a time** through a process-wide semaphore | Parallel creates don't slow each other down (measured flat up to 20): 20 rows take ~12 s instead of ~2 min | D23, §8.5 |
| **Never blindly retry a create** that may have been applied (timeout, dropped connection, gateway 502/504); mark it *outcome unknown* | No idempotency key, so a blind retry can duplicate a hospital | D22, §8.3 |
| **Resume** the same batch: check unknown rows upstream (`GET /hospitals/batch/{id}`), re-send only what's missing | Recover from outages or restarts without duplicates or starting over | D21, §8.4 |
| **Save every row's state before and after each call** (JSON files behind a repository interface) | Progress is visible, a crash leaves exact state, resume is possible; swap to SQL later | D4, D6 |
| **Two-level validation**: file problems → 422; bad rows → `invalid`, the rest still processed | One bad row shouldn't block 19 good ones | D19, §8.1 |
| **Activate only when every row was created** (opt-in for partial); late `/activate` | Never publish a half batch by surprise | D2, D13 |
| Upstream owns business rules (duplicates, phone format) | Don't invent rules the upstream may contradict | D20, D24 |
| Safe defaults: hard clean off, wake upstream before sending | Public URL without auth; sleeping upstream | D25, D28 |

**Limits, on purpose.** Single process (in-memory locks, limiter and background tasks),
JSON-file storage, no auth. The path to many workers (DB-backed queue with leases on
Postgres) is in §7.3 and §8.7.

**Evidence.** A network-free test suite (`pytest`), live probes of the real upstream
(§8.5), and manual runs of outages, resume, the UI and Docker (§9).

---

## 1. Problem

Accept a CSV of hospitals (`name,address,phone`), create each one in the external
[Hospital Directory API](https://hospital-directory.onrender.com/docs) under a shared
`creation_batch_id`, then activate the batch, and report a per-row outcome.

Constraints that shape the design:

- The upstream has **no bulk-create endpoint**: one `POST /hospitals/` per row.
- The upstream is **slow on create**: measured 5.5–6.7 s per call, while reads take
  ~0.5 s. Parallel creates don't slow each other down (measured up to 20 at once, §8.5).
  It may also be unavailable or cold-starting.
- The upstream has **no idempotency key**: a retried `POST` can create a duplicate.
- Activation is **per batch** (`PATCH /hospitals/batch/{id}/activate`), not per row.
- Deletion is **per batch** (`DELETE /hospitals/batch/{id}`).

---

## 2. Architecture at a glance

```
            ┌───────────────────────────── FastAPI process ─────────────────────────────┐
 CSV ──►    │  main.py (HTTP layer, DI wiring)                                          │
 /ui  ──►   │     │                                                                     │
            │     ├─► csv_validation.parse_csv  ── pure: rows / invalid rows / file errors │
            │     │                                                                     │
            │     ├─► service.py  (orchestration: start_batch / run_batch / resume /    │
            │     │                activate_batch / hard_clean / build_result)          │
            │     │        │        run_batch: thread pool (N) + process-wide semaphore  │
            │     │        │                         │                                  │
            │     │        ▼                         ▼                                  │
            │     │   repository.JobRepository   client.HospitalDirectoryClient ────────┼──► Hospital
            │     │   (interface)                 (retries, error mapping)              │    Directory API
            │     │        │                                                            │
            │     │        ▼                                                            │
            │     │   JsonFileRepository  ──► DATA_DIR/jobs/<batch_id>/{job,records}.json
            │     │                                                                     │
            │     └─► BackgroundTasks (same process) for ?background=true               │
            └───────────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `main.py` | Routes, status codes, dependency wiring, background dispatch | everything below |
| `csv_validation.py` | Parse bytes into valid rows, invalid rows (with reasons) and file-level errors | `models` |
| `service.py` | Business rules: concurrent run, outage stop, activation rule, status derivation, resume + upstream check, cleanup | `client`, `repository`, `models` |
| `client.py` | Upstream HTTP, retry rules (D22), `Retry-After`, error classification (`retryable`, `outcome_unknown`) | `httpx` |
| `repository.py` | Persistence behind an interface | `models` |
| `models.py` | Pydantic types and status constants | — |
| `config.py` | Env-var settings | — |
| `static/index.html` | Test console (no build step) | the HTTP API only |

Dependencies point inward: the service never imports FastAPI, and the repository and
client know nothing about each other.

---

## 3. Patterns in use

### 3.1 Layered architecture (HTTP → service → infrastructure)
`main.py` translates HTTP to function calls and exceptions to status codes
(`ActivationNotAllowed` → 409, `CleanupBlocked` → 409, validation → 422). All business
rules live in `service.py`, which is plain Python and testable without HTTP.

### 3.2 Repository pattern
`JobRepository` (ABC) is the storage seam. `JsonFileRepository` is a stand-in for a
database and deliberately models two tables:

- `bulk_jobs` ↔ `job.json`
- `bulk_job_records` keyed by `(batch_id, row)` ↔ `records.json`

Swapping in SQL means a new class implementing the same 9 methods; service code does
not change.

### 3.3 Gateway / anti-corruption wrapper for the upstream
`HospitalDirectoryClient` hides `httpx` and the upstream's error shapes. Every failure
becomes one type, `UpstreamError(message, status_code)`, and `_detail()` flattens
FastAPI-style validation errors into readable text that is stored on the row.

### 3.4 Retry with exponential backoff and jitter (transient-only, safe-only)
Retries 500, 503, 429 (waiting `Retry-After`, capped at 30 s) and requests that never left
(connection refused, pool timeout), with `backoff * 2**(attempt-1)` × random jitter
(default 2 retries, 0.5 s base). Other 4xx fail immediately: the data was rejected. A
**create** that timed out, lost its connection after sending, or got a gateway
502/504 from the proxy in front of the upstream is *not* retried: it may
have been applied, so it is marked `outcome_unknown` and resolved by resume (D22).

### 3.4b Bulkhead: bounded concurrency with a shared limit
`run_batch` sends rows through a thread pool of `UPSTREAM_CONCURRENCY` workers, and one
process-wide `BoundedSemaphore` caps creates in flight across all batches (D23). A simple
circuit breaker stops starting new rows after N consecutive transient failures.

### 3.5 Dependency injection + factory
FastAPI `Depends` supplies `Settings`, the repository and a **client factory**. The
factory exists because a background run must create a client that outlives the request
(a request-scoped client would be closed before the background task runs). Tests
replace these via `dependency_overrides` and an `httpx.MockTransport` (`FakeUpstream`).

### 3.6 Persist-before-act (write-ahead state)
Every row is written as `pending` **before** any upstream call, and each transition is
written as it happens. Consequences:

- Progress is readable from storage at any time (counters double as progress).
- After a crash the stored state shows exactly how far the run got.
- Failed rows keep their full data (`address`, `phone`), so they can be retried later.

### 3.7 Explicit state machines

```
Record:  pending ─► processing ─► created ─► created_and_activated
            ▲                 └─► failed  (retryable=true → back to pending on resume)
            └──── resume ─────────┘        (outcome_unknown → checked upstream first)
         invalid                  (data not correct; stored at upload, never sent)

Job:     processing ─► completed    (all rows created AND batch activated)
                   ├─► partial      (some created; some failed/invalid or not activated)
                   ├─► failed       (nothing created, or unexpected error)
                   └─► interrupted  (stopped before every row was tried: outage / restart)
         partial | interrupted | failed ──(POST /resume)──► processing
         partial ──(POST /activate)──► completed | partial(activated)
```

Status strings are module constants in `models.py`; job status is *derived from the
rows* in one place (`_derive_status`), so the first run, resume and activation can't
disagree.

### 3.8 Validate-all-then-act, at two levels (D19)
`parse_csv` checks the file, the header and every row in one pass and reports **every**
problem with row and field. **File-level** problems (missing column, encoding, size,
more than `MAX_ROWS` rows, broken quoting, or no valid row at all) return 422: nothing is
stored and no upstream call is made. **Row-level** problems (empty `name`/`address`,
extra values) make that row `invalid`: it is stored with the reason and never sent, and
the valid rows are processed. Unknown columns are ignored with a warning (D17).

### 3.9 Async request-reply (202 + polling)
`POST /hospitals/bulk?background=true` validates synchronously, persists the job,
returns **202** with the batch id, and processes in a `BackgroundTasks` task. Clients
poll `GET /hospitals/bulk/{batch_id}`. The synchronous mode is kept for simple clients
and tests.

### 3.10 Atomic file writes + per-key locking
`_write` writes a temp file then `os.replace` (atomic on POSIX), so readers never see a
half-written file. A per-batch `RLock` serialises read-modify-write. `job.json` is
written **last** in `create_job`, so a job "exists" only once its records do; a
directory without `job.json` is an orphan that `purge_orphans` removes.

### 3.11 Defensive input handling
`batch_id` from URLs is parsed as a UUID and re-stringified before touching the
filesystem (path-traversal protection). Upload reads are capped at
`MAX_FILE_BYTES + 1`.

### 3.12 Safe destructive operation (ordered compensation)
Hard clean deletes **upstream first, local second**, and keeps the local job if the
upstream delete fails, so upstream records are never orphaned (local state is the only
index of what this app created). Guard rails: `confirm=true` required, 409 while any
batch is `processing` (override with `force=true`), upstream 404 treated as already
deleted, scope limited to batch ids this app created.

---

## 4. Request flows

### 4.1 Synchronous upload
1. Read ≤ `MAX_FILE_BYTES+1` bytes → `parse_csv`. File-level errors → 422, stop.
2. `start_batch`: new UUID, `Job(processing)`; valid rows stored `pending`, invalid rows
   stored `invalid` with their reason.
3. `run_batch`: `pending` rows go through the pool (≤ N in flight process-wide). Each
   row: `processing` → `POST /hospitals/` → `created(hospital_id)` or
   `failed(error, retryable, outcome_unknown)`; job counters updated under a lock. After
   N consecutive transient failures no new rows start (→ `interrupted`).
4. Activation rule: rows await activation, none unfinished, and
   `(no failed/invalid rows or activate_on_partial)` →
   `PATCH .../activate` → records `created → created_and_activated`.
5. Derive job status, set `finished_at`, return `BulkResult` (200, even for `partial`).
6. Any unexpected exception → job marked `failed` with `error`, then re-raised (never
   left stuck in `processing`).

### 4.2 Background upload
Same as 4.1, but step 3 onward runs after the 202 response, in the same process.

### 4.2b Resume
`POST /hospitals/bulk/{id}/resume`: under a guard, refuse if processing or nothing is
retryable (409); check unknown-outcome rows against `GET /hospitals/batch/{id}` (found →
`created`, else `pending`); reset retryable failures to `pending`; mark the job
`processing` (start time reset, so processing time reports this run); then `run_batch` as
in 4.1. Created rows are never re-sent.

### 4.3 Late activation
`POST /hospitals/bulk/{id}/activate`: 409 if processing / already active / nothing
created; otherwise activate upstream, transition rows, recompute status. An upstream
failure is stored as `activation_error` and returned (not an HTTP error).

### 4.4 Hard clean
See 3.12. Returns `{batches_deleted, hospitals_deleted, orphans_removed, failed[], clean}`.

---

## 5. Decisions taken (with rationale)

| # | Decision | Why | Status |
| --- | --- | --- | --- |
| D1 | Validate the whole file before any upstream call; report every problem in one pass. File-level problems → 422 (refined by D19) | No half-created batches from an untrustworthy file; one fix pass for the user | Done |
| D2 | Partial failure keeps created rows; does **not** activate by default (`activate_on_partial` opt-in) | Avoid publishing an incomplete batch silently; user can activate later | Done |
| D3 | Return 200 with `status: partial` rather than an error code | The request succeeded; the per-row outcome is data | Done |
| D4 | Persist every row before calling upstream; update per transition | Progress, crash forensics, retry of failed rows | Done |
| D5 | Keep address/phone on stored records even though the response omits them | Needed to retry failed rows | Done |
| D6 | Storage behind `JobRepository`; JSON files for now, modelled as two tables | Zero-dependency prototype with a clean path to SQL | Done |
| D7 | Retry only transient failures; never retry 4xx | 4xx is deterministic; retrying wastes time | Done |
| D8 | ~~Sequential processing, in row order~~ | Baseline; superseded by D23 | Superseded |
| D9 | Sync `def` handlers + blocking `httpx.Client` (FastAPI threadpool) | Simpler code; no async plumbing needed at current load | Done (to revisit) |
| D10 | Optional 202 + polling mode via `BackgroundTasks` | Real progress in the UI without a queue/infra | Done |
| D11 | `MAX_ROWS` default **20** (the brief's limit), configurable; `0` = unlimited | The brief states it as a constraint; still configurable for load tests | Done (changed from 0 on 2026-11-17) |
| D12 | Separate `/hospitals/bulk/validate` endpoint (dry run) | Let users check a file with no side effects | Done |
| D13 | Late activation endpoint for partial batches (409 on invalid state) | Completes the D2 workflow | Done |
| D14 | Hard clean: upstream first, local second; `confirm=true`; 409 while processing unless `force` | Never orphan upstream records; prevent accidents | Done |
| D15 | No authentication; `/ui` is a dev/test console | Prototype scope | Done (must change before real use) |
| D16 | Original CSV file is not stored, only parsed rows | Less data to retain; rows are enough to retry | Done |
| D17 | Extra (unknown) columns: accept with a non-blocking warning; still reject missing required columns | Data is read by column name, so extras can't corrupt anything | **Done** (2026-11-17) |
| D18 | Rows with more values than the header: accept if extras are empty (trailing commas); fail the row if extras contain data | Truncating would silently shift values when a field has an unquoted comma | **Done** (2026-11-17) |
| D20 | **Upstream owns the hospital rules.** This service checks only that the file and each row are structurally usable (columns, required fields, row shape). Rules about the hospital itself (duplicates, phone format, etc.) are left to the upstream API; whatever it rejects is shown on that row with the upstream message (already the case: 4xx detail is stored as the row error) | One source of truth; avoids our rules drifting from, or contradicting, upstream's | **Decided (O3)** |
| D21 | **Resume the same batch for system failures; new upload for data failures.** Manual `POST /hospitals/bulk/{batch_id}/resume` + "Resume" button. Startup sweep marks stale `processing` jobs `interrupted`. Failed rows carry a `retryable` flag. Uncertain rows are checked against our own batch upstream before resending. Then the activation rule runs again (§8.4) | Never re-send rows that succeeded; one batch id, one report, one activation | **Done** (2026-11-17), decided as O6 |
| D22 | **No `POST` retry on timeout or dropped connection.** Such rows are marked "unknown outcome" and resolved by the resume check. Gateway 502/504 on a create are treated the same way (added after the 2026-11-17 assessment review: Render's proxy can answer after the app saved the row). 500, 503, 429 and refused connections are still retried. No idempotency key | Removes our own retry-duplicate without inventing business rules | **Done** (2026-11-17), decided as O5 |
| D23 | **Bounded concurrency with a semaphore, N = 10.** One process-wide semaphore shared by all batches, so total in-flight creates never exceed N. `UPSTREAM_CONCURRENCY` is configurable (1 = sequential; 20 = verified maximum). Results reported in row order; stop the batch as `interrupted` after repeated connection/5xx failures; honour `429 Retry-After` | Measured: upstream throughput is linear up to 20 in flight; 500 rows ≈ 5 min at 10 vs ≈ 49 min sequential (§8.5) | **Done** (2026-11-17), decided as O7 |
| D24 | **No phone format check.** Our row checks mirror upstream's published contract (required, non-empty `name` and `address`) and nothing stricter. Phone is trimmed; empty → `null` | Upstream accepts any phone string; a stricter local rule would reject rows upstream accepts (D20) | **Decided (O4)**, no code change needed |
| D19 | Row-level problems do not reject the file: valid rows are processed, each defective row is recorded as `invalid` with "Row data is not correct: …". File-level problems (and a file with no valid row) still return 422. Invalid rows count as failed and block auto-activation (§8.1) | One bad row should not block the good ones | **Done** (2026-11-17), decided as O1 |
| D25 | Hard clean is **off by default** (403, button hidden); enabled only by `ENABLE_HARD_CLEAN=true` on a trusted local instance. Only `1/true/yes/on` enable it. Compose publishes ports on 127.0.0.1 only | A public URL with no auth must not offer "delete everything", and forgetting a variable must not turn it on (secure by default; review 2026-11-17 replaced the earlier on-by-default) | **Done** (2026-11-17) |
| D26 | After a resume, `processing_time_seconds` reports the latest run (start time reset); `created_at` keeps the upload time | Total elapsed time would include hours of downtime and mean nothing as "processing time" | **Done** (2026-11-17) |
| D27 | Packaging: `Dockerfile` (non-root, healthcheck, single worker), `docker-compose.yml` (real upstream, or `--profile fake` fully offline), `render.yaml` blueprint | Brief's Docker bonus + one-click Render deploy | **Done** (2026-11-17) |
| D28 | Wake the upstream before sending creates: one `GET /` with a 60 s timeout (`UPSTREAM_WARMUP_SECONDS`). If it doesn't answer, nothing is sent and the batch ends `interrupted`, ready to resume | The upstream is a free Render instance that sleeps when idle; without this, the first batch sends 10 creates that all time out with an unknown outcome. Raised by the assessment review | **Done** (2026-11-17) |

---

## 6. Is this good practice?

**Yes, for a prototype, and the seams are in the right places.** Specifically:

- Business rules are isolated from HTTP and from storage, so they are unit-testable and
  the infrastructure can change underneath them.
- External calls are wrapped, with a single error type and a deliberate retry policy.
- State is explicit (status enums, transitions in one place), persisted before side
  effects, and never left "running" after an unexpected error.
- Validation collects every problem, separates file-level from row-level, and has no
  side effects.
- Decisions are backed by measurements of the real upstream, not guesses (§8.5).
- Retries are deliberate: only when safe, never a blind re-send of a create that may have
  been applied (D22); resume checks upstream instead.
- Destructive operations are ordered to fail safe, need confirmation, and are off on the
  public deployment.
- The test suite runs with no network (`FakeUpstream`): success, partial failure, invalid rows,
  retries and retry rules, concurrency limits, outage stop, resume (including lost
  responses and crashes), activation, cleanup and storage concurrency.

**Where it is knowingly below production practice** (each is a scaling item in §7 or a
deferred decision in §8, not a design flaw to rewrite):

- Single process: background work, locks, the upstream limiter and the resume guard live
  in memory. Restarts are handled (startup sweep + resume), but there is no automatic
  resume and no scale-out (O8, §8.7).
- File storage rewrites the whole `records.json` per row update (~28 ms/row at 500 rows,
  measured); fine at the 20-row limit, replaced by SQL at scale (O9).
- One HTTP connection pool per run, not per process.
- No auth, no structured logging/metrics.
- `Settings()` is rebuilt per request and not validated (O14).

---

## 7. Scaling analysis

### 7.1 Where the time and cost go today

| Dimension | Current behaviour | Rough cost |
| --- | --- | --- |
| Upstream throughput | 10 creates in flight per process (D23) | ~5.9 s per create → 20 rows ≈ 12 s (was ≈ 2 min one at a time); 500 rows ≈ 5 min |
| Record writes | Each update reads + rewrites all of `records.json` | O(n) per update → O(n²) per batch; measured 28 ms/row at 500 rows; fine at 20, slow at 10k+ |
| `list_jobs` | Reads every `job.json` on each call | O(jobs); no pagination |
| Concurrency of requests | Sync handlers in the FastAPI threadpool (~40 threads) | A long sync upload holds a thread for its full duration |
| Processes | Locks and background tasks are in-memory, per process | **Cannot run >1 worker safely** |
| Durability | Local disk; Render free tier disk is ephemeral | State lost on redeploy |
| Recovery | Startup sweep marks orphaned runs `interrupted`; manual resume with upstream check (D21) | No automatic resume yet (needs O8) |

### 7.2 Can this design scale?

**Yes, incrementally, without changing the core design.** The three seams already exist:

1. `run_batch` is the single place that decides how rows are sent upstream.
2. `JobRepository` is the single place that decides how state is stored.
3. The client factory is the single place that decides how upstream connections are made.

What **does** have to change to scale is the *execution model* (in-process background
tasks → durable queue + workers) and the *storage implementation*. Neither requires
touching routes, models, validation or the business rules.

The hard limit is the **upstream**, not this service: no bulk endpoint, no idempotency
key, unknown rate limits. Scaling past that needs either upstream changes or careful
client-side rate limiting.

### 7.3 Proposed path (each stage is independently shippable)

**Stage 1: same process, much faster** (✅ done except the last two items)
- ✅ Bounded concurrency with a process-wide semaphore (D23): 9× speed-up measured.
- ✅ `Retry-After` on 429; jitter on backoff.
- ✅ Resume with upstream check; startup sweep marks orphaned runs `interrupted` (D21).
- ✅ Outage stop (circuit breaker).
- One pooled `httpx.Client` for the app lifetime instead of per run.
- Cache/validate `Settings` (`pydantic-settings`, O14).

**Stage 2: multi-worker, durable (infrastructure change)**
- `SqlJobRepository` (Postgres): row-level `UPDATE`, indexes on `(batch_id, status)`,
  transactions/conditional updates instead of in-memory locks, paginated `list_jobs`.
- Replace `BackgroundTasks` with a durable queue (Postgres `SELECT … FOR UPDATE SKIP
  LOCKED`, or RQ/Celery/arq). API becomes stateless; workers scale horizontally.
- Make 202 + polling the default; keep sync mode only for small files.
- Automatic resume of `interrupted` batches by the workers (leases replace the
  startup sweep and the in-memory resume guard).

**Stage 3: large files and real users**
- Stream-parse large CSVs; store the upload in object storage; process in chunks.
- Per-tenant/global upstream rate limiter shared across workers.
- AuthN/AuthZ; restrict hard clean to admins (or remove it outside dev).
- Structured logs, metrics (rows/s, retry rate, upstream latency), tracing per batch.
- Push progress (SSE/WebSocket) instead of polling, if needed.

### 7.4 Working with a slow third-party API: common practice

Measured on the real upstream (§8.5): create ≈ 5.9 s, reads ≈ 0.5–0.8 s, and 5
parallel creates finish in 6.2 s. The delay is steady (5.5–6.7 s), only on create,
and does not grow under parallel load. That looks like a deliberate fixed delay rather
than a busy server. Either way, the design should assume a slow dependency we do not
control.

**Rule of thumb (Little's law):** throughput = requests in flight ÷ latency. Latency is
fixed by the provider, so the levers we control are *how many requests are in flight*
and *how many requests we need at all*.

| 500 rows at 5.9 s | In flight | Time |
| --- | --- | --- |
| Sequential (before D23) | 1 | ~49 min |
| | 5 | ~10 min (measured: 5 in 6.2 s) |
| **Now (D23)** | 10 | ~5 min (measured: 10 in 6.15 s) |
| | 20 | ~2.5 min (measured: 20 in 6.19 s) |
| | 50 | ~1 min (not verified; may hit limits) |

**Practices, and where this project stands**

| Practice | Why | Here |
| --- | --- | --- |
| Asynchronous jobs (202 + status / webhook), never a minutes-long HTTP request | Proxies and load balancers cut long requests off; users need progress | `?background=true` exists; should become the default (O10) |
| Durable state, resume, reconciliation | Long jobs will be interrupted | D4, D21 |
| Bounded concurrency (bulkhead) | Speed without flooding the provider | ✅ D23 |
| Client-side rate limit shared across workers, honour `429 Retry-After` | Stay inside the provider's quota | `Retry-After` ✅; cross-worker limit is Stage 3 |
| Timeouts based on measured p99 (separate connect / read) | Fail fast, without cutting off slow successes | 30 s read timeout vs 6 s latency: fine |
| Retry only when safe; backoff with jitter; idempotency keys if the provider offers them | Avoid duplicates and retry storms | ✅ D22 + jitter (no key offered: resume checks upstream) |
| Circuit breaker | Stop hammering a provider that is down | ✅ outage stop → `interrupted` |
| Connection pooling / keep-alive | A new TLS connection costs ~0.35 s here | ✅ per run; per process is Stage 1 |
| Use bulk endpoints, or ask the provider for one | The biggest win of all: 1 call instead of N | Upstream has none |
| Measure per call (p50/p95/p99, error rate) | Decide from numbers, spot provider changes | `upstream_ms` + `scripts/batch_timing.py` |
| Test against a fake with realistic latency | Repeatable load tests without touching the provider | `FAKE_UPSTREAM_DELAY` |
| Show progress and an ETA to the user | Long jobs feel broken without them | UI progress bar; ETA to add |

---

## 8. Decisions log and deferred items

Every question raised in review, with its outcome. Items still open are **deferred**:
they don't affect the assignment at its 20-row scale and are part of the scaling path
in §7.3.

The deep dives (§8.1–§8.8) are kept **as written at the time** of each discussion, so
they show how each decision was reached. "Before:" paragraphs describe the code as it
was then; the current behaviour is in §2–§5.

| # | Question | Options | Recommendation |
| --- | --- | --- | --- |
| ~~O1~~ | Row-level validation problems | **Decided → (b), see D19 and §8.1** | |
| ~~O2~~ | Confirm D18 (extra non-empty values fail the row) | **Decided → fail the row, see D18** | |
| ~~O3~~ | Duplicate hospitals within one file | **Decided → leave to upstream (D20, §8.2)** | |
| ~~O4~~ | Phone format validation | **Decided → D24 (no check)** | |
| ~~O5~~ | Duplicate-on-retry | **Decided → D22 (§8.3, §8.4)** | |
| ~~O6~~ | Resume / retry API | **Decided → D21 (§8.4)** | |
| ~~O7~~ | Concurrency | **Decided → D23 (semaphore, N=10, configurable)** | |
| O8 | Background execution | `BackgroundTasks` / in-process worker / DB-backed queue / Celery-RQ | **Deferred.** Keep `BackgroundTasks` now; DB-backed queue with leases once on Postgres; no broker (see §8.7) |
| O9 | Storage target | JSON files / SQLite / Postgres | **Deferred.** JSON files are enough for the brief (in-memory allowed); Postgres with O8 |
| O10 | Default mode for `POST /hospitals/bulk` | sync / 202 | **Deferred.** Sync stays the default (20 rows ≈ 12 s, and it returns the brief's response shape); `?background=true` for progress. Make 202 the default if the row limit is raised |
| ~~O11~~ | `MAX_ROWS` default | **Decided → 20 in code (D11)** | |
| ~~O12~~ | Hard clean in non-dev environments | **Decided → D25** (off on the public deployment). Auth itself: deferred | |
| O13 | Retain original CSV? | no / yes (object storage, with retention) | **Deferred.** No (D16) |
| O14 | `Settings` caching and validation | per request / `lru_cache` / `pydantic-settings` | **Deferred.** `pydantic-settings` with a cached accessor |
| ~~O15~~ | Deployment packaging | **Decided → D27** (Docker + compose + Render blueprint) | |
| O16 | Cap on resumes per batch | none / fixed max (e.g. 5) | **Deferred.** Each resume is user-triggered and only resends retryable rows |
| O17 | Strict all-or-nothing mode (`?on_invalid_rows=reject`, O1.1) | add / skip | **Deferred.** Lenient default built; opt-in strict mode not needed by the brief |

### 8.1 O1 deep dive: row-level validation problems

**Before:** one bad row (e.g. empty `address` on row 7 of 500) rejects the whole file
with 422. Nothing is stored and no upstream call is made. The user fixes the file and
uploads again.

**Options**

| | (a) Strict: keep 422 for any error | (b) Lenient: process valid rows, store invalid ones as failed records |
| --- | --- | --- |
| User experience | Fix and re-upload everything; one typo blocks 499 good rows | Good rows go through; one list shows every problem |
| Data safety | Maximum: all-or-nothing per file | Same for the data that is sent; bad rows never reach upstream |
| Batch semantics | One file = one complete batch | One file = one batch with gaps; invalid rows must go in a later batch |
| Retry | Re-upload the file | Fix the invalid rows and upload just those (they need edits, so a plain retry does not help) |
| Code change | None | Parser, service, models, `/validate`, UI, tests |

**What stays file-level (422) under either option:** missing required column,
empty/unreadable file, non-UTF-8, over `MAX_FILE_BYTES`, header with no data rows,
over `MAX_ROWS` (counting every data row, valid or not), and a `csv.Error` mid-file (a
broken quote makes every later row untrustworthy).

**Sub-decisions if we choose (b)**

| # | Question | Recommendation | Why |
| --- | --- | --- | --- |
| O1.1 | Default behaviour | Lenient by default, with `?on_invalid_rows=reject` for all-or-nothing clients | Matches the bulk-upload UX; API clients that need atomic files keep it |
| O1.2 | Row status for validation failures | New status `invalid` (not `failed`) | `failed` = upstream rejected, may be retryable; `invalid` = data must be edited first. The UI shows both in red with their error |
| O1.3 | Counting | `failed_hospitals` includes `invalid` rows (response shape from the brief unchanged); per-row `status` tells them apart | No breaking change to `BulkResult` |
| O1.4 | Effect on activation | Invalid rows count as failures, so by default the batch is **not** activated (D2 applies); user activates from the UI or passes `activate_on_partial` | Consistent with how upstream failures are handled today |
| O1.5 | Every row invalid | 422 as today; no job stored | Nothing to process; storing an empty failed batch adds noise |
| O1.6 | What is stored for an invalid row | Raw trimmed values (may be empty) plus the error message(s); no upstream call | Lets the UI show the bad row next to the reason |
| O1.7 | `/validate` response | `valid` = file accepted; add `processable_rows`, `invalid_rows`; `errors` keeps row numbers and fields | Preview matches exactly what `POST` will do |
| O1.8 | Row-length rule (D18) | Applies here: a row with extra non-empty values becomes `invalid` | One rule set for row problems |

**Impact if (b) is accepted:** `parse_csv` returns `(valid_rows, invalid_rows,
file_errors)`; `start_batch` stores invalid rows as `invalid` records and skips them in
`run_batch`; status derivation treats `invalid` like `failed`; `Record.name`/`address`
allow empty strings; UI legend gains "invalid"; tests: `test_collects_all_row_errors_*`
and `test_validation_failure_*` split into file-level (still 422) and row-level
(stored, no upstream call).

**Implemented 2026-11-17 (D19).** All sub-points as recommended, except O1.1's opt-in
strict mode (deferred, O17).

**Status: Decided (2026-11-17) → (b).** When most rows are valid and a row does not
follow the correct structure, process the valid rows and record an error against the
defective row with a clear "data is not correct" message naming the problem. This also
confirms D18. Sub-points O1.1–O1.8 follow the recommendations above; they were not
discussed one by one, so any of them can still be revisited during implementation.

### 8.2 O3 deep dive: duplicate hospitals within one file

**Before:** not detected. Two identical rows create two hospitals upstream.

**What counts as a duplicate:** same `name` **and** same `address`, compared after
trimming, collapsing inner whitespace and ignoring case. Same name with a different
address is a different branch and is **not** a duplicate. Phone is ignored for matching.

**Options**

| | (a) Ignore | (b) Warn, process all | (c) Process the first, mark later copies `invalid` |
| --- | --- | --- | --- |
| Upstream duplicates | Yes | Yes | No |
| User effort | None | Must clean up upstream afterwards | Nothing to clean up; sees "duplicate of row N" |
| Fits O1 decision | — | Partly | Yes: a duplicate is one more kind of defective row |

**Recommendation: (c).** Later copies get status `invalid` with
"Duplicate of row N (same name and address)". The first occurrence is processed.

**Edge cases**

- Same name+address but **different phone**: still a duplicate (the first row wins).
  The error names the conflict so the user can choose which phone is correct.
- Duplicates across **different uploads**, or against hospitals already upstream: out of
  scope. It would need an upstream lookup, and the upstream is shared with other users.
- Duplicates count toward `failed_hospitals` and block auto-activation, as in O1.4.

**Status: Decided (2026-11-17) → none of the above; leave it to upstream.**
Duplicate detection is the upstream API's job, not ours. We send every structurally
valid row as is. If upstream rejects a row as a duplicate, that row is `failed` and its
upstream message is shown in the results and Batches views. If upstream accepts
duplicates, that is upstream's intended behaviour and we don't add rules of our own.
No code change is needed: upstream 4xx messages are already stored on the row. This
became the general principle D20.

Notes:
- The fake upstream (`scripts/fake_upstream.py`) does not simulate a duplicate rejection.
  It only rejects names containing `FAIL`, and that path already shows how such an error
  would appear.
- Whether the real upstream rejects duplicates has not been checked.

### 8.3 O5 in light of D20: the retry duplicate

D20 covers duplicates that are **in the data**. One case it doesn't cover is a
duplicate created by **our own retry**:

1. We `POST` row 5. Upstream creates hospital #101, but the response is lost
   (timeout or dropped connection).
2. Our client sees a timeout and retries the `POST`.
3. Then one of two things happens:
   - If upstream **allows duplicates**, it creates #102, a second copy of the same
     hospital that the user never asked for.
   - If upstream **rejects duplicates**, the retry gets a 4xx and row 5 is marked
     `failed: duplicate`, even though #101 was created. The report is wrong: the row
     says failed, but the hospital exists. And because a batch with a failed row isn't
     auto-activated, #101 sits there inactive.

Options that stay within "invent no business rules":
- (a) **Accept the risk** and document it. It's rare, since it needs a lost response.
- (b) **Don't retry `POST` on timeout or connection error.** Only retry when we know the
  request never reached upstream (a refused connection) or upstream said so (5xx, 429).
  That's a transport decision, not a business rule. The cost is more rows `failed` on
  flaky networks.
- (c) Check the batch upstream before retrying. This is the most accurate, but it adds
  behaviour.

**Leaning:** (b). It is small, adds no new rules and removes the wrong-report case.
**Status: Decided (2026-11-17) → option (b), as part of §8.4 (D22).**

### 8.4 Retry and resume of a bulk operation (O5 + O6)

Raised in review: if a bulk run fails partway (timeout, upstream down, crash), what do
we do? Retry the same batch, or have the user start a new bulk operation? Because every
row is stored with its state (D4), we can resume the existing batch instead of starting
over.

#### Three levels of retry

| Level | What it handles | Who triggers it | Today |
| --- | --- | --- | --- |
| 1. Request | A blip on one call (5xx, 429, refused connection) | Automatic, within seconds | Exists (`client.py`, 2 retries with backoff) |
| 2. Row | A row that still failed after level 1 | Part of a batch resume | Missing |
| 3. Batch (resume) | A run that stopped partway, or ended with retryable failures | The user ("Resume" button / endpoint) | Missing. Activation alone can already be resumed (`/activate`) |

#### What each failure leaves behind, and the right action

| # | Failure | Row state left | Already upstream? | Right action |
| --- | --- | --- | --- | --- |
| A | Upstream rejected the row (4xx: bad data, duplicate) | `failed`, upstream message | No | **Not retryable as is.** User fixes the data and uploads a new file with those rows |
| B | Upstream unavailable after level-1 retries (5xx, 429, refused) | `failed` | No | **Resume** the same batch later |
| C | Timeout / lost response | `failed` today | **Unknown** | **Reconcile, then resume** |
| D | Process crashed or restarted mid-run | job stuck `processing`; one row `processing`, the rest `pending` | `pending`: no. `processing`: unknown | **Reconcile, then resume** |
| E | All rows created, activation failed | rows `created`, job `partial` | Yes, inactive | Resume = activate (exists today: `/activate`) |
| F | Our own bug (unexpected exception) | job `failed` | Mixed | Fix the bug, then resume (same as D) |

#### Resume the same batch, or start a new bulk operation?

| | Resume the same batch | New bulk operation (re-upload) |
| --- | --- | --- |
| Rows already created | Skipped, never re-sent | Re-sent: duplicates upstream, or rejected as duplicates (D20) and shown as failures |
| Batch id upstream | One id for the whole file | The file is split across two ids, and each has to be activated separately |
| Report | One place shows the final outcome of every row | Split across two batches |
| Activation | One `PATCH` once the batch is complete | Partial batches left behind, usually inactive |
| Hard clean | Finds everything by one id | Works, but two batches |
| Effort for us | A resume endpoint, a retryable flag, reconciliation | Nothing |
| Effort for the user | One click | Edit the file to remove the rows that already went through |

**Conclusion:**
- For **system failures** (B, C, D, E, F), **resume the same batch**. Starting over would
  re-send rows that already succeeded.
- For **data failures** (A), **start a new bulk operation** with the corrected rows. A
  resume can't help, because the data itself has to change. To make that easy, offer a
  "download failed rows as CSV" with the error column. (Built after the assessment
  review: `GET /hospitals/bulk/{id}/failed.csv` and a UI button.)

#### How resume works

1. **Only one runner at a time.** Resume is refused (409) while the batch is really
   running.
2. **Detect interrupted runs.** After a restart a job still says `processing`, but
   nothing is running it. A startup sweep marks such jobs `interrupted`. That is enough
   for a single process. With multiple workers (Stage 2) it becomes a lease or heartbeat
   on the job.
3. **Classify failed rows when they fail.** Store a `retryable` flag on the record:
   - true for 5xx, 429 and connection errors (B)
   - "unknown outcome" for timeouts (C)
   - false for other 4xx (A)

   `UpstreamError.status_code` already tells these apart.
4. **Reconcile the ambiguous rows** (C, and D's `processing` row). Call
   `GET /hospitals/batch/{batch_id}` and look for that row in **our own batch**:
   - found → mark it `created` and take its `hospital_id`
   - not found → send it again

   This checks the outcome of our own request. It is not a business rule, so it stays
   within D20, and it removes the need for an idempotency key.
5. **Send the rest:** `pending` rows plus retryable `failed` rows, in row order, using
   the same `run_batch` loop. `attempts` goes up each time.
6. **Activate again** using the usual rule. Activation applies to the whole batch, so
   one call also covers rows created during the resume, even if the batch was activated
   earlier with `activate_on_partial`.
7. **Recompute the job status** (completed / partial / failed).

This settles O5. Level 1 stops retrying `POST` on a timeout (§8.3 option b): the row is
marked "unknown outcome", and the batch resume reconciles it safely. There is no
idempotency key and no invented behaviour.

#### Manual or automatic resume

**Recommendation: manual first.** Show a "Resume" button on interrupted or retryable
batches in the Batches tab, backed by `POST /hospitals/bulk/{batch_id}/resume`.
Automatic resume (on startup, or a scheduled retry with backoff) can wait for the durable
queue in Stage 2. Resuming automatically while upstream is still down only produces more
failures, and the user should know a batch is being retried.

#### Still to confirm

- ~~Does the real upstream expose `GET /hospitals/batch/{batch_id}`?~~ Yes, confirmed
  2026-11-17 (it returns the batch's hospitals with ids, in 0.75 s).
- How to match a row during reconciliation: name + address within our own batch is
  reliable, because the batch scope is small and every row in it is ours.
- Should there be a limit on resumes per batch (e.g. 5)?

**Status: Decided (2026-11-17) → adopted as recommended (D21, D22).** The three
"still to confirm" items stay open, as implementation checks.

### 8.5 O7 deep dive: concurrency

**Before:** one upstream call at a time, in row order.

**Measured against the real upstream (2026-11-17, curl from a dev machine):**

| Call | Time |
| --- | --- |
| Network connect + TLS | ~0.2–0.35 s |
| `GET /` (health) | 0.52–0.64 s |
| `GET /hospitals/` (≈267 KB) | 0.78–1.55 s |
| `POST /hospitals/`, 5 sent one after another | 5.51, 6.15, 5.52, 5.64, 6.71 s → avg **5.9 s** |
| `POST /hospitals/`, 5 sent **at the same time** | each 5.5–6.1 s, **6.2 s wall clock for all 5** |
| `GET /hospitals/batch/{id}` | 0.75 s |
| `PATCH /hospitals/batch/{id}/activate` | 0.70 s |
| `DELETE /hospitals/batch/{id}` | 1.10 s |

**Parallel probe** (Python thread pool + one pooled `httpx.Client`, records deleted
right after):

| In flight | Wall clock | Per call (min / median / max) | Statuses | Rows/s |
| --- | --- | --- | --- | --- |
| 1 (sequential avg) | 5.9 s per row | 5.5 / 5.6 / 6.7 s | 200 | 0.17 |
| 5 | 6.2 s | 5.5 / – / 6.1 s | 5 × 200 | 0.81 |
| 10 | 6.15 s | 5.34 / 5.87 / 6.15 s | 10 × 200 | 1.63 |
| 20 | 6.19 s | 5.46 / 5.59 / 6.19 s | 20 × 200 | 3.23 |

Per-call time stays flat from 1 to 20 in flight, and there was no 429 or 5xx, so
throughput grows linearly up to at least 20. Where it stops is unknown. The upstream is
a shared free-tier service, so we should not push it much further without a reason.

What this shows:

- About 5.5 s of each create is spent **inside upstream** before the first byte comes
  back. The network costs well under 1 s, and reads on the same server are fast, so the
  delay is specific to create.
- Upstream does **not** queue creates behind each other: 5 at once finished in the time
  of 1. Concurrency on our side therefore helps almost linearly.
- 500 rows one at a time ≈ 500 × 5.9 s ≈ **49 min**. 5 at a time ≈ **10 min**.
- `GET /hospitals/batch/{id}` exists on the real API, which settles the first open
  check in D21.

**500-row run against the fake upstream** (`FAKE_UPSTREAM_DELAY=0.3`, batch
`52aab1af…`, 2026-11-17): every create took 0.30–0.32 s, the upstream total was
154.0 s, the job wall time 168.2 s (2.8 min). That leaves **14.2 s of our own overhead,
about 28 ms per row**, almost all of it JSON storage. Each row rewrites the full 500-row
`records.json` twice and `job.json` once. Against the real upstream the same file
would take about 500 × 5.9 s + 14 s ≈ **49 min**, so our overhead is under 0.5 % there.
Storage cost only starts to matter once upstream is fast or we send many rows at once.

Each record now stores `upstream_ms` (time spent in its create call, including retries),
and `scripts/batch_timing.py <batch_id>` prints per-row and total timing for any batch.

**Options**

| | (a) Sequential (today) | (b) Bounded concurrency per batch | (c) Bounded, plus a global limit shared by all batches |
| --- | --- | --- | --- |
| 500 rows at 6 s | ~50 min | ~10 min with 5 in flight | ~10 min for one batch; parallel uploads share the limit |
| Load on upstream | 1 call | N per batch, so 2 uploads mean 2N | Never more than N in total |
| Risk of 429 / overload | Lowest | Grows with the number of uploads | Controlled |
| Code change | None | Thread pool in `run_batch` | Same, plus one process-wide semaphore |

**How it would work:** a thread pool inside `run_batch`, sized by
`UPSTREAM_CONCURRENCY` (`1` = today's behaviour), with one shared, pooled
`httpx.Client` (it is thread-safe). No move to `async` is needed. Each row still goes
`processing` → `created`/`failed` and is stored as it finishes.

**Consequences to accept**

- **Order:** rows are *created* in no fixed order, so upstream ids are not in row order.
  Results are still *reported* in row order (records are keyed by row).
- **Counters:** the job's `created_count`/`failed_count` must be updated under the
  per-batch lock (or derived from the records), not kept as loop-local numbers.
- **After a crash:** up to N rows can be left `processing`, not just one. The resume
  check (D21) already covers this.
- **Storage:** the per-batch lock serialises the writes. Fine at small N. The O(n) file
  rewrite per update gets more noticeable; that is solved by the SQL repository (O9).

**Upstream outage (circuit breaker):** with N calls in flight, an upstream outage fails
rows N times faster. Proposal: after K consecutive connection/5xx failures (e.g. 5),
stop sending, leave the remaining rows `pending` and mark the job `interrupted`. The
user resumes it later (D21). This avoids 500 rows all ending `failed` because upstream
was down for a minute. (Built. At the 20-row limit with 10 in flight it saves little;
it matters once `MAX_ROWS` is raised.)

**Recommendation (updated after the probe):** (c) with `UPSTREAM_CONCURRENCY=10` by
default: 500 rows in ~5 min, half of the verified ceiling, which leaves room for two
uploads at once and stays polite to a shared service. Configurable: 1 = sequential,
20 = verified maximum. Plus the outage stop and `429 Retry-After` handling.

**Status: Decided (2026-11-17) → D23.** A semaphore limits in-flight creates to N = 10,
and N stays configurable.

### 8.6 O4: phone format validation

**Upstream contract** (`/openapi.json`, read 2026-11-17), `HospitalCreate`:

| Field | Upstream rule | Our check today |
| --- | --- | --- |
| `name` | required, string, min length 1 | required, non-empty after trim ✔ same |
| `address` | required, string, min length 1 | required, non-empty after trim ✔ same |
| `phone` | optional, any string or `null`, **no format, no length** | optional; empty → `null` ✔ same |
| `creation_batch_id` | UUID or `null` | we generate a UUID ✔ |

There are no maximum lengths and no uniqueness rules in the schema. Any duplicate
check, if there is one, would be in upstream's server code (D20).

**Recommendation:** no phone format check on our side. Upstream accepts any string,
so a format rule of ours would reject rows upstream would accept, which is exactly what
D20 says not to do. Keep what we already do: trim, and send an empty phone as `null`.

**How D20 reads in practice:** we check only what upstream's **published contract**
already requires (required, non-empty `name` and `address`). That is not an invented
rule: those rows would be rejected anyway, after a ~6 s round trip, so checking them
locally just fails fast. Anything stricter than the contract is left to upstream.

**Status: Decided (2026-11-17) → D24.** No phone format check.

### 8.7 O8: how background work runs

**Before:** `POST /hospitals/bulk?background=true` hands `run_batch` to FastAPI
`BackgroundTasks`. It runs after the 202 is sent, **in the same process** as the web
server, on a threadpool thread.

**What that means**

| Property | `BackgroundTasks` today |
| --- | --- |
| Survives a restart or deploy | No. The run stops; D21's startup sweep marks it `interrupted` for resume |
| More than one server process | No. Each process runs only its own tasks, and D23's semaphore is per process |
| Visibility | Only through our job records (which already exist) |
| Extra infrastructure | None |
| Fits D21 / D23 | Yes, for a single instance |

**The key observation:** our `bulk_jobs` table already *is* a queue. Every job has a
status, and every row has its own state. A "worker" just needs to pick up a job that is
waiting, mark it as taken, and run the existing `run_batch`. So moving off
`BackgroundTasks` doesn't need a new system. We would add:

- a `queued` job status: the API only stores the job and returns 202
- a **lease** on the job (`claimed_by`, `lease_until`, renewed while running). A
  worker takes a job only if it is `queued`, or its lease has expired (the worker died).
  This replaces the startup sweep from D21 and works across many workers.

**Options**

| | (a) `BackgroundTasks` (today) | (b) In-process worker loop | (c) DB-backed queue, separate worker processes | (d) Celery / RQ / arq + Redis |
| --- | --- | --- | --- | --- |
| How | Task attached to the request | Thread started at app startup, polls `queued` jobs | Workers claim jobs from Postgres with `SELECT … FOR UPDATE SKIP LOCKED` and a lease | Broker delivers tasks to workers |
| Survives restart | No (resume by hand) | Yes, picks up on next start | Yes | Yes |
| Scale out | No | No | Yes: add workers | Yes |
| New infrastructure | None | None | None beyond Postgres (O9) | Redis + worker service + monitoring |
| Effort | None | Small | Medium (needs O9) | Medium–large |
| When it fits | Prototype, single instance | Single instance, auto-resume | Production, a few job types | Many job types, schedules, complex retry workflows |

**Global limit with many workers (D23):** a per-process semaphore gives
`N × workers` in flight. Simplest fix: set `UPSTREAM_CONCURRENCY` per worker so that
`N × workers ≤ total budget` (e.g. 2 workers × 5). Only with many workers would a
shared limiter (a Postgres counter or Redis token bucket) be needed.

**Recommendation**

- **Now (prototype):** keep (a). Keep the work as one function (`run_batch`) and the
  hand-off as one call, so switching later touches only the hand-off.
- **With Postgres (Stage 2):** go to (c). It reuses the job table, gives restarts and
  scale-out for free, and adds no Redis.
- **Not** Celery/Redis unless we end up with many job types or need scheduling. One
  job type doesn't justify a broker.
- (b) is an optional step between them, if we want automatic resume before Postgres.

**Status: Deferred** (O8 in the table above): `BackgroundTasks` stays for the
single-process prototype.

---

### 8.8 Implementation notes for D21–D23 (2026-11-17)

Built and verified (unit tests, then a live run against the fake upstream on its own ports):

- **Concurrency:** 20 rows at 0.3 s/create finished in 0.7 s (9.0× speed-up). With
  `UPSTREAM_CONCURRENCY=5` the fake upstream never saw more than 5 in flight; two
  batches sharing a limit of 3 never exceeded 3 in total.
- **Outage stop with N in flight:** rows already in flight finish after the stop. So with
  10 in flight and threshold 5, 14 rows failed and 6 stayed `pending` (expected).
- **Edge case found in live testing:** the upstream died after rows 1–10 were created, and
  the stop fired only after every row had been tried. The batch said "remaining rows
  were not sent" when none remained, and it skipped the activation rule. Fixed: the
  outage message and the activation skip now apply only when rows were actually left
  unsent. Regression test added.
- **Resume** was verified end to end, including from the UI's Resume button: an
  interrupted batch went to `completed` with exactly 20 hospitals upstream (no
  duplicates); a second resume returned 409.
- **Upstream check:** matching uses exact `name` + `address` inside our own batch, and
  never reuses a hospital id already held by another row, so two identical rows in one
  file each claim a different upstream record.
- `GET /hospitals/batch/{id}` returning 404 is treated as "no rows there"; any other
  failure aborts the resume with 502 and changes nothing.

Open points from the build:

- `processing_time_seconds` counted from the original upload, so after a resume it
  included the time the batch sat stopped (65.7 s in the UI test, for under 1 s of
  work). **Resolved by D26:** it now reports the latest run.
- Max number of resumes per batch: deferred (O16).
- The resume guard and the startup sweep are single-process (same as storage). They
  become a DB lease with O8 (c).

## 9. Verification log

| What | How | Result |
| --- | --- | --- |
| Unit + API tests | `pytest` (no network) | All pass |
| Real upstream timings | curl / httpx probes, test records deleted right after | create 5.9 s avg; 20 parallel in 6.19 s (§8.5) |
| Concurrency, outage stop, resume | Live app against the fake upstream, upstream stopped and restarted mid-batch | 20 rows in 0.7 s; interrupted → resumed → completed, no duplicates |
| UI | Browser: Resume from the Batches tab, invalid rows in the dialog | As designed, no console errors |
| Docker | `docker compose --profile fake up --build`, smoke test | Runs as non-root; invalid rows recorded; 20 rows in 0.65 s |
| Independent review | A fresh AI agent with no session context, using a review prompt built from the assessment criteria | Found the gateway-502/504 retry gap, the sleeping upstream and doc drift; all fixed (D22 update, D28, failed-rows CSV) |
| Failed-rows CSV | Live app + fake upstream, `samples/partial_failure.csv`, browser | CSV holds only failed rows with errors; button shown in the dialog |
