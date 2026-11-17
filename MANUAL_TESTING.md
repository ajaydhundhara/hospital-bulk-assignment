# Manual Testing

Uses sample files in `samples/` and a fake upstream (`scripts/fake_upstream.py`) that
rejects any hospital whose name contains `FAIL`. The app runs on **8001** (port 8000
is often taken); the fake upstream runs on **9000**.

## Setup: three terminals, from the project root

```bash
# Terminal 1: fake upstream
.venv/bin/uvicorn scripts.fake_upstream:app --port 9000
```

```bash
# Terminal 2: the app, pointed at the fake upstream
UPSTREAM_BASE_URL=http://localhost:9000 ENABLE_HARD_CLEAN=true .venv/bin/uvicorn app.main:app --port 8001
```

Terminal 3 runs the curl commands below. Swagger UI: http://localhost:8001/docs

**Test console:** http://localhost:8001/ui. To see the progress bar move, start the fake
upstream with a delay: `FAKE_UPSTREAM_DELAY=0.3 .venv/bin/uvicorn scripts.fake_upstream:app --port 9000`.
Then upload `samples/thirty_rows.csv` (progress + scrollable list), `samples/partial_failure.csv`
(red rows + *Activate batch*), and `samples/invalid_rows.csv` (valid row created, bad rows marked invalid),
and check the **Batches** tab.

## Scenarios

| # | Scenario | Command | Expect |
| --- | --- | --- | --- |
| 1 | Health | `curl localhost:8001/` | `{"status":"ok"}` |
| 2 | Happy path | `curl -F file=@samples/good.csv localhost:8001/hospitals/bulk` | `status: completed`, 3 processed, 0 failed, `batch_activated: true`, rows `created_and_activated` |
| 3 | Partial failure (default) | `curl -F file=@samples/partial_failure.csv localhost:8001/hospitals/bulk` | `status: partial`, 2 processed, 2 failed, `batch_activated: false`, failed rows carry the upstream error |
| 4 | Partial, force activation | `curl -F file=@samples/partial_failure.csv "localhost:8001/hospitals/bulk?activate_on_partial=true"` | `status: partial`, `batch_activated: true` |
| 5 | Row limit (20) | `curl -i -F file=@samples/thirty_rows.csv localhost:8001/hospitals/bulk` | HTTP 422, "CSV has 30 rows; maximum is 20". Start the app with `MAX_ROWS=0` to lift it |
| 5b | Concurrency | `curl -F file=@samples/twenty_rows.csv localhost:8001/hospitals/bulk` with `FAKE_UPSTREAM_DELAY=0.3` | `completed` in ~0.7 s (vs ~6 s one at a time); `python3 scripts/batch_timing.py <batch_id>` shows the speed-up |
| 6 | Validate only | `curl -F file=@samples/invalid_rows.csv localhost:8001/hospitals/bulk/validate` | `valid: false`, errors for rows 2, 3, 4 |
| 7 | Invalid rows recorded | `curl -i -F file=@samples/invalid_rows.csv localhost:8001/hospitals/bulk` | HTTP 200, `status: partial`: valid rows created, each bad row `invalid` with "Row data is not correct: …"; batch not activated |
| 8 | Missing column | `curl -i -F file=@samples/missing_column.csv localhost:8001/hospitals/bulk` | HTTP 422, "Missing required column(s): address" |
| 9 | No file | `curl -i -X POST localhost:8001/hospitals/bulk` | HTTP 422 from FastAPI |
| 10 | Unknown batch | `curl -i localhost:8001/hospitals/bulk/00000000-0000-0000-0000-000000000000` | HTTP 404 |

## Inspecting stored state (after scenario 3)

Copy the `batch_id` from the response, then:

```bash
BID=<batch_id>
curl localhost:8001/hospitals/bulk/$BID                      # summary + all rows
curl localhost:8001/hospitals/bulk/$BID/job                  # job record, counters, timestamps
curl localhost:8001/hospitals/bulk/$BID/records              # stored rows incl. address/phone
curl "localhost:8001/hospitals/bulk/$BID/records?status=failed"   # only failures + errors
curl localhost:9000/hospitals/batch/$BID                     # what the fake upstream holds (active=false)
ls data/jobs/$BID && cat data/jobs/$BID/records.json         # files on disk
```

## Upstream outage and resume

1. Stop the fake upstream (Ctrl+C in terminal 1) and upload `samples/twenty_rows.csv`.
   After 5 consecutive connection errors the batch stops: `status: interrupted`, an
   `error` saying how many rows were not sent, failed rows `retryable: true`, the rest
   `pending`. Nothing is activated.
2. Start the fake upstream again, then resume:
   `curl -X POST localhost:8001/hospitals/bulk/$BID/resume` (or **Resume** in the Batches
   tab). Expect `completed`, `batch_activated: true`, and exactly 20 hospitals upstream
   (`curl localhost:9000/hospitals/batch/$BID`): created rows are never re-sent.
3. Resume again: HTTP 409 "Nothing to retry."
4. Rows the upstream *rejected* are not retried: upload `samples/partial_failure.csv`, then
   resume. Expect 409, explaining that those rows need corrected data.

Restart check: start a background upload with a slow fake upstream
(`FAKE_UPSTREAM_DELAY=2`), stop the app mid-run, and start it again. The batch shows
`interrupted` and can be resumed.

## Against the real upstream

Restart terminal 2 without `UPSTREAM_BASE_URL`. Calls take about 6 s each, so use
`samples/good.csv`. Clean up afterwards:

```bash
curl -X DELETE https://hospital-directory.onrender.com/hospitals/batch/<batch_id>
```

## Hard clean (reset everything)

Needs the app started with `ENABLE_HARD_CLEAN=true` (as in the setup above).
In the UI: **Batches → Hard clean…**, type `DELETE`, confirm. To see each outcome:

| Scenario | How | Expect |
| --- | --- | --- |
| Normal | Upload a few files, then Hard clean | Toast "Clean: N batch(es), M hospital(s) deleted"; Batches tab empty; `curl localhost:9000/hospitals/` returns `[]`; `data/jobs` gone |
| Confirmation gate | Type `delete` or `DELET` | *Delete everything* stays disabled |
| Upstream down | Upload, stop the fake upstream, Hard clean | Red list of kept batches with the error; nothing deleted; restart upstream, retype `DELETE`, retry succeeds |
| Batch still processing | Start a clean while a background upload runs (or stop the app mid-upload to leave a batch stuck) | Warning "N batch(es) still processing. Nothing was deleted."; button becomes *Force clean anyway* |

With curl:

```bash
curl -X DELETE "localhost:8001/hospitals/bulk"                        # 400: confirm required
curl -X DELETE "localhost:8001/hospitals/bulk?confirm=true"           # clean everything
curl -X DELETE "localhost:8001/hospitals/bulk?confirm=true&force=true" # even stuck batches
```

Against the **real** upstream this deletes your batches' hospitals there too (about 6 s
per batch). It only removes batches this app created.

## Docker

```bash
UPSTREAM_BASE_URL=http://fake-upstream:9000 APP_PORT=8001 ENABLE_HARD_CLEAN=true docker compose --profile fake up --build
```

The scenarios above then work unchanged against `localhost:8001`. Stop with
`docker compose --profile fake down -v` (`-v` also drops the stored batches).

## Reset local state only

Hard clean is the safe way to reset. Deleting the folder alone leaves upstream records
behind, with no way to find them afterwards:

```bash
rm -rf data/
```
