from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete
from app.data.db import get_session
from app.models.tables import OAuthToken


def store_token(athlete_id: int, token: dict):
    expires_in = token.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    with get_session() as session:
        # remove old tokens for athlete
        session.execute(delete(OAuthToken).where(OAuthToken.athlete_id == athlete_id))
        record = OAuthToken(
            athlete_id=athlete_id,
            access_token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            expires_at=expires_at,
            scope=token.get("scope"),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_token(athlete_id: int):
    with get_session() as session:
        stmt = select(OAuthToken).where(OAuthToken.athlete_id == athlete_id)
        return session.execute(stmt).scalars().first()


def delete_token(athlete_id: int):
    """Remove stored token for an athlete (used after refresh failure)."""
    with get_session() as session:
        session.execute(delete(OAuthToken).where(OAuthToken.athlete_id == athlete_id))
        session.commit()


def find_coach_token():
    """Return an OAuthToken that has coach:athletes scope, if any.

    Chooses the most recent (by expires_at) token with the required scope.
    """
    with get_session() as session:
        stmt = select(OAuthToken).order_by(OAuthToken.expires_at.desc())
        for tok in session.execute(stmt).scalars().all():
            scope = (tok.scope or "").lower()
            if "coach:athletes" in scope:
                return tok
    return None
