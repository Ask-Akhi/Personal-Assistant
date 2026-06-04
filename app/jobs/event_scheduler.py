"""Event Scheduler — checks for events past cutoff, builds & sends calling list.

Runs every 5 minutes via the main app scheduler.
Flow:
  1. Find open events where cutoff_at <= now
  2. Build calling list text
  3. Send to Telegram for 30s undo approval
  4. Auto-send to WhatsApp group after undo window
"""
from __future__ import annotations

import asyncio
from sqlalchemy import select

from app.db import session_scope
from app.logging_setup import log
from app.models import CricketEvent, EventStatus
from app.services import event_manager
from app.services.time_utils import utc_now_naive
from app.jobs.weekly_event_poster import WEEKLY_CUTOFF_HOURS, WEEKLY_TITLE


async def run_once() -> None:
    """Check and process any events that have passed their cutoff."""
    now = utc_now_naive()

    await _normalize_weekly_event_cutoffs()

    async with session_scope() as s:
        result = await s.execute(
            select(CricketEvent).where(
                CricketEvent.status == EventStatus.open,
                CricketEvent.cutoff_at <= now,
            )
        )
        events = result.scalars().all()

    for event in events:
        log.info("event_scheduler.cutoff_reached", event_id=event.id, title=event.title)
        await _process_event(event)


async def _normalize_weekly_event_cutoffs() -> None:
    """Keep the weekly CHCC event aligned to the Wednesday 9 PM AET calling-list time."""
    from datetime import timedelta

    desired_offset = timedelta(hours=WEEKLY_CUTOFF_HOURS)

    async with session_scope() as s:
        result = await s.execute(
            select(CricketEvent).where(
                CricketEvent.status == EventStatus.open,
                CricketEvent.title == WEEKLY_TITLE,
            )
        )
        events = result.scalars().all()

        changed = False
        for event in events:
            desired_cutoff = event.event_at - desired_offset
            if event.cutoff_at != desired_cutoff or event.cutoff_hours != WEEKLY_CUTOFF_HOURS:
                event.cutoff_at = desired_cutoff
                event.cutoff_hours = WEEKLY_CUTOFF_HOURS
                changed = True
                log.info(
                    "event_scheduler.weekly_cutoff_normalized",
                    event_id=event.id,
                    title=event.title,
                    cutoff_at=str(desired_cutoff),
                )

        if changed:
            await s.flush()


async def _process_event(event: CricketEvent) -> None:
    """Build calling list, notify Telegram, then auto-send to WA group."""
    from app.config import get_settings
    from app.services.telegram_notify import send_admin_message
    from app.services import wa_sender, audit
    import html

    settings = get_settings()

    async with session_scope() as s:
        calling_list = await event_manager.build_calling_list(s, event)

        # Save calling list text on event row
        db_event = await s.get(CricketEvent, event.id)
        if db_event:
            db_event.calling_list_text = calling_list

        await audit.record(
            s,
            actor="event_scheduler",
            action="calling_list_built",
            target=str(event.id),
            payload={"title": event.title},
        )

    # Send to Telegram with approve/cancel buttons
    await _send_calling_list_card(event, calling_list)


async def _send_calling_list_card(event: CricketEvent, calling_list: str) -> None:
    """Send a Telegram card so the user can approve or cancel the WA send."""
    import httpx
    import html as _html
    from app.config import get_settings
    from app.db import session_scope
    from app.models import CricketEvent, EventStatus
    from app.services import audit

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.allowed_user_ids:
        log.warning("event_scheduler.no_telegram_config")
        return

    text = (
        f"🏏 <b>Cutoff reached — {_html.escape(event.title)}</b>\n\n"
        f"Ready to send calling list to WhatsApp group.\n\n"
        f"<pre>{_html.escape(calling_list[:2000])}</pre>\n\n"
        f"⏱ Auto-sends in 30s unless you cancel."
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "📤 Send Now", "callback_data": f"event:send:{event.id}"},
            {"text": "🛑 Cancel", "callback_data": f"event:cancel:{event.id}"},
        ]]
    }

    async with httpx.AsyncClient(timeout=10) as client:
        for uid in settings.allowed_user_ids:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": uid,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
            )

    # Wait for undo window, then auto-send unless event was cancelled
    await asyncio.sleep(settings.undo_window_seconds)
    await _auto_send_if_not_cancelled(event.id, calling_list)


async def _auto_send_if_not_cancelled(event_id: int, calling_list: str) -> None:
    from app.services import wa_sender, audit
    from app.services.telegram_notify import send_admin_message

    event_title = None
    async with session_scope() as s:
        db_event = await s.get(CricketEvent, event_id)
        if not db_event:
            return

        event_title = db_event.title

        if db_event.status == EventStatus.cancelled:
            log.info("event_scheduler.send_cancelled", event_id=event_id)
            return

        await audit.record(
            s,
            actor="event_scheduler",
            action="calling_list_auto_send",
            target=str(event_id),
        )

    send_failed = False
    if db_event.group_wa_id:
        try:
            await wa_sender.send_text(db_event.group_wa_id, calling_list)
            log.info("event_scheduler.wa_sent", event_id=event_id, group=db_event.group_wa_id)
        except Exception as exc:
            send_failed = True
            log.error("event_scheduler.wa_send_failed", error=str(exc))
            await send_admin_message(
                f"⚠️ Failed to send calling list to WA group.\n"
                f"Error: <code>{str(exc)[:300]}</code>\n\n"
                f"Copy & paste manually:\n<pre>{calling_list[:2000]}</pre>"
            )
    else:
        send_failed = True
        # No group WA ID configured — just send to Telegram for manual copy
        await send_admin_message(
            f"📋 <b>Calling list ready</b> (no WA group ID set — copy below):\n\n"
            f"<pre>{calling_list[:3000]}</pre>"
        )

    async with session_scope() as s:
        db_event = await s.get(CricketEvent, event_id)
        if db_event:
            db_event.calling_list_text = calling_list
            if not send_failed:
                db_event.status = EventStatus.closed

    if not send_failed:
        await send_admin_message(
            f"✅ Calling list sent to WhatsApp group for <b>{event_title or 'active event'}</b>"
        )
