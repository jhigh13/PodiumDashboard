from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.utils.settings import settings


def _force_psycopg_driver(url: str) -> str:
    """Force SQLAlchemy to use psycopg (v3) instead of psycopg2.

    SQLAlchemy's default "postgresql://" dialect historically targets psycopg2.
    This project depends on psycopg (v3) via `psycopg[binary]`, so we rewrite
    URLs to the explicit driver form.
    """
    if not url:
        return url
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def _ssl_connect_args(url: str) -> dict:
    # Supabase Postgres typically requires sslmode=require.
    # If user already set sslmode in URL, don't override.
    if not url:
        return {}
    if "sslmode=" in url:
        return {}
    return {"sslmode": "require"}


def get_triathlon_engine():
    url = settings.triathlon_database_url
    if not url:
        return None
    url = _force_psycopg_driver(url)
    engine = create_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=_ssl_connect_args(url),
    )

    # psycopg v3 + Supabase PgBouncer pooler connections get an empty
    # search_path, causing "relation does not exist" for unqualified names.
    # Set it explicitly on every connection checkout.
    @event.listens_for(engine, "checkout")
    def _set_search_path(dbapi_conn, conn_record, conn_proxy):
        with dbapi_conn.cursor() as cursor:
            cursor.execute("SET search_path = public")

    return engine


def get_triathlon_sessionmaker():
    engine = get_triathlon_engine()
    if engine is None:
        return None
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
