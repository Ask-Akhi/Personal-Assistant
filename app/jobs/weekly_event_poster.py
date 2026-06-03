# -*- coding: utf-8 -*-
"""Weekly auto-poster: every Tuesday 7 PM AET, create a T20 Cricket event
for the coming Saturday 6:30 AM - 10:30 AM at Coolong Reserve,
then post the announcement to the WhatsApp group.

AET = Australia/Sydney timezone (handles DST automatically).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytz

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import log
from app.models import CricketEvent, EventStatus
from app.services.time_utils import as_utc_naive

AET = pytz.timezone("Australia/Sydney")

WEEKLY_TITLE    = "CHCC Members - T20 Cricket"
WEEKLY_VENUE    = "Coolong Reserve"
WEEKLY_GROUP    = "CHCC Members"  # Castle Hill Cricket Community
WEEKLY_START    = (6, 30)   # 6:30 AM
WEEKLY_END      = (10, 30)  # 10:30 AM
CUTOFF_HOURS    = 36.0      # close RSVP 36 hrs before event


def _next_tuesday_7pm_aet() -> datetime:
    """Return the next Tuesday 7 PM AET as a timezone-aware datetime."""
    now = datetime.now(AET)
    days_ahead = (1 - now.weekday()) % 7   # Tuesday = weekday 1
    if days_ahead == 0 and now.hour >= 19:
        days_ahead = 7                      # already past 7 PM Tuesday, wait a week
    target = now.replace(hour=19, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    return target


def _next_saturday(reference: datetime) -> datetime:
    """Return the Saturday after the given reference date at 6:30 AM AET."""
    days_ahead = (5 - reference.weekday()) % 7   # Saturday = weekday 5
    if days_ahead == 0:
        days_ahead = 7
    sat = reference.replace(
        hour=WEEKLY_START[0], minute=WEEKLY_START[1], second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    return sat


async def _already_exists_for(event_at: datetime) -> bool:
    """Return True if an open/closed event already exists for this Saturday."""
    from sqlalchemy import select
    async with session_scope() as s:
        # Check within +/- 12 hours of the target datetime
        lo = as_utc_naive(event_at - timedelta(hours=12))
        hi = as_utc_naive(event_at + timedelta(hours=12))
        row = (await s.execute(
            select(CricketEvent).where(
                CricketEvent.event_at >= lo,
                CricketEvent.event_at <= hi,
            ).limit(1)
        )).scalar_one_or_none()
        return row is not None


async def _create_and_announce(event_at: datetime) -> None:
    """Create the CricketEvent row and post the WA announcement."""
    from app.services import wa_sender, audit
    from app.services.telegram_notify import send_admin_message

    settings = get_settings()
    cutoff_at = event_at - timedelta(hours=CUTOFF_HOURS)
    cutoff_at_utc = cutoff_at.astimezone(pytz.utc).replace(tzinfo=None)
    event_at_utc  = event_at.astimezone(pytz.utc).replace(tzinfo=None)

    # Auto-lookup group ID from registry (no manual config needed)
    from app.services.group_registry import get_group_for_task
    async with session_scope() as s:
        group_id = await get_group_for_task(s, "weekly_cricket")
    # Fallback to env var if registry has nothing yet
    if not group_id:
        group_id = settings.whatsapp_group_id or None

    async with session_scope() as s:
        event = CricketEvent(
            title=WEEKLY_TITLE,
            event_at=event_at_utc,
            venue=WEEKLY_VENUE,
            group_wa_id=group_id or None,
            cutoff_hours=CUTOFF_HOURS,
            cutoff_at=cutoff_at_utc,
            status=EventStatus.open,
        )
        s.add(event)
        await s.flush()
        event_id = event.id

        await audit.record(
            s,
            actor="weekly_event_poster",
            action="event_created",
            target=str(event_id),
            payload={"title": WEEKLY_TITLE, "event_at": str(event_at)},
        )

    # Build announcement message
    day_str   = event_at.strftime("%A, %d %b %Y")
    start_str = event_at.strftime("%-I:%M %p")
    end_str   = event_at.replace(
        hour=WEEKLY_END[0], minute=WEEKLY_END[1]
    ).strftime("%-I:%M %p")

    announcement = (
        f"Hi CHCC Members! 👋\n\n"
        f"🏏 *T20 Cricket - This Saturday!*\n\n"
        f"📅 {day_str}\n"
        f"⏰ {start_str} - {end_str}\n"
        f"📍 Coolong Reserve\n\n"
        f"Please reply:\n"
        f"✅ *YES* - Coming\n"
        f"🏏 *WNBO* - Coming but Will Not Bowl\n"
        f"❌ *NO* - Can't make it\n\n"
        f"📋 Calling list will be shared in 36 hours. See you on the field! 🏆"
    )

    # Send to WhatsApp group
    if group_id:
        try:
            wa_msg_id = await wa_sender.send_text(group_id, announcement)
            async with session_scope() as s:
                ev = await s.get(CricketEvent, event_id)
                if ev:
                    ev.announcement_wa_msg_id = wa_msg_id
            log.info("weekly_poster.wa_sent", event_id=event_id)
        except Exception as exc:
            log.error("weekly_poster.wa_failed", error=str(exc))
            await send_admin_message(
                f"<b>Weekly event created</b> but WA send failed!\n"
                f"Error: <code>{str(exc)[:300]}</code>\n\n"
                f"Copy &amp; paste manually:\n<pre>{announcement}</pre>"
            )
    else:
        # No group ID yet - send to Telegram for manual copy
        await send_admin_message(
            f"<b>Weekly event created (no WA group ID set)</b>\n"
            f"Copy &amp; paste to your group:\n\n<pre>{announcement}</pre>"
        )

    # Always confirm on Telegram
    await send_admin_message(
        f"<b>Weekly event posted!</b>\n"
        f"Event #{event_id}: {WEEKLY_TITLE} on {day_str}\n"
        f"RSVP window open until {cutoff_at.strftime('%a %d %b, %-I:%M %p AET')}"
    )


async def run() -> None:
    """Main loop: sleep until next Tuesday 7 PM AET, then post."""
    log.info("weekly_poster.started")

    # --- Catch-up on startup: if today is Tuesday and we haven't posted yet, fire now ---
    now = datetime.now(AET)
    if now.weekday() == 1:  # Tuesday
        saturday = _next_saturday(now)
        if not await _already_exists_for(saturday):
            log.info("weekly_poster.catchup_fire", reason="Tuesday startup, no event found yet")
            try:
                await _create_and_announce(saturday)
            except Exception as exc:
                log.error("weekly_poster.failed", error=str(exc))
            await asyncio.sleep(60)

    while True:
        fire_at = _next_tuesday_7pm_aet()
        now = datetime.now(AET)
        wait_seconds = (fire_at - now).total_seconds()

        log.info(
            "weekly_poster.next_fire",
            fire_at=fire_at.strftime("%Y-%m-%d %H:%M AET"),
            wait_hours=round(wait_seconds / 3600, 1),
        )

        await asyncio.sleep(max(wait_seconds, 0))

        # Double-check we haven't already posted for this Saturday
        saturday = _next_saturday(datetime.now(AET))
        if await _already_exists_for(saturday):
            log.info("weekly_poster.skipped_duplicate", saturday=str(saturday))
            await asyncio.sleep(60)
            continue

        try:
            await _create_and_announce(saturday)
        except Exception as exc:
            log.error("weekly_poster.failed", error=str(exc))

        # Sleep 60s to avoid double-fire on restart right after posting
        await asyncio.sleep(60)
