"""In-memory slug -> athlete index for the /compare page.

Builds once from the triathlon-db ``athlete`` table; refreshes lazily after
a TTL. Names are slugified and collisions are disambiguated by suffixing
the athlete's country.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
import time
from typing import Iterable

from sqlalchemy import text

from app.data.triathlon_db import get_triathlon_engine
from app.utils.timefmt import slugify_athlete_name


_REFRESH_SECONDS = 3600  # 1 hour


@dataclass(frozen=True)
class AthleteEntry:
    athlete_id: int
    full_name: str
    slug: str
    country: str | None
    gender: str | None


class AthleteIndex:
    def __init__(self, entries: Iterable[AthleteEntry]):
        self._by_id: dict[int, AthleteEntry] = {}
        self._by_slug: dict[str, AthleteEntry] = {}
        self._all: list[AthleteEntry] = []
        for e in entries:
            self._by_id[e.athlete_id] = e
            # First write wins for a slug; collisions get country suffix
            if e.slug not in self._by_slug:
                self._by_slug[e.slug] = e
            self._all.append(e)

    def by_id(self, athlete_id: int) -> AthleteEntry | None:
        return self._by_id.get(athlete_id)

    def by_slug(self, slug: str) -> AthleteEntry | None:
        if not slug:
            return None
        entry = self._by_slug.get(slug)
        if entry is not None:
            return entry
        # Also accept numeric ID slugs
        if slug.isdigit():
            return self._by_id.get(int(slug))
        return None

    def search(self, query: str, *, limit: int = 12) -> list[AthleteEntry]:
        if not query:
            return []
        q = query.strip().lower()
        if not q:
            return []
        results: list[tuple[int, AthleteEntry]] = []
        for e in self._all:
            name_lower = e.full_name.lower()
            # Score: prefix > word-start > substring
            if name_lower.startswith(q):
                score = 0
            elif any(p.startswith(q) for p in name_lower.split()):
                score = 1
            elif q in name_lower:
                score = 2
            elif q in e.slug:
                score = 3
            else:
                continue
            results.append((score, e))
            if len(results) >= limit * 4:
                break
        results.sort(key=lambda x: (x[0], x[1].full_name))
        return [e for _, e in results[:limit]]

    def all_entries(self) -> list[AthleteEntry]:
        return list(self._all)


_index: AthleteIndex | None = None
_loaded_at: float = 0.0
_lock = Lock()


def _build_from_db() -> AthleteIndex:
    engine = get_triathlon_engine()
    if engine is None:
        return AthleteIndex([])
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT a.athlete_id, a.full_name, a.country, a.gender
            FROM athlete a
            WHERE a.full_name IS NOT NULL AND a.full_name <> ''
        """)).fetchall()

    # Group by slug to detect collisions
    by_slug: dict[str, list[tuple[int, str, str | None, str | None]]] = {}
    for athlete_id, full_name, country, gender in rows:
        slug = slugify_athlete_name(full_name)
        if not slug:
            continue
        by_slug.setdefault(slug, []).append((athlete_id, full_name, country, gender))

    entries: list[AthleteEntry] = []
    for slug, group in by_slug.items():
        if len(group) == 1:
            athlete_id, full_name, country, gender = group[0]
            entries.append(AthleteEntry(athlete_id, full_name, slug, country, gender))
        else:
            # Collision: suffix with country (lowercased). If still ambiguous, append athlete_id.
            seen_slugs: set[str] = set()
            for athlete_id, full_name, country, gender in group:
                base = slug
                suffix = (country or "").lower().replace(" ", "")
                candidate = f"{base}-{suffix}" if suffix else f"{base}-{athlete_id}"
                if candidate in seen_slugs:
                    candidate = f"{base}-{athlete_id}"
                seen_slugs.add(candidate)
                entries.append(AthleteEntry(athlete_id, full_name, candidate, country, gender))
    return AthleteIndex(entries)


def get_athlete_index(*, force_refresh: bool = False) -> AthleteIndex:
    global _index, _loaded_at
    now = time.time()
    if (
        not force_refresh
        and _index is not None
        and (now - _loaded_at) < _REFRESH_SECONDS
    ):
        return _index
    with _lock:
        if force_refresh or _index is None or (now - _loaded_at) >= _REFRESH_SECONDS:
            _index = _build_from_db()
            _loaded_at = time.time()
        return _index
