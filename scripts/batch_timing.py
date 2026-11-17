"""Per-row timing for a stored batch, read from DATA_DIR/jobs/<batch_id>/.

    python3 scripts/batch_timing.py <batch_id>        # summary + 10 slowest rows
    python3 scripts/batch_timing.py <batch_id> --all  # every row

`upstream_ms` on each record is the time spent in the upstream create call (including
our retries). When rows ran one at a time, wall time minus upstream time is our own
overhead (storage, activation). When they overlapped (UPSTREAM_CONCURRENCY > 1), the
ratio of upstream time to wall time is the speed-up from concurrency.
"""
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d = Path(os.getenv("DATA_DIR", "data")) / "jobs" / sys.argv[1]
    job = json.loads((d / "job.json").read_text())
    records = sorted(json.loads((d / "records.json").read_text()), key=lambda r: r["row"])
    timed = [r for r in records if r.get("upstream_ms") is not None]
    if not timed:
        sys.exit("No timed rows (batch still pending, or created before upstream_ms existed).")

    secs = [r["upstream_ms"] / 1000 for r in timed]
    print(f"batch          {job['batch_id']}  ({job['status']}, {job.get('filename')})")
    print(f"rows timed     {len(timed)} / {job['total_rows']}")
    print(f"upstream/row   avg {statistics.mean(secs):.2f}s  median {statistics.median(secs):.2f}s  "
          f"min {min(secs):.2f}s  max {max(secs):.2f}s")
    print(f"upstream sum   {sum(secs):.1f}s ({sum(secs) / 60:.1f} min)")
    if job.get("finished_at"):
        ts = datetime.fromisoformat
        total = (ts(job["finished_at"]) - ts(job["started_at"])).total_seconds()
        print(f"job wall time  {total:.1f}s ({total / 60:.1f} min)")
        if total >= sum(secs):  # sequential: the gap is our own work
            print(f"our overhead   {total - sum(secs):.1f}s  (storage, activation, scheduling)")
        else:  # rows overlapped
            print(f"speed-up       {sum(secs) / total:.1f}x from concurrent creates "
                  f"(upstream time {sum(secs):.1f}s done in {total:.1f}s)")

    show_all = "--all" in sys.argv
    listing = timed if show_all else sorted(timed, key=lambda r: -r["upstream_ms"])[:10]
    print("\n row  status                   upstream" + ("" if show_all else "   (10 slowest)"))
    for r in listing:
        print(f"{r['row']:>4}  {r['status']:<23} {r['upstream_ms'] / 1000:7.2f}s")


if __name__ == "__main__":
    main()
