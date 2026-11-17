"""CSV parsing and validation.

Two levels of problems (D19):

- **File-level** (`errors`): the file as a whole can't be trusted or processed, e.g. a
  missing required column, wrong encoding, too many rows, or broken CSV quoting. The
  upload is rejected with 422 and nothing is created.
- **Row-level** (`invalid`): one row's data is not correct (e.g. empty `address`, or more
  values than columns). That row is recorded as `invalid` with a message and is never
  sent upstream; the valid rows are processed. If *every* row is invalid there is
  nothing to do, so that becomes a file-level error.

Unknown extra columns are ignored with a warning (D17): data is read by column name,
so they can't corrupt anything. Row checks mirror the upstream's published contract
(`name` and `address` required, non-empty) and nothing stricter (D20, D24).
"""
import csv
import io
from typing import List, NamedTuple, Optional

from .models import HospitalRow, RowError

REQUIRED_COLUMNS = ("name", "address")
OPTIONAL_COLUMNS = ("phone",)
ALLOWED_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


class InvalidRow(NamedTuple):
    """A data row that can't be sent upstream, with every reason why."""

    row: int
    name: str
    address: str
    phone: Optional[str]
    errors: List[RowError]

    @property
    def message(self) -> str:
        return "Row data is not correct: " + "; ".join(e.message for e in self.errors)


class ParsedCSV(NamedTuple):
    rows: List[HospitalRow]  # valid rows, ready to send
    invalid: List[InvalidRow]  # rows recorded as `invalid`, never sent
    errors: List[RowError]  # file-level: non-empty means reject the whole upload
    warnings: List[str]

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.invalid)

    @property
    def row_errors(self) -> List[RowError]:
        return [e for r in self.invalid for e in r.errors]


def _reject(message: str) -> ParsedCSV:
    return ParsedCSV([], [], [RowError(row=0, message=message)], [])


def parse_csv(raw: bytes, max_rows: int, max_bytes: int) -> ParsedCSV:
    """Parse and validate CSV bytes, collecting every problem in one pass."""
    if len(raw) > max_bytes:
        return _reject(f"File exceeds {max_bytes} bytes")
    if not raw.strip():
        return _reject("File is empty")

    try:
        text = raw.decode("utf-8-sig")  # tolerate a BOM written by Excel
    except UnicodeDecodeError:
        return _reject("File must be UTF-8 encoded")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return _reject("File is empty")
    except csv.Error as exc:
        return _reject(f"Malformed CSV: {exc}")

    columns = [h.strip().lower() for h in header]
    errors: List[RowError] = []
    warnings: List[str] = []

    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        errors.append(
            RowError(row=0, message=f"Missing required column(s): {', '.join(missing)}")
        )
    if len(set(columns)) != len(columns):
        errors.append(RowError(row=0, message="Duplicate column names in header"))
    if errors:
        return ParsedCSV([], [], errors, [])
    unknown = [c for c in columns if c not in ALLOWED_COLUMNS]
    if unknown:
        warnings.append(f"Ignored column(s): {', '.join(unknown)}")

    rows: List[HospitalRow] = []
    invalid: List[InvalidRow] = []
    number = 0  # 1-based index of non-blank data rows
    try:
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue  # skip blank lines
            number += 1
            row_errors: List[RowError] = []
            extra = record[len(columns):]
            if any(v.strip() for v in extra):
                # Usually an unquoted comma inside a field: every later value has shifted,
                # so truncating would store wrong data (D18). Trailing empty cells are fine.
                row_errors.append(
                    RowError(
                        row=number,
                        message=f"{len(record)} values for {len(columns)} columns "
                        "(check for an unquoted comma)",
                    )
                )
            # Short rows: missing trailing cells count as empty.
            data = {c: (record[i].strip() if i < len(record) else "") for i, c in enumerate(columns)}
            row_errors += [
                RowError(row=number, field=f, message=f"'{f}' is required")
                for f in REQUIRED_COLUMNS
                if not data.get(f)
            ]
            phone = data.get("phone") or None
            if row_errors:
                invalid.append(
                    InvalidRow(number, data["name"], data["address"], phone, row_errors)
                )
            else:
                rows.append(
                    HospitalRow(row=number, name=data["name"], address=data["address"], phone=phone)
                )
    except csv.Error as exc:
        # Broken quoting: rows after this point can't be trusted, so reject the file.
        return _reject(f"Malformed CSV near data row {number + 1}: {exc}")

    total = number
    if total == 0:
        return _reject("CSV has a header but no data rows")
    if max_rows and total > max_rows:  # max_rows == 0 means unlimited
        return _reject(f"CSV has {total} rows; maximum is {max_rows}")
    if not rows:
        errors.append(RowError(row=0, message="No valid rows: every row has a problem"))
    return ParsedCSV(rows, invalid, errors, warnings)
