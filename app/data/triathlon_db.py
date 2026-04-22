from __future__ import annotations

from sqlalchemy import create_engine
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
    connect_args = _ssl_connect_args(url)
    # 10-second statement timeout prevents chart queries from hanging on Supabase
    connect_args["options"] = "-c statement_timeout=10000"
    return create_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


def get_triathlon_sessionmaker():
    engine = get_triathlon_engine()
    if engine is None:
        return None
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
