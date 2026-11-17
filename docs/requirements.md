# Requirements

Taken from the assignment brief (Paribus, Senior Python Developer). Below are the
requirements, the assumptions made where the brief was silent, and what is out of scope.
The README has a coverage table that points to where each requirement is implemented.

## Functional

1. `POST /hospitals/bulk` accepts a CSV file with the columns `name,address,phone`
   (`phone` optional), at most **20 hospitals**.
2. Validate the CSV.
3. Generate a batch id (UUID).
4. Create each hospital with `POST /hospitals/` on the Hospital Directory API
   (`https://hospital-directory.onrender.com`), passing `creation_batch_id`.
5. Once every hospital is created, activate the batch:
   `PATCH /hospitals/batch/{batch_id}/activate`.
6. Return `batch_id`, `total_hospitals`, `processed_hospitals`, `failed_hospitals`,
   `processing_time_seconds`, `batch_activated`, and `hospitals[]` with
   `row, hospital_id, name, status`.

## Technical

- Python with FastAPI or Flask. FastAPI was chosen. Tested on Python 3.11 locally; the
  Docker image uses 3.12.
- Storage: in-memory is acceptable.
- Deploy online (Render) and submit the public URL and the Git repository.

## Bonus items (all done)

- Progress tracking (WebSocket or polling).
- Resume or retry of a failed batch.
- A CSV validation endpoint.
- Comprehensive tests.
- Dockerfile and docker-compose.
- Performance.

## Assumptions (where the brief was silent)

| Question | Assumption | Ref |
| --- | --- | --- |
| Some rows fail upstream | Keep the created rows, report the failed ones, and **don't activate** the batch unless `activate_on_partial=true`; it can be activated later | D2, D13 |
| Some rows are malformed | Process the valid rows; record each bad row as `invalid` with a reason. Reject the whole file (422) only for file-level problems | D19 |
| Meaning of `processed_hospitals` | Rows created upstream | O1.3 |
| Meaning of `failed_hospitals` | Rows that failed upstream **plus** invalid rows | O1.3 |
| Meaning of `total_hospitals` | Every data row in the file, invalid ones included | O1.3 |
| `processing_time_seconds` after a resume | Time of the latest run | D26 |
| Duplicate hospitals, phone format | The upstream's rules; we report its errors | D20, D24 |
| Extra columns | Ignored, with a warning | D17 |
| The upstream is slow (~6 s per create) and has no idempotency key | Send creates concurrently (10 at a time); never blindly retry a create that may have been applied | D22, D23 |
| Storage | JSON files behind a repository interface rather than plain memory, so per-row state survives a restart and resume works | D6 |

## Out of scope

- Authentication.
- Running more than one process (needs a DB-backed queue; see DESIGN §8.7).
- A production database (the interface is ready; DESIGN §7.3).
- Keeping the original CSV file (D16).
- WebSockets (polling meets the brief).
