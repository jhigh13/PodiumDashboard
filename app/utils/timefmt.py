"""Time-string parsing and formatting helpers for triathlon split data.

Triathlon race results in the triathlon-db come back as text columns
(e.g. ``swimtime = "00:09:22"``). These helpers convert to/from seconds
and produce display strings tuned for swim/bike/run/transition magnitudes.
"""
from __future__ import annotations

import re
import unicodedata


_TIME_RE = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d{2}(?:\.\d+)?)\s*$")


def time_to_seconds(value: object) -> float | None:
    """Parse a split string to seconds. Returns None for empty/invalid/zero."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "n.a", "n.a.", "dnf", "dns", "dsq", "lap"}:
        return None
    if s in {"00:00:00", "0:00:00", "00:00", "0:00"}:
        return None
    m = _TIME_RE.match(s)
    if not m:
        try:
            n = float(s)
            return n if n > 0 else None
        except ValueError:
            return None
    h_s, m_s, sec_s = m.group(1), m.group(2), m.group(3)
    h = int(h_s) if h_s else 0
    minutes = int(m_s)
    seconds = float(sec_s)
    total = h * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


EMPTY_TIME = "--:--"


def seconds_to_hms(seconds: float | None) -> str:
    """Format as H:MM:SS (always with hour, e.g. ``1:43:38``)."""
    if seconds is None:
        return EMPTY_TIME
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def seconds_to_mmss(seconds: float | None) -> str:
    """Format as MM:SS (zero-padded minutes)."""
    if seconds is None:
        return EMPTY_TIME
    seconds = int(round(seconds))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def format_segment_time(seconds: float | None, *, segment: str | None = None) -> str:
    """Smart format: H:MM:SS when >= 1 hour, otherwise MM:SS.

    The optional ``segment`` argument is accepted for callers that want to
    document intent (e.g. ``segment="overall"``) but the format is decided
    purely by magnitude. Olympic-distance bike legs cross 1 hour while
    sprint swim legs do not.
    """
    if seconds is None:
        return EMPTY_TIME
    if seconds >= 3600:
        return seconds_to_hms(seconds)
    return seconds_to_mmss(seconds)


def format_gap(seconds: float | None, *, signed: bool = False) -> str:
    """Format a time gap. ``signed`` shows +/- prefix; otherwise absolute."""
    if seconds is None:
        return EMPTY_TIME
    sign = ""
    if signed:
        sign = "+" if seconds > 0 else ("-" if seconds < 0 else "")
    return f"{sign}{seconds_to_mmss(abs(seconds))}"


# Canonical display order + labels for World Triathlon distance categories.
DISTANCE_DISPLAY: dict[str, str] = {
    "super_sprint": "Super-Sprint",
    "sprint": "Sprint",
    "standard": "Olympic",
    "olympic": "Olympic",
    "middle_distance": "Middle Distance",
    "long_distance": "Long Distance",
    "middle": "Middle Distance",
    "long": "Long Distance",
}

_DISTANCE_ORDER: list[str] = [
    "super_sprint", "sprint", "standard", "olympic",
    "middle_distance", "middle", "long_distance", "long",
]


def distance_label(raw: str | None) -> str:
    if not raw:
        return ""
    return DISTANCE_DISPLAY.get(str(raw).strip().lower(), str(raw).replace("_", " ").title())


def distance_sort_key(raw: str | None) -> tuple[int, str]:
    s = (str(raw or "")).strip().lower()
    try:
        return (_DISTANCE_ORDER.index(s), s)
    except ValueError:
        return (len(_DISTANCE_ORDER), s)


def slugify_athlete_name(full_name: str) -> str:
    """Convert an athlete name to a URL-safe slug.

    Examples:
      "Hayden Wilde"          -> "hayden-wilde"
      "Léo Bergère"           -> "leo-bergere"
      "O'Connor, Jamie"       -> "oconnor-jamie"
    """
    if not full_name:
        return ""
    # NFKD strips accents into combining marks, then encode-ignore drops them.
    norm = unicodedata.normalize("NFKD", full_name)
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    ascii_only = ascii_only.lower().replace("'", "").replace("’", "")
    # Replace any run of non-alnum with a single dash.
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return slug


def humanize_elapsed_since(days: int) -> str:
    """Render ``days`` ago as a short string (1y 7m, 3m, 12d)."""
    if days < 0:
        return "—"
    if days < 31:
        return f"{days}d"
    months = days // 30
    if months < 12:
        return f"{months}m"
    years = months // 12
    rem_months = months - years * 12
    if rem_months == 0:
        return f"{years}y"
    return f"{years}y {rem_months}m"
