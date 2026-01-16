from sqlalchemy import delete

from app.data.db import get_session
from app.models.tables import CoachRosterMember
from app.services.tp_api import get_api
from app.services.athletes import upsert_athlete


def sync_coach_roster(athlete_id: int | None = None, coach_athlete_id: int | None = None) -> dict:
    """Fetch the coach's athlete roster from TrainingPeaks and upsert locally.

    The athlete_id is used only to resolve an API client/token; it can be any
    authenticated athlete for whom we have a token with coach:athletes scope
    (typically the coach's own token).

    Returns a summary dict with counts and sample data.
    """
    api = get_api(athlete_id)
    roster = api.fetch_coach_athletes()
    inserted = 0
    updated = 0
    results = []

    # If coach_athlete_id provided, rebuild the roster membership mapping.
    if coach_athlete_id is not None:
        with get_session() as session:
            session.execute(delete(CoachRosterMember).where(CoachRosterMember.coach_athlete_id == int(coach_athlete_id)))
            session.commit()
    for item in roster:
        tp_id = item.get('Id') or item.get('id')
        if not tp_id:
            continue
        first = item.get('FirstName') or ''
        last = item.get('LastName') or ''
        email = item.get('Email')
        name = (first + ' ' + last).strip() or None
        athlete = upsert_athlete(tp_athlete_id=tp_id, name=name, email=email)

        if coach_athlete_id is not None:
            with get_session() as session:
                session.add(CoachRosterMember(coach_athlete_id=int(coach_athlete_id), athlete_id=int(athlete.id)))
                try:
                    session.commit()
                except Exception:
                    session.rollback()

        results.append({
            'id': athlete.id,
            'tp_athlete_id': athlete.tp_athlete_id,
            'name': athlete.name,
            'email': athlete.email,
        })
    return {
        'count': len(results),
        'athletes': results[:10],  # include a small sample
    }
