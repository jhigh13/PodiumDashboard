from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select

from app.data.db import get_session
from app.models.tables import SyncState


def get_or_create_sync_state(athlete_id: int) -> SyncState:
    with get_session() as session:
        row = session.execute(
            select(SyncState).where(SyncState.athlete_id == athlete_id)
        ).scalars().first()
        if row is None:
            row = SyncState(athlete_id=athlete_id)
            session.add(row)
            session.commit()
            session.refresh(row)
        return row


def get_last_training_sync_date(athlete_id: int) -> date | None:
    state = get_or_create_sync_state(athlete_id)
    return state.last_training_sync_date


def set_last_training_sync(athlete_id: int, sync_date: date) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        row = session.execute(
            select(SyncState).where(SyncState.athlete_id == athlete_id)
        ).scalars().first()
        if row is None:
            row = SyncState(athlete_id=athlete_id)
            session.add(row)
        row.last_training_sync_date = sync_date
        row.last_training_sync_at = now
        session.commit()


def get_last_race_sync_date(athlete_id: int) -> date | None:
    state = get_or_create_sync_state(athlete_id)
    return state.last_race_sync_date


def set_last_race_sync(athlete_id: int, sync_date: date) -> None:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        row = session.execute(
            select(SyncState).where(SyncState.athlete_id == athlete_id)
        ).scalars().first()
        if row is None:
            row = SyncState(athlete_id=athlete_id)
            session.add(row)
        row.last_race_sync_date = sync_date
        row.last_race_sync_at = now
        session.commit()
