"""Persistence helpers for recovery alert evaluations."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from sqlalchemy import delete, select

from app.data.db import get_session
from app.models.tables import RecoveryAlertRun


def upsert_recovery_alert_run(
    athlete_id: int,
    alert_date: date,
    *,
    threshold: float,
    triggered: bool,
    reason: str,
    metrics: Dict[str, Any],
) -> None:
    with get_session() as session:
        # Idempotent: keep one row per athlete/day.
        session.execute(
            delete(RecoveryAlertRun).where(
                RecoveryAlertRun.athlete_id == int(athlete_id),
                RecoveryAlertRun.alert_date == alert_date,
            )
        )
        session.add(
            RecoveryAlertRun(
                athlete_id=int(athlete_id),
                alert_date=alert_date,
                threshold=float(threshold),
                triggered=bool(triggered),
                reason=str(reason or ""),
                metrics=metrics or {},
            )
        )
        session.commit()


def list_recovery_alert_runs(
    athlete_id: int,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = 200,
) -> list[RecoveryAlertRun]:
    with get_session() as session:
        stmt = select(RecoveryAlertRun).where(RecoveryAlertRun.athlete_id == int(athlete_id))
        if start is not None:
            stmt = stmt.where(RecoveryAlertRun.alert_date >= start)
        if end is not None:
            stmt = stmt.where(RecoveryAlertRun.alert_date <= end)
        stmt = stmt.order_by(RecoveryAlertRun.alert_date.desc()).limit(int(limit))
        return session.execute(stmt).scalars().all()
