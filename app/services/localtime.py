"""Cairo ↔ UTC conversion for admin-entered times.

The database stores naive UTC everywhere. An admin typing into a
`datetime-local` field is typing Cairo wall-clock, so anything read off a
form has to be converted in, and anything shown back has to be converted
out. Egypt observes DST again since 2023, so this can't be a fixed +2.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    CAIRO = ZoneInfo("Africa/Cairo")
except ZoneInfoNotFoundError:  # slim container with no tzdata
    CAIRO = timezone(timedelta(hours=2))


def cairo_to_utc(naive_local: datetime) -> datetime:
    """Cairo wall-clock (naive) → naive UTC, ready to store."""
    return naive_local.replace(tzinfo=CAIRO).astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_cairo(naive_utc: datetime) -> datetime:
    """Naive UTC out of the database → Cairo wall-clock, ready to show."""
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(CAIRO).replace(tzinfo=None)
