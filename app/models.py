from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class HospitalRow(BaseModel):
    """A validated CSV row. `row` is the 1-based data row number (header excluded)."""

    row: int
    name: str
    address: str
    phone: Optional[str] = None


class RowError(BaseModel):
    row: int  # 0 means a file/header-level error
    field: Optional[str] = None
    message: str


class ValidationResult(BaseModel):
    """Dry run: exactly what `POST /hospitals/bulk` would do with this file."""

    valid: bool  # the file would be accepted (at least one row can be processed)
    total_rows: int  # every data row in the file
    processable_rows: int = 0  # rows that would be sent upstream
    invalid_rows: int = 0  # rows that would be recorded as `invalid` and skipped
    errors: List[RowError] = []  # file-level errors (row 0) and row-level errors
    warnings: List[str] = []


# --- Persisted state -------------------------------------------------------------

# Record (row) lifecycle: pending -> processing -> created -> created_and_activated
#                                                \-> failed
# A failed row with `retryable=True` goes back to `pending` on resume. A row whose
# outcome is unknown (timeout on create, or left `processing` by a crash) is first
# checked against upstream: found -> `created`, not found -> `pending`.
# A row whose data is not correct is stored directly as `invalid` (never sent).
RECORD_PENDING = "pending"
RECORD_PROCESSING = "processing"
RECORD_CREATED = "created"
RECORD_ACTIVATED = "created_and_activated"
RECORD_FAILED = "failed"
RECORD_INVALID = "invalid"  # data not correct (D19): recorded, never sent upstream

# Job lifecycle
JOB_PROCESSING = "processing"
JOB_COMPLETED = "completed"  # every row created and batch activated
JOB_PARTIAL = "partial"  # some rows created, some failed or not activated
JOB_FAILED = "failed"  # nothing created, or an unexpected error
JOB_INTERRUPTED = "interrupted"  # stopped before finishing (upstream outage or restart); resumable


class Record(BaseModel):
    """One CSV row and its processing state. Stored before any upstream call."""

    batch_id: str
    row: int
    name: str
    address: str
    phone: Optional[str] = None
    status: str = RECORD_PENDING
    hospital_id: Optional[int] = None
    error: Optional[str] = None
    attempts: int = 0
    upstream_ms: Optional[int] = None  # time spent in the upstream create call (incl. retries)
    retryable: Optional[bool] = None  # set on failure: can a resume retry this row as is?
    outcome_unknown: bool = False  # create may have happened upstream; check before resending
    updated_at: datetime


class Job(BaseModel):
    """One upload. Counters are updated as rows finish, so they double as progress."""

    batch_id: str
    status: str = JOB_PROCESSING
    filename: Optional[str] = None
    total_rows: int
    created_count: int = 0
    failed_count: int = 0
    activated: bool = False
    activation_error: Optional[str] = None
    error: Optional[str] = None  # batch-level problem (outage stop, restart, bug), not a row error
    resumable_rows: int = 0  # rows a resume would (re)try; set when a run ends
    resume_count: int = 0
    activate_on_partial: bool = False  # the upload's choice, reused by resume
    warnings: List[str] = []  # e.g. ignored CSV columns
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# --- API responses ---------------------------------------------------------------


class HospitalResult(BaseModel):
    row: int
    hospital_id: Optional[int] = None
    name: str
    status: str  # created_and_activated | created | failed | invalid (| pending | processing)
    error: Optional[str] = None
    retryable: Optional[bool] = None  # failed rows only: will a resume retry it?


class CleanupFailure(BaseModel):
    batch_id: str
    error: str


class CleanupResult(BaseModel):
    batches_deleted: int
    hospitals_deleted: int  # records deleted upstream
    orphans_removed: int = 0
    failed: List[CleanupFailure] = []
    clean: bool  # True when nothing failed: the server is in a fresh state


class BulkResult(BaseModel):
    batch_id: str
    status: str  # completed | partial | failed | interrupted | processing
    total_hospitals: int
    processed_hospitals: int
    failed_hospitals: int
    processing_time_seconds: float
    batch_activated: bool
    activation_error: Optional[str] = None
    error: Optional[str] = None  # batch-level problem, e.g. why it was interrupted
    resumable_rows: int = 0  # > 0 means POST /hospitals/bulk/{batch_id}/resume can help
    warnings: List[str] = []
    hospitals: List[HospitalResult]
