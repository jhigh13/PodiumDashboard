from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.data.db import get_session
from app.models.tables import Job


JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"


def enqueue_job(
    job_type: str,
    requested_by_athlete_id: int | None,
    target_athlete_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Job:
    with get_session() as session:
        job = Job(
            job_type=str(job_type),
            status=JOB_STATUS_QUEUED,
            requested_by_athlete_id=requested_by_athlete_id,
            target_athlete_id=target_athlete_id,
            payload=payload or {},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def get_job(job_id: int) -> Job | None:
    with get_session() as session:
        return session.get(Job, int(job_id))


def claim_next_job() -> Optional[Job]:
    """Claim the next queued job for execution.

    Uses row locking so multiple workers can run safely.
    """
    now = datetime.now(timezone.utc)
    with get_session() as session:
        stmt = (
            select(Job)
            .where(Job.status == JOB_STATUS_QUEUED)
            .order_by(Job.created_at.asc())
            .with_for_update(skip_locked=True)
        )
        job = session.execute(stmt).scalars().first()
        if not job:
            return None
        job.status = JOB_STATUS_RUNNING
        job.started_at = now
        job.heartbeat_at = now
        session.commit()
        session.refresh(job)
        return job


def heartbeat_job(job_id: int) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        job = session.get(Job, int(job_id))
        if not job:
            return
        job.heartbeat_at = now
        session.commit()


def mark_job_succeeded(job_id: int, result: dict[str, Any] | None = None) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        job = session.get(Job, int(job_id))
        if not job:
            return
        job.status = JOB_STATUS_SUCCEEDED
        job.finished_at = now
        job.heartbeat_at = now
        job.result = result or {}
        job.error = None
        session.commit()


def mark_job_failed(job_id: int, error: str) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        job = session.get(Job, int(job_id))
        if not job:
            return
        job.status = JOB_STATUS_FAILED
        job.finished_at = now
        job.heartbeat_at = now
        job.error = str(error or "unknown_error")
        session.commit()
