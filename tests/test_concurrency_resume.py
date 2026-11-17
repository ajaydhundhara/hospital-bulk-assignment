"""D23 bounded concurrency, D22 create-retry rules, and D21 resume."""
import threading
import time

import httpx

from app.client import HospitalDirectoryClient
from app.csv_validation import parse_csv
from app.service import mark_interrupted_jobs, run_batch, start_batch

from .conftest import csv_file

GOOD = "name,address,phone\nGeneral,1 Main St,555-1\nCity,2 Oak Ave,\nCounty,3 Elm Rd,555-3\n"


def _rows(n):
    return "name,address\n" + "".join(f"H{i:02d},addr {i}\n" for i in range(1, n + 1))


def _posts(upstream):
    return sum(1 for c in upstream.calls if c == ("POST", "/hospitals/"))


# --- D23: bounded concurrency ----------------------------------------------------------


def test_rows_are_sent_concurrently_up_to_the_limit(client, upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "5")
    upstream.delay = 0.05
    body = client.post("/hospitals/bulk", files=csv_file(_rows(20))).json()
    assert upstream.max_in_flight == 5
    assert body["status"] == "completed" and body["processed_hospitals"] == 20
    # created in any order, reported in row order
    assert [h["row"] for h in body["hospitals"]] == list(range(1, 21))
    assert all(h["status"] == "created_and_activated" for h in body["hospitals"])


def test_concurrency_1_is_sequential(client, upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "1")
    upstream.delay = 0.01
    client.post("/hospitals/bulk", files=csv_file(_rows(5)))
    assert upstream.max_in_flight == 1
    assert [h["name"] for h in upstream.created] == [f"H{i:02d}" for i in range(1, 6)]


def test_limit_is_shared_by_concurrent_batches(upstream, repo):
    """Two batches at once never exceed the process-wide limit."""
    upstream.delay = 0.05
    limiter = threading.BoundedSemaphore(3)
    rows = parse_csv(_rows(9).encode(), max_rows=0, max_bytes=100_000).rows

    def run():
        c = HospitalDirectoryClient("http://u", retries=0, transport=httpx.MockTransport(upstream))
        try:
            job = start_batch(repo, rows)
            run_batch(c, repo, job.batch_id, concurrency=3, limiter=limiter)
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert upstream.max_in_flight == 3
    assert len(upstream.created) == 18


def test_job_counters_are_exact_under_concurrency(client, upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "10")
    upstream.fail_names.update({"H03": 422, "H07": 422})
    bid = client.post("/hospitals/bulk", files=csv_file(_rows(20))).json()["batch_id"]
    job = client.get(f"/hospitals/bulk/{bid}/job").json()
    assert (job["created_count"], job["failed_count"]) == (18, 2)


# --- Outage stop ----------------------------------------------------------------------


def test_upstream_outage_stops_the_batch_as_interrupted(client, upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "1")
    monkeypatch.setenv("UPSTREAM_OUTAGE_THRESHOLD", "2")
    upstream.fail_names.update({f"H{i:02d}": 503 for i in range(1, 7)})
    body = client.post("/hospitals/bulk", files=csv_file(_rows(6))).json()
    assert body["status"] == "interrupted"
    assert "consecutive upstream failures" in body["error"]
    assert [h["status"] for h in body["hospitals"]] == ["failed"] * 2 + ["pending"] * 4
    assert body["resumable_rows"] == 6 and body["batch_activated"] is False
    assert upstream.activated == []
    assert _posts(upstream) == 2 * 2  # two rows tried (each with 1 retry), four never sent


def test_outage_after_every_row_was_tried_is_a_normal_finish(client, upstream, monkeypatch):
    """Found in manual testing: rows 1-3 created, then upstream dies. The stop fires but
    nothing is left unsent, so it's an ordinary partial result (activation rule applies)."""
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "1")
    monkeypatch.setenv("UPSTREAM_OUTAGE_THRESHOLD", "2")
    upstream.fail_names.update({"H04": 503, "H05": 503})
    body = client.post(
        "/hospitals/bulk?activate_on_partial=true", files=csv_file(_rows(5))
    ).json()
    assert body["status"] == "partial" and body["error"] is None
    assert body["batch_activated"] is True  # every created row is active
    assert upstream.activated == [body["batch_id"]] and body["resumable_rows"] == 2


def test_rejections_do_not_count_as_an_outage(client, upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_OUTAGE_THRESHOLD", "2")
    upstream.fail_names.update({"H01": 422, "H02": 422, "H03": 422})
    body = client.post("/hospitals/bulk", files=csv_file(_rows(5))).json()
    assert body["status"] == "partial" and body["processed_hospitals"] == 2


# --- D22: what a failed create may retry -----------------------------------------------


def test_create_timeout_is_not_retried_and_marked_unknown(client, upstream, repo):
    upstream.raise_on["City"] = httpx.ReadTimeout("slow")
    body = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    city_posts = [c for c in upstream.calls if c == ("POST", "/hospitals/")]
    assert len(city_posts) == 3  # one per row: the timed-out create was not re-sent
    city = repo.list_records(body["batch_id"])[1]
    assert city.status == "failed" and city.retryable and city.outcome_unknown
    assert "outcome unknown" in city.error
    assert body["hospitals"][1]["retryable"] is True and body["resumable_rows"] == 1


def test_create_that_never_left_is_retried(client, upstream):
    upstream.raise_on["City"] = httpx.ConnectError("refused")
    client.post("/hospitals/bulk", files=csv_file(GOOD))
    assert _posts(upstream) == 4  # City sent twice (1 retry): safe, it never reached upstream


def test_rejected_row_is_not_retryable(client, upstream):
    upstream.fail_names["City"] = 422
    body = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    assert body["hospitals"][1]["retryable"] is False and body["resumable_rows"] == 0


# --- D21: resume ----------------------------------------------------------------------


def test_resume_finishes_an_interrupted_batch(client, upstream, monkeypatch):
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "1")
    monkeypatch.setenv("UPSTREAM_OUTAGE_THRESHOLD", "2")
    upstream.fail_names.update({f"H{i:02d}": 503 for i in range(1, 7)})
    bid = client.post("/hospitals/bulk", files=csv_file(_rows(6))).json()["batch_id"]

    upstream.fail_names.clear()  # upstream is back
    r = client.post(f"/hospitals/bulk/{bid}/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed" and body["batch_activated"] is True
    assert body["error"] is None and body["resumable_rows"] == 0
    assert len(upstream.created) == 6 and upstream.activated == [bid]
    job = client.get(f"/hospitals/bulk/{bid}/job").json()
    assert job["resume_count"] == 1 and (job["created_count"], job["failed_count"]) == (6, 0)


def test_resume_never_resends_created_rows(client, upstream):
    upstream.fail_names["City"] = 503
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    assert [h["name"] for h in upstream.created] == ["General", "County"]

    upstream.fail_names.clear()
    before = _posts(upstream)
    body = client.post(f"/hospitals/bulk/{bid}/resume").json()
    assert _posts(upstream) - before == 1  # only City
    assert body["status"] == "completed"
    assert sorted(h["name"] for h in upstream.created) == ["City", "County", "General"]


def test_resume_finds_a_row_whose_response_was_lost(client, upstream, repo):
    """The create succeeded upstream but we never heard back: no duplicate on resume."""
    upstream.lost_response.add("City")
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    assert repo.list_records(bid)[1].outcome_unknown

    before = _posts(upstream)
    body = client.post(f"/hospitals/bulk/{bid}/resume").json()
    assert _posts(upstream) == before  # found upstream, nothing re-sent
    assert ("GET", f"/hospitals/batch/{bid}") in upstream.calls
    assert len(upstream.created) == 3  # no duplicate
    city = body["hospitals"][1]
    assert city["status"] == "created_and_activated"
    assert city["hospital_id"] == next(h["id"] for h in upstream.created if h["name"] == "City")
    assert body["status"] == "completed"


def test_gateway_timeout_after_create_is_not_duplicated(client, upstream, repo):
    """Upstream created the row but the proxy answered 504: no retry, resume finds it."""
    upstream.gateway_error_after_create.add("City")
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    city = repo.list_records(bid)[1]
    assert city.status == "failed" and city.outcome_unknown
    assert len(upstream.created) == 3  # created once, not re-sent

    upstream.gateway_error_after_create.clear()
    before = _posts(upstream)
    body = client.post(f"/hospitals/bulk/{bid}/resume").json()
    assert _posts(upstream) == before and len(upstream.created) == 3
    assert body["status"] == "completed"


def test_sleeping_upstream_sends_nothing_and_batch_can_be_resumed(client, upstream, repo):
    upstream.asleep = True
    body = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    assert body["status"] == "interrupted"
    assert "waking up" in body["error"] and "Resume" in body["error"]
    assert _posts(upstream) == 0 and body["resumable_rows"] == 3
    assert {r.status for r in repo.list_records(body["batch_id"])} == {"pending"}

    upstream.asleep = False
    resumed = client.post(f"/hospitals/bulk/{body['batch_id']}/resume").json()
    assert resumed["status"] == "completed" and len(upstream.created) == 3


def test_upstream_is_woken_once_before_creates(client, upstream):
    client.post("/hospitals/bulk", files=csv_file(GOOD))
    gets = [i for i, c in enumerate(upstream.calls) if c == ("GET", "/")]
    first_post = next(i for i, c in enumerate(upstream.calls) if c[0] == "POST")
    assert gets == [0] and first_post == 1


def test_resume_resends_an_unknown_row_not_found_upstream(client, upstream):
    upstream.raise_on["City"] = httpx.ReadTimeout("slow")  # never reached upstream
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    upstream.raise_on.clear()
    body = client.post(f"/hospitals/bulk/{bid}/resume").json()
    assert body["status"] == "completed" and len(upstream.created) == 3


def test_resume_after_a_crash_left_a_row_processing(client, upstream, repo, monkeypatch):
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "1")
    upstream.crash_on = "City"
    try:
        client.post("/hospitals/bulk", files=csv_file(GOOD))
    except RuntimeError:
        pass
    (job,) = repo.list_jobs()
    assert job.status == "failed" and job.resumable_rows == 2  # City (processing) + County

    upstream.crash_on = None
    body = client.post(f"/hospitals/bulk/{job.batch_id}/resume").json()
    assert body["status"] == "completed" and len(upstream.created) == 3


def test_resume_keeps_the_uploads_activate_on_partial(client, upstream):
    upstream.fail_names["City"] = 503
    bid = client.post(
        "/hospitals/bulk?activate_on_partial=true", files=csv_file(GOOD)
    ).json()["batch_id"]
    assert upstream.activated == [bid]  # partial, activated anyway

    upstream.fail_names.clear()
    body = client.post(f"/hospitals/bulk/{bid}/resume").json()
    assert body["status"] == "completed" and body["batch_activated"] is True
    assert upstream.activated == [bid, bid]  # activated again to cover the new row


def test_resume_in_background(client, upstream):
    upstream.fail_names["City"] = 503
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    upstream.fail_names.clear()
    r = client.post(f"/hospitals/bulk/{bid}/resume?background=true")
    assert r.status_code == 202 and r.json()["status"] == "processing"
    assert client.get(f"/hospitals/bulk/{bid}").json()["status"] == "completed"


def test_resume_refused_when_only_rejected_rows_remain(client, upstream):
    upstream.fail_names["City"] = 422
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    r = client.post(f"/hospitals/bulk/{bid}/resume")
    assert r.status_code == 409 and "corrected data" in r.json()["detail"]


def test_resume_refused_while_processing(client, repo):
    rows = parse_csv(GOOD.encode(), max_rows=0, max_bytes=10_000).rows
    job = start_batch(repo, rows)  # stays `processing`
    r = client.post(f"/hospitals/bulk/{job.batch_id}/resume")
    assert r.status_code == 409 and "still processing" in r.json()["detail"]


def test_resume_unknown_batch_404(client):
    assert client.post("/hospitals/bulk/00000000-0000-0000-0000-000000000000/resume").status_code == 404


def test_resume_reports_502_when_upstream_check_fails(client, upstream):
    upstream.lost_response.add("City")
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    upstream.get_batch_status = 500
    r = client.post(f"/hospitals/bulk/{bid}/resume")
    assert r.status_code == 502
    # nothing changed: the batch can be resumed again later
    assert client.get(f"/hospitals/bulk/{bid}").json()["status"] == "partial"


def test_startup_sweep_marks_orphaned_runs_interrupted(client, upstream, repo):
    rows = parse_csv(GOOD.encode(), max_rows=0, max_bytes=10_000).rows
    job = start_batch(repo, rows)  # as if the server died mid-run
    assert mark_interrupted_jobs(repo) == 1
    swept = repo.get_job(job.batch_id)
    assert swept.status == "interrupted" and swept.resumable_rows == 3 and "restarted" in swept.error

    body = client.post(f"/hospitals/bulk/{job.batch_id}/resume").json()
    assert body["status"] == "completed" and len(upstream.created) == 3


def test_processing_time_after_resume_covers_the_latest_run(client, upstream, repo):
    from datetime import timedelta

    upstream.fail_names["City"] = 503
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    # pretend the batch sat stopped for an hour before being resumed
    hour_ago = repo.get_job(bid).started_at - timedelta(hours=1)
    repo.update_job(bid, started_at=hour_ago, created_at=hour_ago)

    upstream.fail_names.clear()
    body = client.post(f"/hospitals/bulk/{bid}/resume").json()
    assert body["processing_time_seconds"] < 60  # the resume run, not the hour of waiting
    job = repo.get_job(bid)
    assert job.created_at == hour_ago and job.started_at > hour_ago


def test_progress_is_visible_while_a_batch_runs(client, upstream, repo):
    """Polling mid-run: partial counts, and resume/activate refuse until it finishes."""
    upstream.delay = 0.05
    job = start_batch(repo, parse_csv(_rows(6).encode(), 20, 10**6).rows)
    c = HospitalDirectoryClient("http://u", retries=0, transport=httpx.MockTransport(upstream))
    runner = threading.Thread(target=run_batch, args=(c, repo, job.batch_id), kwargs={"concurrency": 1})
    runner.start()
    try:
        deadline = time.monotonic() + 5
        while repo.get_job(job.batch_id).created_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        mid = client.get(f"/hospitals/bulk/{job.batch_id}").json()
        assert mid["status"] == "processing"
        assert 2 <= mid["processed_hospitals"] < 6
        assert client.post(f"/hospitals/bulk/{job.batch_id}/resume").status_code == 409
        assert client.post(f"/hospitals/bulk/{job.batch_id}/activate").status_code == 409
    finally:
        runner.join()
        c.close()
    assert client.get(f"/hospitals/bulk/{job.batch_id}").json()["status"] == "completed"
