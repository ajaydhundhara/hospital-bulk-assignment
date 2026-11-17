import pytest

from .conftest import csv_file

GOOD = "name,address,phone\nGeneral,1 Main St,555-1\nCity,2 Oak Ave,\nCounty,3 Elm Rd,555-3\n"


def test_health(client):
    assert client.get("/").json() == {"status": "ok"}


def test_bulk_success(client, upstream):
    r = client.post("/hospitals/bulk", files=csv_file(GOOD))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert (body["total_hospitals"], body["processed_hospitals"], body["failed_hospitals"]) == (3, 3, 0)
    assert body["batch_activated"] is True
    assert [h["row"] for h in body["hospitals"]] == [1, 2, 3]
    assert all(h["status"] == "created_and_activated" for h in body["hospitals"])
    # every hospital carried the same batch id, activation called once for it
    assert {h["creation_batch_id"] for h in upstream.created} == {body["batch_id"]}
    assert upstream.activated == [body["batch_id"]]
    assert upstream.created[1]["phone"] is None


def test_validation_failure_makes_no_upstream_calls(client, upstream):
    # the only row is invalid: nothing can be processed, so the file is rejected
    r = client.post("/hospitals/bulk", files=csv_file("name,address\nA,\n"))
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert errors[0]["message"] == "No valid rows: every row has a problem"
    assert {"row": 1, "field": "address", "message": "'address' is required"} in errors
    assert upstream.calls == []


def test_invalid_rows_are_recorded_and_valid_rows_processed(client, upstream):
    text = "name,address,phone\nGeneral,1 Main St,555-1\nNo Address,,555-2\nCounty,3 Elm Rd,\n"
    body = client.post("/hospitals/bulk", files=csv_file(text)).json()
    assert body["status"] == "partial"
    assert (body["total_hospitals"], body["processed_hospitals"], body["failed_hospitals"]) == (3, 2, 1)
    bad = body["hospitals"][1]
    assert bad["status"] == "invalid" and bad["retryable"] is False and bad["hospital_id"] is None
    assert bad["error"] == "Row data is not correct: 'address' is required"
    assert [h["name"] for h in upstream.created] == ["General", "County"]  # never sent
    # an invalid row is a failure, so the batch is not activated by default (D2)
    assert body["batch_activated"] is False and upstream.activated == []
    # and resume can't fix it: the data must be corrected
    r = client.post(f"/hospitals/bulk/{body['batch_id']}/resume")
    assert r.status_code == 409 and "corrected data" in r.json()["detail"]


def test_invalid_rows_with_activate_on_partial(client, upstream):
    text = "name,address\nGeneral,1 Main St\n,2 Oak Ave\n"
    body = client.post("/hospitals/bulk?activate_on_partial=true", files=csv_file(text)).json()
    assert body["status"] == "partial" and body["batch_activated"] is True
    assert [h["status"] for h in body["hospitals"]] == ["created_and_activated", "invalid"]


def test_ignored_columns_are_reported(client, upstream):
    text = "name,address,email\nGeneral,1 Main St,g@x.org\n"
    body = client.post("/hospitals/bulk", files=csv_file(text)).json()
    assert body["status"] == "completed" and body["warnings"] == ["Ignored column(s): email"]
    assert "email" not in upstream.created[0]


def test_partial_failure_keeps_successes_and_reports_error(client, upstream):
    upstream.fail_names["City"] = 422
    body = client.post(
        "/hospitals/bulk?activate_on_partial=true", files=csv_file(GOOD)
    ).json()
    assert body["status"] == "partial"
    assert (body["processed_hospitals"], body["failed_hospitals"]) == (2, 1)
    assert body["batch_activated"] is True
    by_row = {h["row"]: h for h in body["hospitals"]}
    assert by_row[2]["status"] == "failed" and "simulated failure" in by_row[2]["error"]
    assert by_row[1]["status"] == "created_and_activated"
    assert len(upstream.created) == 2


def test_partial_failure_is_not_activated_by_default(client, upstream):
    upstream.fail_names["City"] = 422
    body = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    assert body["status"] == "partial"
    assert body["batch_activated"] is False and upstream.activated == []
    assert {h["status"] for h in body["hospitals"]} == {"created", "failed"}
    assert (body["processed_hospitals"], body["failed_hospitals"]) == (2, 1)


def _rows(n):
    return "name,address\n" + "".join(f"H{i},addr\n" for i in range(n))


def test_default_row_limit_is_20(client, upstream):
    body = client.post("/hospitals/bulk", files=csv_file(_rows(20))).json()
    assert body["total_hospitals"] == 20 and body["status"] == "completed"

    r = client.post("/hospitals/bulk", files=csv_file(_rows(21)))
    assert r.status_code == 422
    assert "maximum is 20" in r.json()["detail"]["errors"][0]["message"]
    assert len(upstream.created) == 20  # the rejected file made no upstream calls


def test_row_limit_is_configurable(client, upstream, monkeypatch):
    monkeypatch.setenv("MAX_ROWS", "0")  # 0 = unlimited
    body = client.post("/hospitals/bulk", files=csv_file(_rows(50))).json()
    assert body["total_hospitals"] == 50 and body["status"] == "completed"


def test_all_rows_fail(client, upstream):
    for n in ("General", "City", "County"):
        upstream.fail_names[n] = 400
    body = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    assert body["status"] == "failed" and body["batch_activated"] is False
    assert upstream.activated == []


def test_transient_5xx_is_retried_then_reported(client, upstream):
    upstream.fail_names["City"] = 503
    body = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    city_calls = [c for c in upstream.calls if c == ("POST", "/hospitals/")]
    assert len(city_calls) == 2 + 2  # General + County once, City twice (1 retry)
    assert body["hospitals"][1]["status"] == "failed"
    assert "503" in body["hospitals"][1]["error"]


def test_activation_failure_reported(client, upstream):
    upstream.activate_status = 400
    body = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    assert body["batch_activated"] is False and body["status"] == "partial"
    assert "activation_error" in body and body["activation_error"]
    assert {h["status"] for h in body["hospitals"]} == {"created"}


def test_validate_endpoint(client, upstream):
    ok = client.post("/hospitals/bulk/validate", files=csv_file(GOOD)).json()
    assert ok == {
        "valid": True, "total_rows": 3, "processable_rows": 3, "invalid_rows": 0,
        "errors": [], "warnings": [],
    }
    mixed = client.post(
        "/hospitals/bulk/validate", files=csv_file("name,address\nA,1\nB,\n")
    ).json()
    assert mixed["valid"] is True and (mixed["processable_rows"], mixed["invalid_rows"]) == (1, 1)
    assert mixed["errors"] == [{"row": 2, "field": "address", "message": "'address' is required"}]
    bad = client.post("/hospitals/bulk/validate", files=csv_file("name\nA\n")).json()
    assert bad["valid"] is False
    assert upstream.calls == []


def test_info_endpoint(client):
    assert client.get("/info").json() == {
        "max_rows": 20, "upstream_concurrency": 10, "hard_clean_enabled": False,
    }


@pytest.fixture
def hard_clean_on(monkeypatch):
    monkeypatch.setenv("ENABLE_HARD_CLEAN", "true")


def test_hard_clean_is_off_by_default(client, upstream):
    r = client.delete("/hospitals/bulk?confirm=true")
    assert r.status_code == 403 and upstream.calls == []


@pytest.mark.parametrize("value", ["false", "0", "", "flase", "maybe"])
def test_hard_clean_stays_off_unless_explicitly_enabled(client, upstream, monkeypatch, value):
    monkeypatch.setenv("ENABLE_HARD_CLEAN", value)
    assert client.get("/info").json()["hard_clean_enabled"] is False
    assert client.delete("/hospitals/bulk?confirm=true").status_code == 403
    assert upstream.calls == []


@pytest.mark.parametrize("value", ["true", "TRUE", " yes ", "1", "on"])
def test_hard_clean_enabled_values(client, monkeypatch, value):
    monkeypatch.setenv("ENABLE_HARD_CLEAN", value)
    assert client.get("/info").json()["hard_clean_enabled"] is True


def test_missing_file_is_422(client):
    assert client.post("/hospitals/bulk").status_code == 422


def test_get_result_roundtrip_and_404(client):
    posted = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()
    fetched = client.get(f"/hospitals/bulk/{posted['batch_id']}").json()
    assert fetched == posted
    assert client.get("/hospitals/bulk/nope").status_code == 404
    assert client.get("/hospitals/bulk/00000000-0000-0000-0000-000000000000").status_code == 404


def test_rows_are_stored_with_full_data_and_state(client):
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    records = client.get(f"/hospitals/bulk/{bid}/records").json()
    assert [r["name"] for r in records] == ["General", "City", "County"]
    # address/phone are retained (needed later to retry failed rows)
    assert records[0]["address"] == "1 Main St" and records[0]["phone"] == "555-1"
    assert records[1]["phone"] is None
    assert all(r["status"] == "created_and_activated" and r["hospital_id"] for r in records)
    assert all(r["attempts"] == 1 for r in records)


def test_failed_rows_stored_with_error_and_filterable(client, upstream):
    upstream.fail_names["City"] = 422
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    failed = client.get(f"/hospitals/bulk/{bid}/records?status=failed").json()
    assert len(failed) == 1
    assert failed[0]["name"] == "City" and failed[0]["address"] == "2 Oak Ave"
    assert "simulated failure" in failed[0]["error"] and failed[0]["hospital_id"] is None


def test_job_record_counters_and_timestamps(client, upstream):
    upstream.fail_names["City"] = 422
    r = client.post(
        "/hospitals/bulk?activate_on_partial=true", files=csv_file(GOOD, name="march.csv")
    )
    job = client.get(f"/hospitals/bulk/{r.json()['batch_id']}/job").json()
    assert job["filename"] == "march.csv"
    assert (job["total_rows"], job["created_count"], job["failed_count"]) == (3, 2, 1)
    assert job["status"] == "partial" and job["activated"] is True
    assert job["started_at"] and job["finished_at"]


def test_validation_failure_stores_nothing(client, repo, tmp_path):
    client.post("/hospitals/bulk", files=csv_file("name,address\nA,\n"))
    assert not (tmp_path / "jobs").exists()


def test_records_unknown_batch_404(client):
    assert client.get("/hospitals/bulk/nope/records").status_code == 404
    assert client.get("/hospitals/bulk/nope/job").status_code == 404


def test_unexpected_error_marks_job_failed_not_stuck_processing(client, upstream, repo, monkeypatch):
    monkeypatch.setenv("UPSTREAM_CONCURRENCY", "1")  # sequential, so the state is exact
    upstream.crash_on = "City"  # row 2 hits a bug after row 1 succeeded
    with pytest.raises(RuntimeError):
        client.post("/hospitals/bulk", files=csv_file(GOOD))
    (job_dir,) = repo._root.iterdir()
    job = repo.get_job(job_dir.name)
    assert job.status == "failed" and "kaboom" in job.error and job.finished_at
    # state at the moment of the crash was persisted
    assert [r.status for r in repo.list_records(job.batch_id)] == [
        "created",
        "processing",
        "pending",
    ]


# --- UI support: list, background processing, manual activation ---------------------


def test_list_batches_newest_first(client):
    first = client.post("/hospitals/bulk", files=csv_file(GOOD, "a.csv")).json()["batch_id"]
    second = client.post("/hospitals/bulk", files=csv_file(GOOD, "b.csv")).json()["batch_id"]
    jobs = client.get("/hospitals/bulk").json()
    assert [j["batch_id"] for j in jobs] == [second, first]
    assert jobs[0]["filename"] == "b.csv" and jobs[0]["status"] == "completed"


def test_list_batches_empty(client):
    assert client.get("/hospitals/bulk").json() == []


def test_background_upload_returns_202_then_finishes(client, upstream):
    r = client.post("/hospitals/bulk?background=true", files=csv_file(GOOD))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "processing" and body["total_hospitals"] == 3
    # TestClient runs background tasks before returning, so the run is done now
    final = client.get(f"/hospitals/bulk/{body['batch_id']}").json()
    assert final["status"] == "completed" and final["processed_hospitals"] == 3
    assert upstream.activated == [body["batch_id"]]


def test_activate_partial_batch(client, upstream):
    upstream.fail_names["City"] = 422
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    r = client.post(f"/hospitals/bulk/{bid}/activate")
    assert r.status_code == 200
    body = r.json()
    assert body["batch_activated"] is True and body["status"] == "partial"
    assert {h["status"] for h in body["hospitals"]} == {"created_and_activated", "failed"}
    assert upstream.activated == [bid]
    # second attempt is rejected
    assert client.post(f"/hospitals/bulk/{bid}/activate").status_code == 409


def test_activate_after_activation_failure_completes_batch(client, upstream):
    upstream.activate_status = 500
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    upstream.activate_status = 200
    body = client.post(f"/hospitals/bulk/{bid}/activate").json()
    assert body["status"] == "completed" and body["activation_error"] is None


def test_activate_upstream_error_is_reported(client, upstream):
    upstream.fail_names["City"] = 422
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    upstream.activate_status = 500
    body = client.post(f"/hospitals/bulk/{bid}/activate").json()
    assert body["batch_activated"] is False and "500" in body["activation_error"]


def test_activate_rejects_batch_with_nothing_created(client, upstream):
    upstream.fail_names.update({"General": 422, "City": 422, "County": 422})
    bid = client.post("/hospitals/bulk", files=csv_file(GOOD)).json()["batch_id"]
    assert client.post(f"/hospitals/bulk/{bid}/activate").status_code == 409


def test_activate_unknown_batch_404(client):
    r = client.post("/hospitals/bulk/00000000-0000-0000-0000-000000000000/activate")
    assert r.status_code == 404


def test_ui_page_served(client):
    r = client.get("/ui")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


# --- Hard clean -----------------------------------------------------------------------


def _upload(client, text=GOOD, query=""):
    return client.post("/hospitals/bulk" + query, files=csv_file(text)).json()["batch_id"]


def test_hard_clean_removes_upstream_records_and_local_state(hard_clean_on, client, upstream, repo, tmp_path):
    upstream.fail_names["City"] = 422
    a = _upload(client)
    b = _upload(client, query="?activate_on_partial=true")
    created_upstream = len(upstream.created)  # 2 + 2
    assert created_upstream == 4

    r = client.delete("/hospitals/bulk?confirm=true")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "batches_deleted": 2,
        "hospitals_deleted": 4,
        "orphans_removed": 0,
        "failed": [],
        "clean": True,
    }
    assert upstream.created == [] and set(upstream.deleted_batches) == {a, b}
    assert client.get("/hospitals/bulk").json() == []
    assert client.get(f"/hospitals/bulk/{a}").status_code == 404
    assert not (tmp_path / "jobs").exists()  # nothing left on disk


def test_hard_clean_requires_confirm(hard_clean_on, client, upstream):
    _upload(client)
    r = client.delete("/hospitals/bulk")
    assert r.status_code == 400
    assert len(client.get("/hospitals/bulk").json()) == 1 and upstream.deleted_batches == []


def test_hard_clean_on_empty_state(hard_clean_on, client):
    body = client.delete("/hospitals/bulk?confirm=true").json()
    assert body["batches_deleted"] == 0 and body["clean"] is True


def test_hard_clean_keeps_batch_whose_upstream_delete_failed(hard_clean_on, client, upstream):
    first, second = _upload(client), _upload(client)  # listing is newest first: second, first
    upstream.delete_queue = [400]  # the first DELETE call (newest batch) is refused
    body = client.delete("/hospitals/bulk?confirm=true").json()
    assert body["clean"] is False and body["batches_deleted"] == 1
    assert [f["batch_id"] for f in body["failed"]] == [second]
    assert "delete refused" in body["failed"][0]["error"]
    # the failed batch is still there (so nothing upstream is orphaned) and can be retried
    assert [j["batch_id"] for j in client.get("/hospitals/bulk").json()] == [second]
    retry = client.delete("/hospitals/bulk?confirm=true").json()
    assert retry["clean"] is True and retry["batches_deleted"] == 1
    assert client.get("/hospitals/bulk").json() == []


def test_hard_clean_treats_upstream_404_as_already_gone(hard_clean_on, client, upstream):
    _upload(client)
    upstream.delete_queue = [404]
    body = client.delete("/hospitals/bulk?confirm=true").json()
    assert body["clean"] is True and body["batches_deleted"] == 1
    assert client.get("/hospitals/bulk").json() == []


def test_hard_clean_blocked_while_a_batch_is_processing(hard_clean_on, client, upstream, repo):
    from app.csv_validation import parse_csv
    from app.service import start_batch

    rows = parse_csv(GOOD.encode(), max_rows=0, max_bytes=10_000).rows
    job = start_batch(repo, rows, filename="running.csv")  # stays `processing`
    done = _upload(client)

    r = client.delete("/hospitals/bulk?confirm=true")
    assert r.status_code == 409 and "still processing" in r.json()["detail"]
    assert len(client.get("/hospitals/bulk").json()) == 2 and upstream.deleted_batches == []

    forced = client.delete("/hospitals/bulk?confirm=true&force=true").json()
    assert forced["batches_deleted"] == 2 and forced["clean"] is True
    assert set(upstream.deleted_batches) == {job.batch_id, done}
    assert client.get("/hospitals/bulk").json() == []


def test_hard_clean_removes_orphan_directories(hard_clean_on, client, repo, tmp_path):
    import uuid

    orphan = tmp_path / "jobs" / str(uuid.uuid4())
    orphan.mkdir(parents=True)
    (orphan / "records.json").write_text("[]")  # crashed before job.json was written
    body = client.delete("/hospitals/bulk?confirm=true").json()
    assert body["orphans_removed"] == 1
    assert not (tmp_path / "jobs").exists()


def test_failed_rows_csv_can_be_fixed_and_uploaded_again(client, upstream):
    upstream.fail_names["Bad"] = 422
    text = "name,address,phone\nGood,1 Main St,555\nBad,2 Oak Ave,\n,3 Elm Rd,\n"
    bid = client.post("/hospitals/bulk", files=csv_file(text)).json()["batch_id"]
    r = client.get(f"/hospitals/bulk/{bid}/failed.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "name,address,phone,error"
    assert [l.split(",")[0] for l in lines[1:]] == ["Bad", ""]  # failed + invalid, not Good

    upstream.fail_names.clear()
    fixed = r.text.replace(",3 Elm Rd,", "Fixed,3 Elm Rd,")
    again = client.post("/hospitals/bulk", files=csv_file(fixed)).json()
    assert again["status"] == "completed" and again["processed_hospitals"] == 2
    assert any("error" in w for w in again["warnings"])  # the error column is ignored


def test_failed_rows_csv_unknown_batch_is_404(client):
    assert client.get("/hospitals/bulk/nope/failed.csv").status_code == 404
