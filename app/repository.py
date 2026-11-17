"""Storage for jobs and their per-row records.

`JobRepository` is the seam between business logic and storage. The JSON-file
implementation below is a stand-in for a database: `job.json` maps to a `bulk_jobs`
table and `records.json` to a `bulk_job_records` table keyed by (batch_id, row).
"""
import json
import os
import shutil
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .models import Job, Record


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRepository(ABC):
    @abstractmethod
    def create_job(self, job: Job, records: List[Record]) -> None:
        """Persist a new job together with all of its (pending) records."""

    @abstractmethod
    def get_job(self, batch_id: str) -> Optional[Job]:
        """Return the job, or None if the id is unknown or malformed."""

    @abstractmethod
    def list_jobs(self) -> List[Job]:
        """Every job, newest first."""

    @abstractmethod
    def update_job(self, batch_id: str, **fields: Any) -> Job:
        """Update fields on a job. Raises KeyError if it does not exist."""

    @abstractmethod
    def list_records(self, batch_id: str, status: Optional[str] = None) -> List[Record]:
        """Records ordered by row number, optionally filtered by status."""

    @abstractmethod
    def update_record(self, batch_id: str, row: int, **fields: Any) -> Record:
        """Update fields on one record. Raises KeyError if it does not exist."""

    @abstractmethod
    def transition_records(self, batch_id: str, from_status: str, to_status: str) -> int:
        """Move every record in `from_status` to `to_status`; return how many moved."""

    @abstractmethod
    def delete_job(self, batch_id: str) -> bool:
        """Delete a job and all of its records. False if it did not exist."""

    @abstractmethod
    def purge_orphans(self) -> int:
        """Remove leftover storage that belongs to no valid job (e.g. a crash mid-create).
        Returns how many were removed."""


class JsonFileRepository(JobRepository):
    """Layout: <base_dir>/jobs/<batch_id>/{job.json,records.json}

    Writes are atomic (temp file + rename) and serialized per batch with a lock, so
    concurrent updates to one batch cannot overwrite each other. The lock is
    in-process only: this is single-process storage, not a multi-worker one.
    """

    def __init__(self, base_dir: Union[str, Path]) -> None:
        self._root = Path(base_dir) / "jobs"
        self._locks: Dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    # -- helpers --------------------------------------------------------------

    def _dir(self, batch_id: str) -> Optional[Path]:
        # batch_id arrives from URLs: only accept real UUIDs, and rebuild the string
        # from the parsed value so it can never contain path separators.
        try:
            canonical = str(uuid.UUID(batch_id))
        except (ValueError, AttributeError, TypeError):
            return None
        return self._root / canonical

    def _lock(self, batch_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(batch_id, threading.RLock())

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_records(self, d: Path) -> List[Record]:
        return [Record.model_validate(r) for r in self._read(d / "records.json")]

    def _save_records(self, d: Path, records: List[Record]) -> None:
        self._write(d / "records.json", [r.model_dump(mode="json") for r in records])

    def _require_dir(self, batch_id: str) -> Path:
        d = self._dir(batch_id)
        if d is None or not (d / "job.json").exists():
            raise KeyError(batch_id)
        return d

    # -- JobRepository --------------------------------------------------------

    def create_job(self, job: Job, records: List[Record]) -> None:
        d = self._dir(job.batch_id)
        if d is None:
            raise ValueError(f"Invalid batch id: {job.batch_id!r}")
        with self._lock(job.batch_id):
            d.mkdir(parents=True, exist_ok=False)
            self._save_records(d, records)
            self._write(d / "job.json", job.model_dump(mode="json"))  # written last

    def get_job(self, batch_id: str) -> Optional[Job]:
        d = self._dir(batch_id)
        if d is None or not (d / "job.json").exists():
            return None
        with self._lock(batch_id):
            return Job.model_validate(self._read(d / "job.json"))

    def list_jobs(self) -> List[Job]:
        if not self._root.exists():
            return []
        jobs = [j for j in (self.get_job(d.name) for d in self._root.iterdir() if d.is_dir()) if j]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def update_job(self, batch_id: str, **fields: Any) -> Job:
        with self._lock(batch_id):
            d = self._require_dir(batch_id)
            job = Job.model_validate(self._read(d / "job.json")).model_copy(update=fields)
            self._write(d / "job.json", job.model_dump(mode="json"))
            return job

    def list_records(self, batch_id: str, status: Optional[str] = None) -> List[Record]:
        d = self._dir(batch_id)
        if d is None or not (d / "records.json").exists():
            return []
        with self._lock(batch_id):
            records = self._load_records(d)
        if status is not None:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: r.row)

    def update_record(self, batch_id: str, row: int, **fields: Any) -> Record:
        with self._lock(batch_id):
            d = self._require_dir(batch_id)
            records = self._load_records(d)
            for i, rec in enumerate(records):
                if rec.row == row:
                    records[i] = rec.model_copy(update={**fields, "updated_at": utcnow()})
                    self._save_records(d, records)
                    return records[i]
        raise KeyError(f"{batch_id}/row {row}")

    def transition_records(self, batch_id: str, from_status: str, to_status: str) -> int:
        with self._lock(batch_id):
            d = self._require_dir(batch_id)
            records = self._load_records(d)
            now = utcnow()
            moved = 0
            for i, rec in enumerate(records):
                if rec.status == from_status:
                    records[i] = rec.model_copy(update={"status": to_status, "updated_at": now})
                    moved += 1
            if moved:
                self._save_records(d, records)
            return moved

    def delete_job(self, batch_id: str) -> bool:
        d = self._dir(batch_id)  # None for anything that is not a UUID: never touches disk
        if d is None:
            return False
        with self._lock(batch_id):
            if not d.exists():
                return False
            shutil.rmtree(d)
        with self._guard:
            self._locks.pop(batch_id, None)
        return True

    def purge_orphans(self) -> int:
        if not self._root.exists():
            return 0
        removed = 0
        for d in self._root.iterdir():
            # A directory without job.json was never a complete job (create_job writes it last).
            if d.is_dir() and not (d / "job.json").exists():
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        try:
            self._root.rmdir()  # only succeeds when empty: leave a truly fresh state
        except OSError:
            pass
        return removed
