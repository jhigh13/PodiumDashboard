from sqlalchemy import select
from app.data.db import get_session
from app.models.tables import Athlete

HARD_CODED_ATHLETE = {
    "external_id": "athlete_demo_1",
    "name": "Demo Athlete",
    "email": "demo@example.com",
}

def get_or_create_demo_athlete():
    with get_session() as session:
        stmt = select(Athlete).where(Athlete.external_id == HARD_CODED_ATHLETE["external_id"])
        existing = session.execute(stmt).scalars().first()
        if existing:
            return existing
        athlete = Athlete(**HARD_CODED_ATHLETE)
        session.add(athlete)
        session.commit()
        session.refresh(athlete)
        return athlete


def get_athlete_by_id(athlete_id: int) -> Athlete | None:
    """Fetch an athlete by internal id."""
    with get_session() as session:
        stmt = select(Athlete).where(Athlete.id == athlete_id)
        return session.execute(stmt).scalars().first()


def list_athletes() -> list[Athlete]:
    """Return all athletes in the local database (for coach mode selection)."""
    with get_session() as session:
        stmt = select(Athlete).order_by(Athlete.name)
        return session.execute(stmt).scalars().all()


def upsert_athlete(tp_athlete_id: int, name: str | None = None, email: str | None = None, external_id: str | None = None) -> Athlete:
    """Create or update a local Athlete row from a TrainingPeaks roster entry."""
    with get_session() as session:
        # Prefer matching by tp_athlete_id if present
        stmt = select(Athlete).where(Athlete.tp_athlete_id == tp_athlete_id)
        existing = session.execute(stmt).scalars().first()
        if existing:
            # Update fields if provided
            if name and existing.name != name:
                existing.name = name
            if email and existing.email != email:
                existing.email = email
            session.commit()
            session.refresh(existing)
            return existing
        # Else try external_id if provided
        if external_id:
            stmt2 = select(Athlete).where(Athlete.external_id == external_id)
            existing2 = session.execute(stmt2).scalars().first()
            if existing2:
                existing2.tp_athlete_id = tp_athlete_id
                if name and existing2.name != name:
                    existing2.name = name
                if email and existing2.email != email:
                    existing2.email = email
                session.commit()
                session.refresh(existing2)
                return existing2
        # Create new
        athlete = Athlete(
            external_id=external_id or f"tp_{tp_athlete_id}",
            tp_athlete_id=tp_athlete_id,
            name=name or f"Athlete {tp_athlete_id}",
            email=email,
        )
        session.add(athlete)
        session.commit()
        session.refresh(athlete)
        return athlete
