from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import OperationalError
import time
from sqlalchemy.orm import sessionmaker
from app.utils.settings import settings
from app.models.base import Base
from app.models import tables  # noqa: F401 ensure models imported

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,           # Validate connections before using (avoids stale/closed connections)
    pool_recycle=1800,            # Recycle connections every 30 minutes to avoid server timeouts
    connect_args={"sslmode": "require"},  # Required by many hosted Postgres providers (e.g., Supabase)
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def get_session():
    return SessionLocal()


def ensure_schema():
    """Lightweight schema patch: add newly introduced columns if missing.

    This replaces a proper migration system for now (Option A). Add any future
    ad-hoc ALTER TABLE statements here guarded by existence checks.
    """
    insp = inspect(engine)
    # athletes.tp_athlete_id (Integer) added in code after initial table creation
    athlete_cols = {c['name'] for c in insp.get_columns('athletes')}
    if 'tp_athlete_id' not in athlete_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE athletes ADD COLUMN tp_athlete_id INTEGER"))

def init_db():
    """Create tables then apply simple schema patches if needed, with transient retry."""
    backoffs = [1, 3, 5]
    last_err = None
    for delay in [0] + backoffs:
        if delay:
            time.sleep(delay)
        try:
            Base.metadata.create_all(bind=engine)
            ensure_schema()
            return
        except OperationalError as e:
            # Capture and retry on transient server-closed-connection errors
            last_err = e
            continue
    # If we exhausted retries, re-raise the last error for visibility
    if last_err:
        raise last_err
