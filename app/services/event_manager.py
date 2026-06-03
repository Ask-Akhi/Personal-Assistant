"""Cricket/Sports Event Manager — Phase 2/3/5.

Responsibilities:
- RSVP classification (English + Hinglish + emoji)
- WNBO (Will Not Bowl) tracking
- Calling list builder
- Event creation helpers
- Cutoff timer trigger (called from scheduler)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import log
from app.models import (
    Contact, CricketEvent, EventRsvp, EventStatus, InboundMessage, RsvpStatus
)

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
    return list(rows.all())


# ── Calling list builder ─────────────────────────────────────────────

async def build_calling_list(
    session: AsyncSession,
    event: CricketEvent,
) -> str:
    """Build the formatted Calling List message (English)."""
    rows = await get_rsvps(session, event.id)

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

    event_dt = event.event_at.strftime("%a, %b %-d at %-I:%M %p") if event.event_at else ""
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
    if not inbound.text:
        return False

    event = await get_active_event(session)
    if not event:
        return False

    status, is_wnbo = classify_rsvp(inbound.text)
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
