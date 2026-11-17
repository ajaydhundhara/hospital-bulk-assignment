# Hospital Bulk Processing API

A FastAPI service that accepts a CSV of hospitals and creates them through the
[Hospital Directory API](https://hospital-directory.onrender.com/docs) as one batch.

**Live:** https://hospital-bulk-processing-t53b.onrender.com · API docs at [`/docs`](https://hospital-bulk-processing-t53b.onrender.com/docs) · test console at [`/ui`](https://hospital-bulk-processing-t53b.onrender.com/ui)

## Requirements coverage

| Brief requirement | Where |
| --- | --- |
| `POST /hospitals/bulk` with a CSV (`name,address,phone`), max 20 rows | `app/main.py`, `app/csv_validation.py`; `MAX_ROWS=20` |
| Generate a batch id, create each hospital with `creation_batch_id` | `service.start_batch` / `run_batch` |
| Activate the batch once all hospitals are created | `service._activate_if_ready` |
| Response: `batch_id, total_hospitals, processed_hospitals, failed_hospitals, processing_time_seconds, batch_activated, hospitals[]` | `models.BulkResult` (extra fields are additive) |
| Per-row result with `row, hospital_id, name, status` | `models.HospitalResult` |
| CSV validation and error handling | Two levels: file errors → 422, bad rows recorded as `invalid` (D19) |
| Bonus: batch validation endpoint | `POST /hospitals/bulk/validate` |
| Bonus: progress tracking | `?background=true` (202) + `GET /hospitals/bulk/{batch_id}` |
| Bonus: resume / retry of a failed batch | `POST /hospitals/bulk/{batch_id}/resume` |
| Performance (upstream is ~6 s per create) | 10 creates in parallel: 20 rows in ~12 s instead of ~2 min |
| Tests | `pytest`, no network |
| Deployment / Docker | `Dockerfile`, `docker-compose.yml`, `render.yaml` |
| Documentation | See [Documentation and artifacts](#documentation-and-artifacts) below |

## Documentation and artifacts

| Document | What it covers |
| --- | --- |
| [`docs/requirements.md`](docs/requirements.md) | The brief's requirements, assumptions where it was silent, and what is out of scope |
| [`DESIGN.md`](DESIGN.md) | One-page summary (§0), architecture and state diagrams, patterns, every decision with its reason (D1–D28), scaling analysis, measurements of the real upstream (§8.5), trade-offs |
| [`MANUAL_TESTING.md`](MANUAL_TESTING.md) | Manual test scenarios: outages, resume, hard clean, Docker |

## Development process

Built in two AI-assisted sessions on 16–17 Nov 2026. The order of work:

1. **Requirements and baseline:** FastAPI, a sequential first version on purpose
   ("try the variations later"), and partial-failure rules (D1–D3).
2. **Per-row state:** job and record storage behind a repository interface, saved
   before each upstream call (D4–D6). This is what made progress, results and resume
   possible later.
3. **Code review, file by file:** CSV validation made more lenient (D17–D19), and the
   manual test plan written.
4. **Test console and hard clean** (`/ui`, D14).
5. **Design review:** `DESIGN.md` with open questions O1–O15, each discussed and
   decided (D19–D24).
6. **Measure, then build:** probes of the real upstream (~5.9 s per create, flat up to
   20 in parallel), then concurrency (D23), safe retry rules (D22) and resume (D21).
7. **Packaging:** Docker, compose and the Render blueprint; hard clean made off by
   default after review (D25).
8. **Independent review** against the assessment criteria, then the fixes: gateway
   errors (D22), waking the upstream (D28), the failed-rows CSV, a mid-run test and
   pinned dependencies.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# add ENABLE_HARD_CLEAN=true in front to enable the Hard clean button
```

Interactive docs: http://localhost:8000/docs
Test console (no auth): http://localhost:8000/ui

### With Docker

```bash
docker compose up --build
```

That runs against the real upstream on http://localhost:8000 (published on 127.0.0.1
only, hard clean off). To run fully offline
against the bundled fake upstream (0.3 s per create, rejects names containing `FAIL`):

```bash
UPSTREAM_BASE_URL=http://fake-upstream:9000 docker compose --profile fake up --build
```

Prefix `ENABLE_HARD_CLEAN=true` to enable hard clean.

The image runs one uvicorn worker as a non-root user, with a healthcheck; state is kept
in the `bulk-data` volume.

### Deploy to Render

`render.yaml` is a Blueprint: in Render choose **New → Blueprint** and pick this repo. It
builds the Dockerfile on the free plan with hard clean disabled. Free instances have no
persistent disk, so batch history lasts until the next restart (the brief allows
in-memory storage).

### Test console

`/ui` is a single static page (`app/static/index.html`, no build step) served by the app:

- **Bulk upload**: pick or drop a CSV, *Validate only* or *Process*. Processing uses
  `?background=true` and polls the batch, so the progress bar shows real per-row progress.
  When done, a dialog lists every record: successes in green, failures in red with the
  upstream error (scrolls past 20 rows; filter All / Succeeded / Failed). If the batch was
  left inactive because some rows failed, an **Activate batch** button activates the
  created rows, and **Download failed rows** gives a CSV to fix and upload again.
- **Batches**: every upload, newest first, with status (Completed, Partial · not activated,
  Partial · activated, Failed, Interrupted, Processing), counts and timing. Click a row for
  its records, *Resume* a batch with retryable rows, or *Activate* a partial batch straight
  from the list.
- **Hard clean…** (Batches tab): deletes every batch created through this app, in the
  upstream API *and* locally, to get back to a fresh state. It asks you to type `DELETE`.
  **Off by default**; start the app with `ENABLE_HARD_CLEAN=true` to enable it. See
  [Hard clean](#hard-clean) below.

## Test

```bash
pytest
```

Tests mock the upstream with `httpx.MockTransport`, so they need no network.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/hospitals/bulk` | Upload CSV (multipart field `file`), create + activate a batch |
| `GET` | `/hospitals/bulk` | List all batches (job records), newest first |
| `POST` | `/hospitals/bulk/{batch_id}/resume` | Continue a stopped batch: retry unfinished / temporarily failed rows, never re-send created ones, then activate (see [Resume](#resume)) |
| `POST` | `/hospitals/bulk/{batch_id}/activate` | Activate a finished, inactive batch (e.g. partial); 409 if processing, already active or nothing created |
| `POST` | `/hospitals/bulk/validate` | Validate a CSV only; creates nothing |
| `GET` | `/hospitals/bulk/{batch_id}` | Current state of a run: summary plus every row's status and error |
| `GET` | `/hospitals/bulk/{batch_id}/job` | Raw job record (counters, timestamps, filename) |
| `GET` | `/hospitals/bulk/{batch_id}/failed.csv` | Failed and invalid rows as `name,address,phone,error`: fix and upload again (the `error` column is ignored) |
| `GET` | `/hospitals/bulk/{batch_id}/records` | Stored CSV rows with state; filter with `?status=failed` |
| `GET` | `/` | Health check |
| `GET` | `/info` | Runtime limits: `max_rows`, `upstream_concurrency`, `hard_clean_enabled` |
| `DELETE` | `/hospitals/bulk?confirm=true` | **Destructive.** Delete all batches created here (upstream + local); 403 when disabled |
| `GET` | `/ui` | Test console |

`POST /hospitals/bulk?background=true` validates synchronously, then returns **202** with
the batch id and `status: processing`, and processes rows in a background task; poll
`GET /hospitals/bulk/{batch_id}` for progress.

```bash
curl -F "file=@hospitals.csv" http://localhost:8000/hospitals/bulk
```

### CSV format

`name,address,phone` — `name` and `address` required, `phone` optional. Header is
case-insensitive; a UTF-8 BOM is accepted; blank lines are skipped. At most **20 rows**
per file (the brief's limit; `MAX_ROWS`, `0` = unlimited). Unknown columns are ignored
with a warning.

Validation works at two levels:

- **File problems** return **422** with every error and nothing is created: missing
  `name`/`address` column, not UTF-8, too large, more than 20 rows, broken quoting, or
  no valid row at all.
- **Row problems** don't block the rest: a row with an empty `name`/`address` or extra
  values (usually an unquoted comma) is recorded as `invalid` with
  "Row data is not correct: …", never sent upstream, and the valid rows are processed.
  Like a failed row, it keeps the batch inactive unless `activate_on_partial=true`.

Phone format is not checked: the upstream accepts any string, and it owns hospital rules.

### Workflow

1. Parse and validate the whole file. File-level problems return **422** with every
   error (row number, field, message) and **no upstream call is made**.
2. Generate a batch UUID.
3. `POST /hospitals/` for each row with `creation_batch_id`, **up to 10 at a time**
   (`UPSTREAM_CONCURRENCY`). Results are reported in row order.
4. `PATCH /hospitals/batch/{id}/activate` once every row was created.
5. Return the result.

### Stored state

Every upload creates a **job** and one **record per CSV row**, written to storage
*before* any upstream call and updated as each row finishes:

- Record: `row, name, address, phone, status, hospital_id, error, attempts, upstream_ms,
  retryable, outcome_unknown, updated_at` with status
  `pending → processing → created → created_and_activated`, or `failed`.
- Job: `status, filename, total_rows, created_count, failed_count, activated,
  resumable_rows, resume_count, activate_on_partial, timestamps`.

Because full row data and per-row errors are persisted, a client can read progress and
the final per-row outcome from the `GET` endpoints, and failed rows can be retried later
(address and phone are retained). If the process dies mid-run, the stored state shows
exactly how far it got. The original CSV file itself is not kept.

Storage sits behind `JobRepository` (`app/repository.py`). The current implementation,
`JsonFileRepository`, writes `DATA_DIR/jobs/<batch_id>/{job.json,records.json}`
atomically, with a per-batch lock. It models two DB tables (`bulk_jobs`,
`bulk_job_records`), so swapping in SQL touches only that class. Note that it is
single-process storage, and Render's free-tier disk is ephemeral across deploys.

### Hard clean

Disabled unless `ENABLE_HARD_CLEAN=true` (403 otherwise). Enable it only on a local,
trusted instance.

`DELETE /hospitals/bulk?confirm=true` resets to a fresh state. For every stored batch it
deletes the hospitals upstream (`DELETE /hospitals/batch/{id}`) and then removes the job,
its records and its files from this server. It returns
`{batches_deleted, hospitals_deleted, orphans_removed, failed[], clean}`.

- **Scope:** only batches this app created, found by batch ID. Other records in the
  shared upstream API are never touched.
- **Failure-safe:** upstream is deleted first, and the local batch is removed only if that
  succeeded. A batch whose upstream delete fails is kept and listed in `failed`, so nothing
  is orphaned and you can simply retry. An upstream 404 counts as already gone.
- **Refuses while a batch is `processing`** (409): a running background job would keep
  creating upstream records. `&force=true` overrides this for batches stuck after a crash.
- `confirm=true` is required (400 otherwise) so a stray request cannot wipe everything.
- There is no authentication, by design for this test console, so treat this endpoint as
  a development tool.

### Partial failure

Rows that were created are **kept**; failed rows are reported with the upstream error.
By default the batch is **not** activated if any row failed: created rows stay inactive
and the job status is `partial`. Pass `?activate_on_partial=true` to activate the
successful rows anyway.

`status` is `completed` (all rows created and activated), `partial`, `failed`, or
`interrupted` (stopped before every row was tried; see [Resume](#resume)).
Per-row `status` is `created_and_activated`, `created` (not activated), `failed`, or
`invalid` (data not correct, never sent); a failed row also says whether it is
`retryable`. After a resume, `processing_time_seconds` reports the latest run.

### Performance: concurrent creates

The upstream takes about **5.9 s per create**, and creates sent in parallel don't slow
each other down (measured up to 20 at once; see `DESIGN.md` §8.5). Rows are therefore
sent through a thread pool, and **one process-wide semaphore** caps how many creates are
in flight across *all* batches (`UPSTREAM_CONCURRENCY`, default 10; `1` = sequential).

| 20 rows at ~5.9 s per create | Time |
| --- | --- |
| One at a time | ~118 s |
| 10 at a time (default) | ~12 s |

Each record stores `upstream_ms`; `python3 scripts/batch_timing.py <batch_id>` prints
per-row timing and the speed-up for a stored batch.

### Upstream resilience

| Failure | Retried right away? | Row afterwards |
| --- | --- | --- |
| 4xx (rejected data) | No | `failed`, not retryable: needs corrected data in a new upload |
| 500, 503, 429 (`Retry-After` honoured, max 30 s) | Yes, with backoff + jitter (`UPSTREAM_RETRIES`) | `failed`, retryable |
| Connection refused (request never sent) | Yes | `failed`, retryable |
| Timeout / connection dropped / gateway 502 or 504 on a **create** | **No**: it may have been applied | `failed`, retryable, *outcome unknown* |

A gateway 502/504 comes from the proxy in front of the upstream (Render), which may
answer after the app behind it already saved the hospital. A timed-out create is not re-sent blindly, because that could create a duplicate (the
upstream has no idempotency key). Resume checks it against the upstream first.

**Sleeping upstream:** the upstream is a free Render instance that sleeps when idle and
takes ~30–60 s to answer its first request. Before sending any create, the app calls
`GET /` once with a 60 s timeout (`UPSTREAM_WARMUP_SECONDS`). If upstream still doesn't
answer, nothing is sent: the batch ends `interrupted` with "…may be waking up… Resume
this batch in a minute", and every row stays `pending` for resume.

**Outage stop:** after `UPSTREAM_OUTAGE_THRESHOLD` (default 5) consecutive transient
failures, no new rows are started. Rows already in flight finish, the rest stay `pending`,
and the batch ends `interrupted`, so it doesn't burn through every row while the
upstream is down.

### Resume

`POST /hospitals/bulk/{batch_id}/resume[?background=true]` (or the **Resume** button in
the UI) continues the **same batch**:

1. Rows with an unknown outcome (a timed-out create, or a row left `processing` by a
   crash) are looked up with `GET /hospitals/batch/{batch_id}`: if found they become
   `created` with their hospital id, otherwise they are sent again.
2. `pending` rows and retryable failures are sent; created rows are never re-sent.
3. The usual activation rule runs (the upload's `activate_on_partial` choice is kept).

It returns 409 while the batch is processing or when nothing is retryable (rows the
upstream *rejected* need corrected data in a new upload), and 502 if the upstream lookup
fails. On startup, any batch still marked `processing` lost its runner (single process),
so it is marked `interrupted` and can be resumed.

## Configuration (env vars)

| Variable | Default |
| --- | --- |
| `UPSTREAM_BASE_URL` | `https://hospital-directory.onrender.com` |
| `DATA_DIR` | `data` |
| `MAX_ROWS` | `20` (`0` = no limit) |
| `MAX_FILE_BYTES` | `1000000` |
| `UPSTREAM_TIMEOUT_SECONDS` | `30` |
| `UPSTREAM_RETRIES` | `2` |
| `UPSTREAM_WARMUP_SECONDS` | `60` (wait for upstream `GET /` before sending creates; `0` = skip) |
| `UPSTREAM_RETRY_BACKOFF_SECONDS` | `0.5` |
| `UPSTREAM_CONCURRENCY` | `10` (creates in flight per process; `1` = sequential) |
| `UPSTREAM_OUTAGE_THRESHOLD` | `5` (consecutive transient failures before stopping; `0` = off) |
| `ENABLE_HARD_CLEAN` | `false`: the endpoint returns 403 and the UI hides the button. Set `true` to enable (local development only) |

## Layout

```
app/
  main.py            routes and dependency wiring
  csv_validation.py  CSV parsing + validation
  client.py          upstream HTTP client with retries
  service.py         orchestration: run_batch (concurrent), resume, activation, hard clean
  repository.py      JobRepository interface + JSON-file implementation
  models.py          pydantic models (Job, Record, API responses)
  config.py          env-based settings
tests/               unit + API tests with a fake upstream (no network)
scripts/
  fake_upstream.py   local stand-in for the upstream (FAKE_UPSTREAM_DELAY to slow it)
  batch_timing.py    per-row timing for a stored batch
samples/             example CSVs (good, invalid rows, 20 and 500 rows)
docs/requirements.md the brief's requirements and assumptions
Dockerfile, docker-compose.yml, render.yaml
```

## Design notes

- See [`DESIGN.md`](DESIGN.md) for the patterns used, every decision taken (with
  reasons), the measurements behind them, and how this scales; the other documents
  are listed under [Documentation and artifacts](#documentation-and-artifacts).
- `service.run_batch` is the single place that decides how rows are sent upstream, and is
  shared by the first run and by resume.
