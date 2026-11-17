import threading
import uuid

import pytest

from app.models import Job, Record
from app.repository import JsonFileRepository, utcnow


def make(batch_id=None, n=3):
    batch_id = batch_id or str(uuid.uuid4())
    now = utcnow()
    job = Job(batch_id=batch_id, total_rows=n, created_at=now, started_at=now)
    records = [
        Record(batch_id=batch_id, row=i, name=f"H{i}", address=f"{i} St", updated_at=now)
        for i in range(1, n + 1)
    ]
    return job, records


def test_create_and_read_back(repo):
    job, records = make()
    repo.create_job(job, records)
    assert repo.get_job(job.batch_id) == job
    got = repo.list_records(job.batch_id)
    assert [r.row for r in got] == [1, 2, 3]
    assert got[0].address == "1 St" and got[0].status == "pending"


def test_data_survives_a_new_repository_instance(tmp_path):
    job, records = make()
    JsonFileRepository(tmp_path).create_job(job, records)
    assert JsonFileRepository(tmp_path).get_job(job.batch_id) is not None


def test_update_record_and_filter(repo):
    job, records = make()
    repo.create_job(job, records)
    repo.update_record(job.batch_id, 2, status="failed", error="bad")
    failed = repo.list_records(job.batch_id, status="failed")
    assert [(r.row, r.error) for r in failed] == [(2, "bad")]
    assert repo.list_records(job.batch_id, status="pending")[0].row == 1


def test_update_job_and_transition(repo):
    job, records = make()
    repo.create_job(job, records)
    assert repo.update_job(job.batch_id, created_count=2).created_count == 2
    repo.update_record(job.batch_id, 1, status="created")
    repo.update_record(job.batch_id, 3, status="created")
    assert repo.transition_records(job.batch_id, "created", "created_and_activated") == 2
    assert repo.transition_records(job.batch_id, "created", "x") == 0
    statuses = {r.row: r.status for r in repo.list_records(job.batch_id)}
    assert statuses == {1: "created_and_activated", 2: "pending", 3: "created_and_activated"}


def test_missing_things(repo):
    job, records = make()
    repo.create_job(job, records)
    with pytest.raises(KeyError):
        repo.update_record(job.batch_id, 99, status="x")
    with pytest.raises(KeyError):
        repo.update_job(str(uuid.uuid4()), status="x")
    assert repo.get_job(str(uuid.uuid4())) is None
    assert repo.list_records(str(uuid.uuid4())) == []


@pytest.mark.parametrize("bad", ["../../etc", "..", "not-a-uuid", "", "a/b"])
def test_malformed_ids_never_touch_the_filesystem(repo, tmp_path, bad):
    assert repo.get_job(bad) is None
    assert repo.list_records(bad) == []
    with pytest.raises(KeyError):
        repo.update_job(bad, status="x")
    assert not (tmp_path / "jobs").exists()


def test_duplicate_create_rejected(repo):
    job, records = make()
    repo.create_job(job, records)
    with pytest.raises(FileExistsError):
        repo.create_job(job, records)


def test_no_temp_files_left_behind(repo, tmp_path):
    job, records = make()
    repo.create_job(job, records)
    repo.update_record(job.batch_id, 1, status="created")
    files = sorted(p.name for p in (tmp_path / "jobs" / job.batch_id).iterdir())
    assert files == ["job.json", "records.json"]


def test_concurrent_updates_do_not_lose_writes(repo):
    job, records = make(n=20)
    repo.create_job(job, records)

    def work(row):
        repo.update_record(job.batch_id, row, status="created", hospital_id=row)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(1, 21)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = repo.list_records(job.batch_id)
    assert all(r.status == "created" and r.hospital_id == r.row for r in got)


def test_delete_job_removes_everything(repo, tmp_path):
    job, records = make()
    repo.create_job(job, records)
    assert repo.delete_job(job.batch_id) is True
    assert repo.get_job(job.batch_id) is None and repo.list_records(job.batch_id) == []
    assert not (tmp_path / "jobs" / job.batch_id).exists()
    assert repo.delete_job(job.batch_id) is False  # already gone


@pytest.mark.parametrize("bad", ["../..", "..", "not-a-uuid", "", "a/b"])
def test_delete_job_ignores_malformed_ids(repo, tmp_path, bad):
    job, records = make()
    repo.create_job(job, records)
    assert repo.delete_job(bad) is False
    assert repo.get_job(job.batch_id) is not None  # nothing else was touched


def test_purge_orphans_keeps_valid_jobs(repo, tmp_path):
    job, records = make()
    repo.create_job(job, records)
    orphan = tmp_path / "jobs" / str(uuid.uuid4())
    orphan.mkdir()
    assert repo.purge_orphans() == 1
    assert repo.get_job(job.batch_id) is not None and not orphan.exists()
    repo.delete_job(job.batch_id)
    assert repo.purge_orphans() == 0
    assert not (tmp_path / "jobs").exists()  # empty root is removed for a fresh state
