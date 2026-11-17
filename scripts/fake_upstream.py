"""Tiny stand-in for the Hospital Directory API, for manual testing.

    uvicorn scripts.fake_upstream:app --port 9000

Then run the bulk app with  UPSTREAM_BASE_URL=http://localhost:9000
Any hospital whose name contains "FAIL" is rejected with a 422, so you can see partial
failures. Everything is in memory. Set FAKE_UPSTREAM_DELAY=0.3 (seconds per create) to
slow it down so the UI's progress bar is visible.
"""
import itertools
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Fake Hospital Directory")
_ids = itertools.count(1)
_db: Dict[int, dict] = {}
DELAY = float(os.getenv("FAKE_UPSTREAM_DELAY", "0"))


class HospitalCreate(BaseModel):
    name: str
    address: str
    phone: Optional[str] = None
    creation_batch_id: Optional[str] = None


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/hospitals/", status_code=201)
def create(h: HospitalCreate):
    if DELAY:
        time.sleep(DELAY)
    if "FAIL" in h.name:
        raise HTTPException(422, f"Simulated upstream rejection for '{h.name}'")
    row = {
        "id": next(_ids),
        **h.model_dump(),
        "active": h.creation_batch_id is None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _db[row["id"]] = row
    return row


@app.get("/hospitals/")
def list_all():
    return list(_db.values())


@app.get("/hospitals/batch/{batch_id}")
def by_batch(batch_id: str):
    return [h for h in _db.values() if h["creation_batch_id"] == batch_id]


@app.patch("/hospitals/batch/{batch_id}/activate")
def activate(batch_id: str):
    hits = [h for h in _db.values() if h["creation_batch_id"] == batch_id]
    for h in hits:
        h["active"] = True
    return {"activated_count": len(hits)}


@app.delete("/hospitals/batch/{batch_id}")
def delete_batch(batch_id: str):
    ids = [i for i, h in _db.items() if h["creation_batch_id"] == batch_id]
    for i in ids:
        del _db[i]
    return {"deleted_count": len(ids)}
