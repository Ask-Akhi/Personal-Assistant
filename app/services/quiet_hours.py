"""Quiet hours check. Local time per TIMEZONE env."""
from __future__ import annotations

from datetime import datetime

import pytz

from app.config import get_settings


def is_quiet_now() -> bool:
    s = get_settings()
    tz = pytz.timezone(s.timezone)
    h = datetime.now(tz).hour
    start, end = s.quiet_hours_start, s.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= h < end
    # wraps midnight, e.g. 22 -> 8
    return h >= start or h < end
