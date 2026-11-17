import csv
import io
import logging
import threading
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse, Response as RawResponse

from .client import HospitalDirectoryClient, UpstreamError
from .config import Settings, get_settings
from .csv_validation import parse_csv
from .models import (
    RECORD_FAILED,
    RECORD_INVALID,
    BulkResult,
    CleanupResult,
    Job,
    Record,
    ValidationResult,
)
from .repository import JobRepository, JsonFileRepository
from .service import (
    ActivationNotAllowed,
    CleanupBlocked,
    ResumeNotAllowed,
    activate_batch,
    build_result,
    hard_clean,
    mark_interrupted_jobs,
    prepare_resume,
    run_batch,
    start_batch,
)

logger = logging.getLogger(__name__)
UI_FILE = Path(__file__).parent / "static" / "index.html"
ClientFactory = Callable[[], HospitalDirectoryClient]


@lru_cache(maxsize=None)
def _repository_for(data_dir: str) -> JobRepository:
    # One instance per directory so its per-batch locks are shared across requests.
    return JsonFileRepository(data_dir)


@lru_cache(maxsize=None)
def _upstream_limiter(size: int) -> threading.BoundedSemaphore:
    # One semaphore per process, shared by every batch: total creates in flight <= size.
    return threading.BoundedSemaphore(size)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Single process: anything still `processing` at startup lost its runner.
    swept = mark_interrupted_jobs(_repository_for(get_settings().data_dir))
    if swept:
        logger.warning("Marked %d batch(es) interrupted after restart; they can be resumed", swept)
    yield


app = FastAPI(
    title="Hospital Bulk Processing API",
    description="Bulk-create hospitals from a CSV via the Hospital Directory API.",
    version="0.3.0",
    lifespan=lifespan,
)


def get_run_options(settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    """How rows are sent upstream (D23): pool size, shared limiter, outage stop."""
    return {
        "concurrency": settings.upstream_concurrency,
        "limiter": _upstream_limiter(settings.upstream_concurrency),
        "outage_threshold": settings.outage_threshold,
        "warmup_timeout": settings.upstream_warmup,
    }


def get_repository(settings: Settings = Depends(get_settings)) -> JobRepository:
    return _repository_for(settings.data_dir)


def get_client_factory(settings: Settings = Depends(get_settings)) -> ClientFactory:
    """Builds upstream clients; background runs need one that outlives the request."""
    return lambda: HospitalDirectoryClient(
        settings.upstream_base_url,
        timeout=settings.upstream_timeout,
        retries=settings.upstream_retries,
        backoff=settings.retry_backoff,
    )


def get_client(factory: ClientFactory = Depends(get_client_factory)) -> Iterator[HospitalDirectoryClient]:
    client = factory()
    try:
        yield client
    finally:
        client.close()


def _require_job(repo: JobRepository, batch_id: str) -> Job:
    job = repo.get_job(batch_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown batch id")
    return job


def _run_in_background(
    factory: ClientFactory,
    repo: JobRepository,
    batch_id: str,
    activate_on_partial: bool,
    options: Dict[str, Any],
) -> None:
    client = factory()
    try:
        run_batch(client, repo, batch_id, activate_on_partial, **options)
    except Exception:  # run_batch already marked the job failed
        logger.exception("Background batch %s crashed", batch_id)
    finally:
        client.close()


def _run(
    job: Job,
    background: bool,
    background_tasks: BackgroundTasks,
    response: Response,
    factory: ClientFactory,
    repo: JobRepository,
    options: Dict[str, Any],
) -> BulkResult:
    """Run a prepared job now (200 with the final result) or in the background (202)."""
    if background:
        background_tasks.add_task(
            _run_in_background, factory, repo, job.batch_id, job.activate_on_partial, options
        )
        response.status_code = 202
        return build_result(job, repo.list_records(job.batch_id))
    client = factory()
    try:
        return run_batch(client, repo, job.batch_id, job.activate_on_partial, **options)
    finally:
        client.close()


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """A browser opening the bare URL lands on the test console."""
    return RedirectResponse("/ui")


@app.get("/health", tags=["health"])
def health() -> Dict[str, str]:
    """Liveness check (Render's healthCheckPath, Docker HEALTHCHECK)."""
    return {"status": "ok"}


@app.get("/info", tags=["health"])
def info(settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    """Limits and features of this deployment (used by the test console)."""
    return {
        "max_rows": settings.max_rows,
        "upstream_concurrency": settings.upstream_concurrency,
        "hard_clean_enabled": settings.enable_hard_clean,
    }


@app.get("/ui", include_in_schema=False)
def ui() -> FileResponse:
    """Single-page test console (no auth)."""
    return FileResponse(UI_FILE)


@app.get("/hospitals/bulk", response_model=List[Job], tags=["bulk"])
def list_bulk_jobs(repo: JobRepository = Depends(get_repository)) -> List[Job]:
    """Every batch that has been uploaded, newest first."""
    return repo.list_jobs()


@app.delete("/hospitals/bulk", response_model=CleanupResult, tags=["bulk"])
def hard_clean_all(
    confirm: bool = Query(False, description="Must be true: this deletes everything"),
    force: bool = Query(
        False, description="Also clean while a batch is 'processing' (e.g. stuck after a crash)"
    ),
    settings: Settings = Depends(get_settings),
    client: HospitalDirectoryClient = Depends(get_client),
    repo: JobRepository = Depends(get_repository),
) -> CleanupResult:
    """DESTRUCTIVE. Delete every batch created through this app: its hospitals upstream
    (by batch id) and all stored jobs, records and files, leaving a fresh state.

    Batches whose upstream delete fails are kept and listed in `failed` so you can retry.
    Returns 409 if a batch is still processing, unless `force=true` (a still-running
    background job would keep creating records, so only force for stuck batches).
    403 when disabled (`ENABLE_HARD_CLEAN=false`, as on the public deployment).
    """
    if not settings.enable_hard_clean:
        raise HTTPException(status_code=403, detail="Hard clean is disabled on this deployment")
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass confirm=true to delete all batches")
    try:
        return hard_clean(client, repo, force=force)
    except CleanupBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/hospitals/bulk/validate", response_model=ValidationResult, tags=["bulk"])
def validate_csv(
    file: UploadFile = File(...), settings: Settings = Depends(get_settings)
) -> ValidationResult:
    """Dry run: report exactly what an upload of this file would do. Creates nothing."""
    parsed = parse_csv(
        file.file.read(settings.max_file_bytes + 1), settings.max_rows, settings.max_file_bytes
    )
    return ValidationResult(
        valid=not parsed.errors,
        total_rows=parsed.total,
        processable_rows=len(parsed.rows),
        invalid_rows=len(parsed.invalid),
        errors=parsed.errors + parsed.row_errors,
        warnings=parsed.warnings,
    )


@app.post("/hospitals/bulk", response_model=BulkResult, tags=["bulk"])
def bulk_create(
    response: Response,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    activate_on_partial: bool = Query(
        False, description="Activate the batch even if some rows failed"
    ),
    background: bool = Query(
        False,
        description="Return 202 immediately with the batch id and process in the background; "
        "poll GET /hospitals/bulk/{batch_id} for progress",
    ),
    settings: Settings = Depends(get_settings),
    client_factory: ClientFactory = Depends(get_client_factory),
    repo: JobRepository = Depends(get_repository),
    options: Dict[str, Any] = Depends(get_run_options),
) -> BulkResult:
    """Validate the CSV, create each hospital under a new batch id, then activate.

    File-level problems (missing column, encoding, > MAX_ROWS rows, ...) return 422 and
    create nothing. A row whose data is not correct is recorded as `invalid` with the
    reason and skipped; the valid rows are processed (D19). Rows are sent concurrently
    (`UPSTREAM_CONCURRENCY`, default 10) and reported in row order.
    """
    parsed = parse_csv(
        file.file.read(settings.max_file_bytes + 1), settings.max_rows, settings.max_file_bytes
    )
    if parsed.errors:
        errors = parsed.errors + parsed.row_errors
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV validation failed", "errors": [e.model_dump() for e in errors]},
        )
    job = start_batch(
        repo,
        parsed.rows,
        filename=file.filename,
        activate_on_partial=activate_on_partial,
        invalid=parsed.invalid,
        warnings=parsed.warnings,
    )
    return _run(job, background, background_tasks, response, client_factory, repo, options)


@app.post("/hospitals/bulk/{batch_id}/resume", response_model=BulkResult, tags=["bulk"])
def resume_bulk(
    batch_id: str,
    response: Response,
    background_tasks: BackgroundTasks,
    background: bool = Query(False, description="Return 202 and continue in the background"),
    activate_on_partial: Optional[bool] = Query(
        None, description="Override the upload's choice; omitted = reuse it"
    ),
    client_factory: ClientFactory = Depends(get_client_factory),
    repo: JobRepository = Depends(get_repository),
    options: Dict[str, Any] = Depends(get_run_options),
) -> BulkResult:
    """Continue a batch that stopped partway or ended with retryable failures.

    Rows already created are never re-sent. Rows whose outcome is unknown are first
    looked up upstream in this batch. Rows the upstream rejected (4xx) are left as
    failed: they need corrected data in a new upload. 409 if the batch is processing or
    there is nothing to retry; 502 if the upstream check itself fails.
    """
    job = _require_job(repo, batch_id)
    client = client_factory()
    try:
        job = prepare_resume(client, repo, job, activate_on_partial)
    except ResumeNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"Could not check the upstream batch: {exc}")
    finally:
        client.close()
    return _run(job, background, background_tasks, response, client_factory, repo, options)


@app.post("/hospitals/bulk/{batch_id}/activate", response_model=BulkResult, tags=["bulk"])
def activate_bulk(
    batch_id: str,
    client: HospitalDirectoryClient = Depends(get_client),
    repo: JobRepository = Depends(get_repository),
) -> BulkResult:
    """Activate a finished batch that was left inactive (e.g. partial): only its created rows."""
    job = _require_job(repo, batch_id)
    try:
        return activate_batch(client, repo, job)
    except ActivationNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/hospitals/bulk/{batch_id}", response_model=BulkResult, tags=["bulk"])
def get_bulk_result(batch_id: str, repo: JobRepository = Depends(get_repository)) -> BulkResult:
    """Current state of a bulk run: job summary plus every row's status and error."""
    job = _require_job(repo, batch_id)
    return build_result(job, repo.list_records(batch_id))


@app.get("/hospitals/bulk/{batch_id}/job", response_model=Job, tags=["bulk"])
def get_bulk_job(batch_id: str, repo: JobRepository = Depends(get_repository)) -> Job:
    """The raw job record (counters, timestamps, filename)."""
    return _require_job(repo, batch_id)


@app.get(
    "/hospitals/bulk/{batch_id}/failed.csv",
    tags=["bulk"],
    response_class=RawResponse,
    responses={200: {"content": {"text/csv": {}}}},
)
def get_failed_rows_csv(batch_id: str, repo: JobRepository = Depends(get_repository)) -> RawResponse:
    """Failed and invalid rows as a CSV (`name,address,phone,error`), ready to fix and
    upload again as a new batch. The `error` column is ignored on upload, with a warning."""
    _require_job(repo, batch_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["name", "address", "phone", "error"])
    for r in repo.list_records(batch_id):
        if r.status in (RECORD_FAILED, RECORD_INVALID):
            writer.writerow([r.name, r.address, r.phone or "", r.error or ""])
    return RawResponse(
        out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="failed-{batch_id}.csv"'},
    )


@app.get("/hospitals/bulk/{batch_id}/records", response_model=List[Record], tags=["bulk"])
def get_bulk_records(
    batch_id: str,
    status: Optional[str] = Query(None, description="e.g. failed, created_and_activated"),
    repo: JobRepository = Depends(get_repository),
) -> List[Record]:
    """Stored CSV rows with their processing state; filter with ?status=failed."""
    _require_job(repo, batch_id)
    return repo.list_records(batch_id, status=status)
