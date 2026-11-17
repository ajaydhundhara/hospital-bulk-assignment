import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import ContextManager, List, Optional

from .client import HospitalDirectoryClient, UpstreamError
from .csv_validation import InvalidRow
from .models import (
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_PARTIAL,
    JOB_PROCESSING,
    RECORD_ACTIVATED,
    RECORD_CREATED,
    RECORD_FAILED,
    RECORD_INVALID,
    RECORD_PENDING,
    RECORD_PROCESSING,
    BulkResult,
    CleanupFailure,
    CleanupResult,
    HospitalResult,
    HospitalRow,
    Job,
    Record,
)
from .repository import JobRepository, utcnow

_DONE = {RECORD_CREATED, RECORD_ACTIVATED}
_UNFINISHED = {RECORD_PENDING, RECORD_PROCESSING}
_FAILED = {RECORD_FAILED, RECORD_INVALID}  # both count as failed rows (O1.3)


# --- Derived state ------------------------------------------------------------------


def _resumable(records: List[Record]) -> int:
    """Rows a resume would act on: never finished, or failed for a transient reason."""
    return sum(
        1 for r in records if r.status in _UNFINISHED or (r.status == RECORD_FAILED and r.retryable)
    )


def _derive_status(records: List[Record]) -> str:
    if any(r.status in _UNFINISHED for r in records):
        return JOB_INTERRUPTED  # a run ended (outage stop, restart) before every row was tried
    created = any(r.status in _DONE for r in records)
    failed = any(r.status in _FAILED for r in records)
    awaiting_activation = any(r.status == RECORD_CREATED for r in records)
    if created and not failed and not awaiting_activation:
        return JOB_COMPLETED
    return JOB_PARTIAL if created else JOB_FAILED


def _fully_activated(records: List[Record]) -> bool:
    return any(r.status == RECORD_ACTIVATED for r in records) and not any(
        r.status == RECORD_CREATED for r in records
    )


def build_result(job: Job, records: List[Record]) -> BulkResult:
    """Shape persisted state into the API response."""
    end = job.finished_at or utcnow()
    elapsed = (end - job.started_at).total_seconds() if job.started_at else 0.0
    return BulkResult(
        batch_id=job.batch_id,
        status=job.status,
        total_hospitals=job.total_rows,
        processed_hospitals=sum(1 for r in records if r.status in _DONE),
        failed_hospitals=sum(1 for r in records if r.status in _FAILED),
        processing_time_seconds=round(elapsed, 3),
        batch_activated=job.activated,
        activation_error=job.activation_error,
        error=job.error,
        resumable_rows=0 if job.status == JOB_PROCESSING else _resumable(records),
        warnings=job.warnings,
        hospitals=[
            HospitalResult(
                row=r.row,
                hospital_id=r.hospital_id,
                name=r.name,
                status=r.status,
                error=r.error,
                retryable=r.retryable if r.status in _FAILED else None,
            )
            for r in records
        ],
    )


# --- Upload and run -----------------------------------------------------------------


def start_batch(
    repo: JobRepository,
    rows: List[HospitalRow],
    filename: Optional[str] = None,
    activate_on_partial: bool = False,
    invalid: Optional[List[InvalidRow]] = None,
    warnings: Optional[List[str]] = None,
) -> Job:
    """Create the job and persist every row before any upstream call: valid rows as
    `pending`, rows whose data is not correct as `invalid` with the reason (D19)."""
    invalid = invalid or []
    batch_id = str(uuid.uuid4())
    now = utcnow()
    job = Job(
        batch_id=batch_id,
        filename=filename,
        total_rows=len(rows) + len(invalid),
        failed_count=len(invalid),
        activate_on_partial=activate_on_partial,
        warnings=warnings or [],
        created_at=now,
        started_at=now,
    )
    records = [
        Record(
            batch_id=batch_id,
            row=r.row,
            name=r.name,
            address=r.address,
            phone=r.phone,
            status=RECORD_PENDING,
            updated_at=now,
        )
        for r in rows
    ] + [
        Record(
            batch_id=batch_id,
            row=r.row,
            name=r.name,
            address=r.address,
            phone=r.phone,
            status=RECORD_INVALID,
            error=r.message,
            retryable=False,
            updated_at=now,
        )
        for r in invalid
    ]
    repo.create_job(job, sorted(records, key=lambda r: r.row))
    return job


def run_batch(
    client: HospitalDirectoryClient,
    repo: JobRepository,
    batch_id: str,
    activate_on_partial: bool = False,
    concurrency: int = 1,
    limiter: Optional[ContextManager] = None,
    outage_threshold: int = 0,
    warmup_timeout: float = 0,
) -> BulkResult:
    """Create every `pending` row upstream, then activate the batch if the rule allows.

    Rows are sent through a pool of `concurrency` threads. `limiter` (a semaphore shared
    by every batch in the process) caps how many creates are in flight in total (D23).
    Every state change is written as it happens, so progress is visible while running
    and the exact state survives a crash.

    If `outage_threshold` consecutive rows fail for a transient reason (network, 5xx,
    429), no new rows are started: the batch ends `interrupted` with the rest still
    `pending`, ready for a resume once upstream is back.

    If `warmup_timeout` is set, upstream must answer `GET /` within that time before any
    create is sent; otherwise nothing is sent and the batch ends `interrupted`.

    Used for the first run and for resumes alike: it only looks at `pending` rows, and
    counts rows that already finished.
    """
    try:
        records = repo.list_records(batch_id)
        todo = [r for r in records if r.status == RECORD_PENDING]
        if todo and warmup_timeout > 0:
            try:
                client.wake(warmup_timeout)
            except UpstreamError as exc:
                job = _finish(
                    repo,
                    batch_id,
                    error=f"{exc}. It may be waking up after being idle; nothing was sent. "
                    "Resume this batch in a minute.",
                )
                return build_result(job, repo.list_records(batch_id))
        counts = {
            "created": sum(1 for r in records if r.status in _DONE),
            "failed": sum(1 for r in records if r.status in _FAILED),
            "streak": 0,  # consecutive transient failures
        }
        lock = threading.Lock()
        stop = threading.Event()
        outage = threading.Event()

        def process(rec: Record) -> None:
            repo.update_record(
                batch_id,
                rec.row,
                status=RECORD_PROCESSING,
                attempts=rec.attempts + 1,
                error=None,
                retryable=None,
                outcome_unknown=False,
            )
            t0 = time.perf_counter()
            try:
                hospital = client.create_hospital(rec.name, rec.address, rec.phone, batch_id)
            except UpstreamError as exc:
                repo.update_record(
                    batch_id,
                    rec.row,
                    status=RECORD_FAILED,
                    error=str(exc),
                    retryable=exc.retryable,
                    outcome_unknown=exc.outcome_unknown,
                    upstream_ms=round((time.perf_counter() - t0) * 1000),
                )
                ok, transient = False, exc.retryable
            else:
                repo.update_record(
                    batch_id,
                    rec.row,
                    status=RECORD_CREATED,
                    hospital_id=hospital.get("id"),
                    upstream_ms=round((time.perf_counter() - t0) * 1000),
                )
                ok, transient = True, False
            with lock:
                counts["created" if ok else "failed"] += 1
                counts["streak"] = counts["streak"] + 1 if transient else 0
                if outage_threshold and counts["streak"] >= outage_threshold:
                    outage.set()
                    stop.set()
                repo.update_job(
                    batch_id, created_count=counts["created"], failed_count=counts["failed"]
                )

        def work(rec: Record) -> None:
            if stop.is_set():
                return  # stays `pending`
            with limiter or nullcontext():
                if stop.is_set():
                    return
                try:
                    process(rec)
                except BaseException:
                    stop.set()  # a bug: don't start further rows
                    raise

        with ThreadPoolExecutor(
            max_workers=max(1, concurrency), thread_name_prefix=f"batch-{batch_id[:8]}"
        ) as pool:
            futures = [pool.submit(work, r) for r in todo]
        errors = [f.exception() for f in futures if f.exception() is not None]
        if errors:
            raise errors[0]

        activation_error = None
        error = None
        left = sum(1 for r in repo.list_records(batch_id) if r.status == RECORD_PENDING)
        if outage.is_set() and left:
            error = (
                f"Stopped after {outage_threshold} consecutive upstream failures; "
                f"{left} row(s) were not sent. Resume when the upstream is back."
            )
        else:  # finished normally, or the stop came after every row had been tried
            activation_error = _activate_if_ready(client, repo, batch_id, activate_on_partial)
        job = _finish(repo, batch_id, activation_error=activation_error, error=error)
    except Exception as exc:
        # Unexpected bug: don't leave the job looking like it is still running.
        _mark_failed(repo, batch_id, exc)
        raise

    return build_result(job, repo.list_records(batch_id))


def _activate_if_ready(
    client: HospitalDirectoryClient, repo: JobRepository, batch_id: str, activate_on_partial: bool
) -> Optional[str]:
    """Activate when rows await activation, nothing is unfinished, and either nothing
    failed or the caller opted in. Returns the activation error, if any."""
    records = repo.list_records(batch_id)
    waiting = any(r.status == RECORD_CREATED for r in records)
    unfinished = any(r.status in _UNFINISHED for r in records)
    failed = any(r.status in _FAILED for r in records)
    if not waiting or unfinished or (failed and not activate_on_partial):
        return None
    try:
        client.activate_batch(batch_id)
    except UpstreamError as exc:
        return str(exc)
    repo.transition_records(batch_id, RECORD_CREATED, RECORD_ACTIVATED)
    return None


def _finish(
    repo: JobRepository,
    batch_id: str,
    activation_error: Optional[str] = None,
    error: Optional[str] = None,
) -> Job:
    records = repo.list_records(batch_id)
    return repo.update_job(
        batch_id,
        status=_derive_status(records),
        activated=_fully_activated(records),
        activation_error=activation_error,
        error=error,
        created_count=sum(1 for r in records if r.status in _DONE),
        failed_count=sum(1 for r in records if r.status in _FAILED),
        resumable_rows=_resumable(records),
        finished_at=utcnow(),
    )


def _mark_failed(repo: JobRepository, batch_id: str, exc: Exception) -> None:
    try:
        resumable = _resumable(repo.list_records(batch_id))
    except Exception:
        resumable = 0
    repo.update_job(
        batch_id, status=JOB_FAILED, error=str(exc), resumable_rows=resumable, finished_at=utcnow()
    )


# --- Activation -----------------------------------------------------------------------


class ActivationNotAllowed(Exception):
    pass


def activate_batch(client: HospitalDirectoryClient, repo: JobRepository, job: Job) -> BulkResult:
    """Activate the created rows of a finished, not-yet-activated batch (e.g. a partial one)."""
    if job.status == JOB_PROCESSING:
        raise ActivationNotAllowed("Batch is still processing")
    if job.activated:
        raise ActivationNotAllowed("Batch is already activated")
    if not repo.list_records(job.batch_id, status=RECORD_CREATED):
        raise ActivationNotAllowed("Batch has no created hospitals to activate")
    try:
        client.activate_batch(job.batch_id)
    except UpstreamError as exc:
        job = repo.update_job(job.batch_id, activation_error=str(exc))
        return build_result(job, repo.list_records(job.batch_id))
    repo.transition_records(job.batch_id, RECORD_CREATED, RECORD_ACTIVATED)
    records = repo.list_records(job.batch_id)
    job = repo.update_job(
        job.batch_id,
        activated=_fully_activated(records),
        activation_error=None,
        status=_derive_status(records),
    )
    return build_result(job, records)


# --- Resume (D21) ---------------------------------------------------------------------


class ResumeNotAllowed(Exception):
    pass


# Check-and-claim must be atomic so two resume requests can't both start the same batch.
# Process-local, like the rest of single-instance storage (a DB lease replaces it, O8).
_resume_guard = threading.Lock()


def prepare_resume(
    client: HospitalDirectoryClient,
    repo: JobRepository,
    job: Job,
    activate_on_partial: Optional[bool] = None,
) -> Job:
    """Get a stopped batch ready to run again, then mark it `processing`.

    1. Rows whose outcome is unknown (left `processing` by a crash, or a create that
       timed out) are looked up in our own batch upstream: found -> `created` with its
       hospital id; not found -> `pending`. This replaces an idempotency key (D22).
    2. Rows that failed for a transient reason go back to `pending`.
    3. Rows rejected by upstream (4xx) are left alone: they need corrected data.

    The caller then runs `run_batch`, which sends the `pending` rows and activates.
    """
    with _resume_guard:
        job = repo.get_job(job.batch_id) or job  # re-read inside the guard
        if job.status == JOB_PROCESSING:
            raise ResumeNotAllowed("Batch is still processing")
        records = repo.list_records(job.batch_id)
        unknown = [
            r
            for r in records
            if r.status == RECORD_PROCESSING or (r.status == RECORD_FAILED and r.outcome_unknown)
        ]
        retry = [
            r
            for r in records
            if r.status == RECORD_FAILED and r.retryable and not r.outcome_unknown
        ]
        pending = [r for r in records if r.status == RECORD_PENDING]
        if not (unknown or retry or pending):
            reasons = ["Nothing to retry."]
            if any(r.status in _FAILED for r in records):
                reasons.append(
                    "The failed rows were rejected (invalid data, or refused by the "
                    "upstream) and need corrected data: upload them in a new file."
                )
            if any(r.status == RECORD_CREATED for r in records):
                reasons.append("Use activate to make the created hospitals live.")
            raise ResumeNotAllowed(" ".join(reasons))

        if unknown:
            try:
                upstream = client.get_batch(job.batch_id)
            except UpstreamError as exc:
                if exc.status_code != 404:
                    raise  # can't check safely: resend nothing
                upstream = []
            claimed = {r.hospital_id for r in records if r.hospital_id is not None}
            for r in unknown:
                match = next(
                    (
                        h
                        for h in upstream
                        if h.get("id") not in claimed
                        and h.get("name") == r.name
                        and h.get("address") == r.address
                    ),
                    None,
                )
                if match is not None:
                    claimed.add(match.get("id"))
                    repo.update_record(
                        job.batch_id,
                        r.row,
                        status=RECORD_ACTIVATED if match.get("active") else RECORD_CREATED,
                        hospital_id=match.get("id"),
                        error=None,
                        retryable=None,
                        outcome_unknown=False,
                    )
                else:
                    repo.update_record(
                        job.batch_id,
                        r.row,
                        status=RECORD_PENDING,
                        error=None,
                        retryable=None,
                        outcome_unknown=False,
                    )
        for r in retry:
            repo.update_record(job.batch_id, r.row, status=RECORD_PENDING, error=None, retryable=None)

        return repo.update_job(
            job.batch_id,
            status=JOB_PROCESSING,
            error=None,
            activation_error=None,
            finished_at=None,
            resumable_rows=0,
            resume_count=job.resume_count + 1,
            started_at=utcnow(),  # processing time reports the latest run (created_at = upload)
            activate_on_partial=(
                job.activate_on_partial if activate_on_partial is None else activate_on_partial
            ),
        )


def mark_interrupted_jobs(repo: JobRepository) -> int:
    """Startup sweep: a job still `processing` when the server starts has no runner
    (single process), so mark it `interrupted` and resumable. Returns how many."""
    swept = 0
    for job in repo.list_jobs():
        if job.status != JOB_PROCESSING:
            continue
        records = repo.list_records(job.batch_id)
        repo.update_job(
            job.batch_id,
            status=JOB_INTERRUPTED,
            error="The server restarted while this batch was running. Resume to continue.",
            created_count=sum(1 for r in records if r.status in _DONE),
            failed_count=sum(1 for r in records if r.status in _FAILED),
            resumable_rows=_resumable(records),
            finished_at=utcnow(),
        )
        swept += 1
    return swept


# --- Hard clean -----------------------------------------------------------------------


class CleanupBlocked(Exception):
    pass


def hard_clean(
    client: HospitalDirectoryClient, repo: JobRepository, force: bool = False
) -> CleanupResult:
    """Delete every batch this app created: upstream hospitals first, then local state.

    Only batches known to this app are touched (by batch id); other upstream records are
    never deleted. For each batch the upstream delete runs first, and the local job is
    removed only if it succeeded, so a failure leaves the batch in place to retry rather
    than orphaning upstream records. An upstream 404 means nothing is left there, which
    counts as success.

    Refuses while a batch is still processing, because a running background job would
    keep creating upstream records after the clean. `force` skips that check (for jobs
    stuck in `processing` after a crash).
    """
    jobs = repo.list_jobs()
    busy = [j for j in jobs if j.status == JOB_PROCESSING]
    if busy and not force:
        raise CleanupBlocked(f"{len(busy)} batch(es) still processing")

    batches_deleted = hospitals_deleted = 0
    failed: List[CleanupFailure] = []
    for job in jobs:
        try:
            hospitals_deleted += client.delete_batch(job.batch_id)
        except UpstreamError as exc:
            if exc.status_code != 404:
                failed.append(CleanupFailure(batch_id=job.batch_id, error=str(exc)))
                continue
        if repo.delete_job(job.batch_id):
            batches_deleted += 1

    orphans = repo.purge_orphans() if not failed else 0
    return CleanupResult(
        batches_deleted=batches_deleted,
        hospitals_deleted=hospitals_deleted,
        orphans_removed=orphans,
        failed=failed,
        clean=not failed,
    )
