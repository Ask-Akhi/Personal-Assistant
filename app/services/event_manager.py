"""Cricket/Sports Event Manager — Phase 2/3/5.

Responsibilities:
- Auto-detect event announcements from group messages (no manual command needed)
- RSVP classification (English + Hinglish + emoji)
- WNBO (Will Not Bowl) tracking
- Calling list builder
- Event creation helpers
- Cutoff timer trigger (called from scheduler)

## Zero-effort flow:
  You post in WA group: "T20 this Saturday 6:30am, Oval Ground - who's in?"
  PI detects it -> creates CricketEvent automatically -> starts RSVP collection
  After 36hrs -> builds calling list -> sends to Telegram for 30s undo -> auto-posts to group
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.logging_setup import log
from app.models import (
    Contact, CricketEvent, EventRsvp, EventStatus, InboundMessage, RsvpStatus
)
from app.services.time_utils import as_utc_aware
from app.services.wa_groups import fetch_group_participant_ids

import pytz

AET = pytz.timezone("Australia/Sydney")

_EVENT_RESPONSE_TO_RSVP: dict[object, RsvpStatus] = {
    0: RsvpStatus.maybe,  # WhatsApp's UNKNOWN can still map to a soft maybe
    1: RsvpStatus.yes,
    2: RsvpStatus.no,
    3: RsvpStatus.maybe,
    "going": RsvpStatus.yes,
    "yes": RsvpStatus.yes,
    "yes_go": RsvpStatus.yes,
    "maybe": RsvpStatus.maybe,
    "not_going": RsvpStatus.no,
    "not going": RsvpStatus.no,
    "no": RsvpStatus.no,
    "GOING": RsvpStatus.yes,
    "NOT_GOING": RsvpStatus.no,
    "MAYBE": RsvpStatus.maybe,
    "EVENT_RESPONSE_TYPE_GOING": RsvpStatus.yes,
    "EVENT_RESPONSE_TYPE_NOT_GOING": RsvpStatus.no,
    "EVENT_RESPONSE_TYPE_MAYBE": RsvpStatus.maybe,
    "event_response_type_going": RsvpStatus.yes,
    "event_response_type_not_going": RsvpStatus.no,
    "event_response_type_maybe": RsvpStatus.maybe,
}

# ── RSVP keyword classifier ──────────────────────────────────────────

_YES_PATTERNS = [
    r"\byes\b", r"\byep\b", r"\byup\b", r"\bya\b", r"\byaa\b",
    r"\bi'?m in\b", r"\bim in\b", r"\bin\b",
    r"\bcoming\b", r"\bcome\b", r"\baa raha\b", r"\baa rha\b",
    r"\bpakka\b", r"\bpucca\b", r"\bconfirm\b", r"\bconfirmed\b",
    r"\bready\b", r"\bcount me in\b", r"\bwill come\b", r"\bwill be there\b",
    r"\bhal\b",   # short for "haan le" / hal
    r"^👍", r"^✅", r"^🏏",
]

_NO_PATTERNS = [
    r"\bno\b", r"\bnope\b", r"\bnahi\b", r"\bnahin\b", r"\bnai\b",
    r"\bnot coming\b", r"\bcan'?t\b", r"\bcannot\b", r"\bwon'?t\b",
    r"\bskipping\b", r"\bskip\b", r"\bpass\b",
    r"\bnot available\b", r"\bunavailable\b",
    r"\bnahi aaunga\b", r"\bnahi aa\b", r"\bnot in\b", r"\bout\b",
    r"^❌", r"^🚫",
]

_WNBO_PATTERNS = [
    r"\bwnbo\b",
    r"\bwill not bowl\b", r"\bnot bowl\b", r"\bno bowl\b",
    r"\bonly bat\b", r"\bjust bat\b", r"\bbatting only\b",
    r"\bbowling nahi\b", r"\bbowl nahi\b",
]

_MAYBE_PATTERNS = [
    r"\bmaybe\b", r"\bperhaps\b", r"\bshayad\b", r"\btrying\b",
    r"\bwill try\b", r"\btry karta\b", r"\bdekhta hu\b", r"\bdekhte hai\b",
    r"\bnot sure\b", r"\bunsure\b",
]


def classify_rsvp(text: str) -> tuple[RsvpStatus | None, bool]:
    """
    Returns (RsvpStatus | None, is_wnbo).
    None means not an RSVP message at all.
    """
    lower = text.lower().strip()

    # WNBO check first (can co-exist with YES)
    is_wnbo = any(re.search(p, lower) for p in _WNBO_PATTERNS)

    # Check explicit NO
    if any(re.search(p, lower) for p in _NO_PATTERNS):
        return RsvpStatus.no, is_wnbo

    # Check MAYBE
    if any(re.search(p, lower) for p in _MAYBE_PATTERNS):
        return RsvpStatus.maybe, is_wnbo

    # Check YES
    if any(re.search(p, lower) for p in _YES_PATTERNS):
        status = RsvpStatus.wnbo if is_wnbo else RsvpStatus.yes
        return status, is_wnbo

    # Not an RSVP
    return None, is_wnbo


# ── DB helpers ───────────────────────────────────────────────────────

async def get_active_event(session: AsyncSession) -> CricketEvent | None:
    """Return the most recently created open event, if any."""
    result = await session.execute(
        select(CricketEvent)
        .where(CricketEvent.status == EventStatus.open)
        .order_by(CricketEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_event_for_group(session: AsyncSession, group_wa_id: str) -> CricketEvent | None:
    """Return the most recently created open event for a specific WA group."""
    if not group_wa_id:
        return None
    result = await session.execute(
        select(CricketEvent)
        .where(
            CricketEvent.status == EventStatus.open,
            CricketEvent.group_wa_id == group_wa_id,
        )
        .order_by(CricketEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_event_for_group(session: AsyncSession, group_wa_id: str) -> CricketEvent | None:
    """Return the latest event row for a specific WA group, regardless of status."""
    if not group_wa_id:
        return None
    result = await session.execute(
        select(CricketEvent)
        .where(CricketEvent.group_wa_id == group_wa_id)
        .order_by(CricketEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_event(session: AsyncSession) -> CricketEvent | None:
    """Return the most recently created cricket event, regardless of status."""
    result = await session.execute(
        select(CricketEvent)
        .order_by(CricketEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_weekly_event(session: AsyncSession) -> CricketEvent | None:
    """Return the latest CHCC weekly cricket event, regardless of status."""
    result = await session.execute(
        select(CricketEvent)
        .where(CricketEvent.title == "CHCC Members - T20 Cricket")
        .order_by(CricketEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_event_near_datetime(
    session: AsyncSession,
    event_at: datetime,
    hours_window: int = 12,
) -> CricketEvent | None:
    """Return the most recent event row close to a target UTC datetime."""
    lo = event_at - timedelta(hours=hours_window)
    hi = event_at + timedelta(hours=hours_window)
    result = await session.execute(
        select(CricketEvent)
        .where(
            CricketEvent.event_at >= lo,
            CricketEvent.event_at <= hi,
        )
        .order_by(CricketEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def resolve_event_for_control_plane(
    session: AsyncSession,
    preferred_event_at: datetime | None = None,
) -> CricketEvent | None:
    """Best-effort event lookup for Telegram commands without creating new rows."""
    event = await get_active_event(session)
    if event:
        return event

    if preferred_event_at is not None:
        event = await get_event_near_datetime(session, preferred_event_at)
        if event:
            return event

    event = await get_latest_weekly_event(session)
    if event:
        return event

    return await get_latest_event(session)


async def upsert_rsvp(
    session: AsyncSession,
    event: CricketEvent,
    contact: Contact,
    inbound: InboundMessage,
    status: RsvpStatus,
    raw_text: str,
    note: str | None = None,
) -> EventRsvp:
    """Insert or update an RSVP row."""
    existing = (
        await session.execute(
            select(EventRsvp).where(
                EventRsvp.event_id == event.id,
                EventRsvp.contact_id == contact.id,
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.status = status
        existing.raw_text = raw_text
        existing.inbound_id = inbound.id
        if note:
            existing.note = note
        await session.flush()
        return existing

    rsvp = EventRsvp(
        event_id=event.id,
        contact_id=contact.id,
        inbound_id=inbound.id,
        status=status,
        raw_text=raw_text,
        note=note,
    )
    session.add(rsvp)
    await session.flush()
    return rsvp


async def get_rsvps(
    session: AsyncSession,
    event_id: int,
) -> list[tuple[EventRsvp, Contact]]:
    """Return all RSVPs with their contact for a given event."""
    rows = await session.execute(
        select(EventRsvp, Contact)
        .join(Contact, Contact.id == EventRsvp.contact_id)
        .where(EventRsvp.event_id == event_id)
        .order_by(EventRsvp.created_at)
    )
    return [
        (rsvp, contact)
        for rsvp, contact in rows.all()
        if _is_real_rsvp_contact(contact)
    ]


async def get_group_filtered_rsvps(
    session: AsyncSession,
    event: CricketEvent,
) -> list[tuple[EventRsvp, Contact]]:
    rows = await get_rsvps(session, event.id)
    if not event.group_wa_id:
        return rows

    try:
        participant_ids = await fetch_group_participant_ids(event.group_wa_id)
    except Exception as exc:
        log.warning(
            "event_manager.group_participant_lookup_failed",
            event_id=event.id,
            group_id=event.group_wa_id,
            error=str(exc),
        )
        return rows

    if not participant_ids:
        return rows

    return [
        (rsvp, contact)
        for rsvp, contact in rows
        if (contact.external_id or "").strip() in participant_ids
    ]


def _is_real_rsvp_contact(contact: Contact) -> bool:
    external_id = (contact.external_id or "").strip()
    if not external_id:
        return False
    if external_id.endswith("@g.us"):
        return False
    if external_id.endswith("@broadcast"):
        return False
    return True


# ── Calling list builder ─────────────────────────────────────────────

async def build_calling_list(
    session: AsyncSession,
    event: CricketEvent,
) -> str:
    """Build the formatted Calling List message (English)."""
    rows = await get_group_filtered_rsvps(session, event)

    going: list[str] = []
    not_coming: list[str] = []
    maybe_list: list[str] = []

    for rsvp, contact in rows:
        name = contact.display_name or contact.external_id
        if rsvp.status in (RsvpStatus.yes, RsvpStatus.wnbo):
            tag = " (wnbo)" if rsvp.status == RsvpStatus.wnbo else ""
            going.append(f"{name}{tag}")
        elif rsvp.status == RsvpStatus.no:
            not_coming.append(name)
        elif rsvp.status == RsvpStatus.maybe:
            maybe_list.append(name)

    event_dt = (
        as_utc_aware(event.event_at).astimezone(AET).strftime("%a, %b %-d at %-I:%M %p AET")
        if event.event_at
        else ""
    )
    venue_line = f"\n📍 {event.venue}" if event.venue else ""

    lines = [f"🏏 *{event.title} – Calling List*",
             f"📅 {event_dt}{venue_line}", ""]

    if going:
        lines.append(f"✅ *Going ({len(going)}):*")
        for i, name in enumerate(going, 1):
            lines.append(f"  {i}. {name}")
        lines.append("")

    if maybe_list:
        lines.append(f"🤔 *Maybe ({len(maybe_list)}):*")
        for name in maybe_list:
            lines.append(f"  • {name}")
        lines.append("")

    if not_coming:
        lines.append(f"❌ *Not coming ({len(not_coming)}):*")
        for name in not_coming:
            lines.append(f"  • {name}")

    total = len(going) + len(maybe_list)
    lines.append(f"\n👥 Total confirmed: {len(going)}  |  Maybe: {len(maybe_list)}")

    return "\n".join(lines)


# ── Entry point: called from whatsapp_ingest pipeline ───────────────

async def handle_possible_rsvp(
    session: AsyncSession,
    contact: Contact,
    inbound: InboundMessage,
) -> bool:
    """
    Check if this message is an RSVP for the active event.
    Returns True if handled as RSVP, False otherwise.
    """
    group_wa_id = _extract_group_wa_id(inbound)
    event = await get_active_event_for_group(session, group_wa_id) if group_wa_id else None
    if not event:
        event = await get_latest_event_for_group(session, group_wa_id) if group_wa_id else None
    if not event:
        event = await get_active_event(session)
    if not event:
        return False
    if not _is_real_rsvp_contact(contact):
        log.info(
            "event_manager.rsvp_ignored_non_person",
            event_id=event.id,
            external_id=contact.external_id,
            message_type=inbound.message_type,
        )
        return False
    if event.group_wa_id:
        try:
            participant_ids = await fetch_group_participant_ids(event.group_wa_id)
        except Exception as exc:
            log.warning(
                "event_manager.group_participant_lookup_failed",
                event_id=event.id,
                group_id=event.group_wa_id,
                error=str(exc),
            )
            participant_ids = set()
        if participant_ids and (contact.external_id or "").strip() not in participant_ids:
            log.info(
                "event_manager.rsvp_ignored_non_member",
                event_id=event.id,
                group_id=event.group_wa_id,
                external_id=contact.external_id,
                message_type=inbound.message_type,
            )
            return False

    status, is_wnbo = _classify_inbound_rsvp(inbound)
    if status is None:
        return False

    # Build note from WNBO detection
    note = "Will Not Bowl" if is_wnbo and status != RsvpStatus.wnbo else None

    rsvp = await upsert_rsvp(
        session,
        event=event,
        contact=contact,
        inbound=inbound,
        status=status,
        raw_text=inbound.text,
        note=note,
    )

    name = contact.display_name or contact.external_id
    log.info(
        "event_manager.rsvp_recorded",
        event_id=event.id,
        contact=name,
        status=status.value,
        wnbo=is_wnbo,
    )

    # Notify via Telegram
    await _notify_rsvp(event, contact, rsvp, is_wnbo)
    return True


def _extract_group_wa_id(inbound: InboundMessage) -> str | None:
    raw = inbound.raw or {}
    for key in ("group_id", "remote_jid", "remoteJid"):
        value = raw.get(key)
        if isinstance(value, str) and value.endswith("@g.us"):
            return value
    context = raw.get("context") or {}
    value = context.get("group_id") or context.get("remote_jid") or context.get("remoteJid")
    if isinstance(value, str) and value.endswith("@g.us"):
        return value
    return None


def _classify_inbound_rsvp(inbound: InboundMessage) -> tuple[RsvpStatus | None, bool]:
    raw = inbound.raw or {}
    if inbound.message_type in {"event_response", "poll_vote"}:
        response = raw.get("event_response")
        if isinstance(response, dict):
            response = response.get("response")
        if response is None:
            response = raw.get("response")
        if response is None and inbound.text:
            response = inbound.text
        status = _EVENT_RESPONSE_TO_RSVP.get(response)
        if status is None and response is not None:
            status = _EVENT_RESPONSE_TO_RSVP.get(str(response).strip().lower())
        if status is None:
            return None, False
        is_wnbo = status == RsvpStatus.wnbo
        return status, is_wnbo

    if not inbound.text:
        return None, False

    return classify_rsvp(inbound.text)


async def _notify_rsvp(
    event: CricketEvent,
    contact: Contact,
    rsvp: EventRsvp,
    is_wnbo: bool,
) -> None:
    from app.services.telegram_notify import send_admin_message
    from app.db import session_scope

    name = contact.display_name or contact.external_id
    icon = {"yes": "✅", "no": "❌", "wnbo": "🏏", "maybe": "🤔"}.get(rsvp.status.value, "❓")
    wnbo_tag = "  _(Will Not Bowl)_" if is_wnbo else ""

    # Count current going
    async with session_scope() as s:
        rows = await get_rsvps(s, event.id)
    going_count = sum(1 for r, _ in rows if r.status in (RsvpStatus.yes, RsvpStatus.wnbo))
    await send_admin_message(
        f"{icon} <b>{name}</b> → <code>{rsvp.status.value.upper()}</code>{wnbo_tag}\n"
        f"Event: <b>{event.title}</b>\n"
        f"Going so far: <b>{going_count}</b>"
    )


# ── Auto-detect event announcements from group messages ──────────────
#
# Triggers on messages YOU send to the group that look like:
#   "T20 this Saturday 6:30am at Oval - who's in?"
#   "Cricket Sunday 7am Shenley, reply if coming"
#   "Football tmrw 6pm - who's playing?"
#
# No manual command needed. PI watches your outgoing group messages.

_EVENT_SPORT_KEYWORDS = [
    "cricket", "t20", "football", "soccer", "futsal",
    "badminton", "tennis", "basketball", "volleyball",
]

_EVENT_TRIGGER_PHRASES = [
    r"who.?s in", r"who.?s (coming|playing|joining|there)",
    r"reply if", r"let me know", r"confirm",
    r"are you (in|coming|playing)",
    r"game (on|this|tmrw|tomorrow|sunday|saturday|friday)",
    r"match (this|tmrw|tomorrow|on)",
    r"anyone (in|coming|up for|interested)",
]

_TIME_PATTERNS = [
    # "6:30am", "6:30 am", "6am", "6 am", "18:30"
    r"\b(\d{1,2}:\d{2}\s*(?:am|pm))\b",
    r"\b(\d{1,2}\s*(?:am|pm))\b",
    r"\b(\d{2}:\d{2})\b",
]

_DAY_PATTERNS = [
    r"\b(today|tmrw|tomorrow)\b",
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\b(this\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
]

_VENUE_PATTERNS = [
    r"\bat\s+([\w\s]{3,30}?)(?:\s*[-,]|\s+who|\s+reply|\s*$)",
    r"\b(?:at|@)\s+([\w\s]{3,25}ground|[\w\s]{3,25}park|[\w\s]{3,25}court|[\w\s]{3,25}field|[\w\s]{3,25}club)\b",
]


def _detect_event_announcement(text: str) -> dict | None:
    """
    Returns a dict with detected fields if this looks like an event announcement,
    or None if it doesn't.
    """
    lower = text.lower()

    # Must mention a sport
    sport = next((s for s in _EVENT_SPORT_KEYWORDS if s in lower), None)
    if not sport:
        return None

    # Must have a trigger phrase (who's in, reply if coming, etc.)
    has_trigger = any(re.search(p, lower) for p in _EVENT_TRIGGER_PHRASES)
    if not has_trigger:
        return None

    # Try to extract time
    time_str = None
    for pat in _TIME_PATTERNS:
        m = re.search(pat, lower)
        if m:
            time_str = m.group(1).strip()
            break

    # Try to extract day
    day_str = None
    for pat in _DAY_PATTERNS:
        m = re.search(pat, lower)
        if m:
            day_str = m.group(1).strip()
            break

    # Try to extract venue
    venue = None
    for pat in _VENUE_PATTERNS:
        m = re.search(pat, lower)
        if m:
            venue = m.group(1).strip().title()
            break

    return {
        "sport": sport.upper(),
        "time_str": time_str,
        "day_str": day_str,
        "venue": venue,
    }


def _resolve_event_datetime(day_str: str | None, time_str: str | None) -> datetime | None:
    """Best-effort parse of day+time into a UTC datetime."""
    import re as _re
    from datetime import date
    import pytz

    now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
    today = now_ist.date()

    # Resolve day
    target_date = today
    if day_str:
        day_str = day_str.lower().strip()
        if day_str in ("today",):
            target_date = today
        elif day_str in ("tmrw", "tomorrow"):
            target_date = today + timedelta(days=1)
        else:
            # Named weekday
            weekdays = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
            for i, wd in enumerate(weekdays):
                if wd in day_str:
                    days_ahead = (i - today.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7  # next occurrence
                    target_date = today + timedelta(days=days_ahead)
                    break

    # Resolve time
    hour, minute = 6, 30  # sensible default for morning cricket
    if time_str:
        time_str = time_str.lower().replace(" ", "")
        m = _re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", time_str)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            if m.group(3) == "pm" and hour < 12:
                hour += 12
            elif m.group(3) == "am" and hour == 12:
                hour = 0
        else:
            m = _re.match(r"(\d{1,2})\s*(am|pm)", time_str)
            if m:
                hour = int(m.group(1))
                if m.group(2) == "pm" and hour < 12:
                    hour += 12
                elif m.group(2) == "am" and hour == 12:
                    hour = 0

    ist = pytz.timezone("Asia/Kolkata")
    naive_dt = datetime(target_date.year, target_date.month, target_date.day, hour, minute)
    return ist.localize(naive_dt).astimezone(pytz.utc).replace(tzinfo=timezone.utc)


async def auto_detect_and_create_event(
    session: AsyncSession,
    contact: Contact,
    inbound: InboundMessage,
) -> CricketEvent | None:
    """
    Called on every inbound message.
    Detects event announcements and auto-creates a CricketEvent.
    Group WA ID is extracted from the raw webhook payload.
    Returns the created event or None.
    """
    if not inbound.text:
        return None

    # Don't create if there's already an open event
    existing = await get_active_event(session)
    if existing:
        return None

    detected = _detect_event_announcement(inbound.text)
    if not detected:
        return None

    event_dt = _resolve_event_datetime(detected["day_str"], detected["time_str"])
    if not event_dt:
        return None

    # Extract group WA ID from raw webhook payload (group messages have a "from" at group level)
    raw = inbound.raw or {}
    group_wa_id = (
        raw.get("group_id")
        or (raw.get("context") or {}).get("group_id")
        or inbound.from_external_id  # fallback: treat sender as target for direct groups
    )

    # Cutoff uses the configured default lead time before the event.
    cutoff_hours = get_settings().event_default_cutoff_hours
    cutoff_at = event_dt - timedelta(hours=cutoff_hours)
    # If cutoff already passed, use 2 hours from now
    now = datetime.now(timezone.utc)
    if cutoff_at < now:
        cutoff_at = now + timedelta(hours=2)

    title = f"{detected['sport']} {detected['day_str'] or ''}".strip()

    event = CricketEvent(
        title=title,
        event_at=event_dt.replace(tzinfo=None),  # store naive UTC
        venue=detected["venue"],
        group_wa_id=group_wa_id,
        cutoff_hours=cutoff_hours,
        cutoff_at=cutoff_at.replace(tzinfo=None),
    )
    session.add(event)
    await session.flush()

    log.info(
        "event_manager.auto_created",
        event_id=event.id,
        title=title,
        event_at=str(event_dt),
        cutoff_at=str(cutoff_at),
        venue=detected["venue"],
    )

    # Notify you on Telegram
    from app.services.telegram_notify import send_admin_message
    venue_line = f"\nVenue: {detected['venue']}" if detected["venue"] else ""
    await send_admin_message(
        f"🏏 <b>Event auto-detected!</b>\n"
        f"Title: <b>{title}</b>\n"
        f"Date/Time: <b>{event_dt.strftime('%a %b %-d at %-I:%M %p')} IST</b>"
        f"{venue_line}\n"
        f"Cutoff: <b>{cutoff_at.strftime('%a %b %-d %-I:%M %p')} IST</b>\n\n"
        f"Watching for RSVPs in the group automatically.\n"
        f"Use /votes to check anytime."
    )

    return event
