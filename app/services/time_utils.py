"""UTC datetime helpers for Postgres timestamp-without-time-zone columns."""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now_naive() -> datetime:
    """Return current UTC as a naive datetime for TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc_naive(value: datetime) -> datetime:
    """Normalize aware or naive datetimes to naive UTC."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc_aware(value: datetime) -> datetime:
    """Normalize aware or naive UTC datetimes to aware UTC for comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
