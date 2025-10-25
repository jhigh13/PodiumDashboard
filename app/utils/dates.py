"""Date utilities for handling sandbox offsets and effective timelines."""
from __future__ import annotations

from datetime import date, timedelta

from app.utils.settings import settings


def get_effective_today() -> date:
    """Return the logical "today" taking sandbox offset into account."""
    offset = settings.sandbox_current_day_offset
    if offset <= 0:
        return date.today()
    return date.today() - timedelta(days=offset)
