from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv()

from app.data.db import init_db
from app.services.jobs import (
    claim_next_job,
    heartbeat_job,
    mark_job_failed,
    mark_job_succeeded,
)


def _run_job(job) -> dict:
    """Execute a single job and return a JSON-serializable result."""
    job_type = getattr(job, "job_type", None)
    payload = getattr(job, "payload", None) or {}

    if job_type == "sync_roster":
        from app.services.coach_roster import sync_coach_roster

        coach_id = getattr(job, "requested_by_athlete_id", None)
        if not coach_id:
            raise RuntimeError("sync_roster job missing requested_by_athlete_id")
        return sync_coach_roster(athlete_id=int(coach_id), coach_athlete_id=int(coach_id))

    if job_type == "sync_recent":
        from app.services.ingest import ingest_recent

        days = int(payload.get("days", 7) or 7)
        target = getattr(job, "target_athlete_id", None)
        if not target:
            raise RuntimeError("sync_recent job missing target_athlete_id")
        return ingest_recent(days=days, athlete_id=int(target))

    raise RuntimeError(f"Unknown job_type: {job_type}")


def main(poll_seconds: float = 1.0) -> int:
    init_db()
    print("[worker] started")

    while True:
        job = claim_next_job()
        if not job:
            time.sleep(poll_seconds)
            continue

        job_id = int(job.id)
        print(f"[worker] claimed job {job_id} ({job.job_type})")
        try:
            heartbeat_job(job_id)
            result = _run_job(job)
            mark_job_succeeded(job_id, result=result if isinstance(result, dict) else {"result": str(result)})
            print(f"[worker] job {job_id} succeeded")
        except Exception as exc:  # noqa: BLE001
            mark_job_failed(job_id, error=str(exc))
            print(f"[worker] job {job_id} failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
