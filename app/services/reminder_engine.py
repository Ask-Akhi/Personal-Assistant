"""Reminder engine — persists commitments and schedules Telegram reminders.

Called after a draft is approved so we track what was committed to.
Also called directly from handle_inbound for inbound commitment signals.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Commitment, CommitmentStatus, Contact, InboundMessage, Reminder, ReminderStatus
from app.services import audit
from app.services.commitment_extractor import CommitmentSignal
from app.services.time_utils import as_utc_naive, utc_now_naive


async def persist_signals(
    session: AsyncSession,
    *,
    contact: Contact,
    inbound: InboundMessage,
    signals: list[CommitmentSignal],
) -> list[Commitment]:
    """Persist extracted commitment signals and schedule a follow-up reminder."""
    created: list[Commitment] = []
    for sig in signals:
        commitment = Commitment(
            contact_id=contact.id,
            inbound_id=inbound.id,
            kind=sig.kind,
            raw_snippet=sig.raw_snippet,
            context_text=(inbound.text or "")[:500],
        )
        session.add(commitment)
        await session.flush()
        created.append(commitment)

        # Schedule a reminder 4 hours later (Phase 3 default; Phase 4 will parse actual dates)
        reminder = Reminder(
            commitment_id=commitment.id,
            contact_id=contact.id,
            message=_reminder_text(contact, inbound, sig),
            fire_at=utc_now_naive() + timedelta(hours=4),
        )
        session.add(reminder)

        await audit.record(
            session,
            actor="reminder_engine",
            action="commitment_created",
            target=str(commitment.id),
            payload={
                "kind": sig.kind,
                "snippet": sig.raw_snippet[:100],
                "contact_id": contact.id,
            },
        )

    await session.flush()
    return created


def _reminder_text(contact: Contact, inbound: InboundMessage, sig: CommitmentSignal) -> str:
    name = contact.display_name or contact.external_id
    body = (inbound.text or "")[:200]
    return (
        f"⏰ Reminder — commitment from {html.escape(name)}\n"
        f"Signal: {sig.kind} → {html.escape(sig.raw_snippet[:80])}\n"
        f"Original: {html.escape(body)}"
    )


async def due_reminders(session: AsyncSession, *, now: datetime | None = None) -> list[Reminder]:
    """Return pending reminders whose fire_at has passed."""
    from sqlalchemy import select
    current = as_utc_naive(now) if now is not None else utc_now_naive()
    stmt = (
        select(Reminder)
        .where(Reminder.status == ReminderStatus.pending, Reminder.fire_at <= current)
        .order_by(Reminder.fire_at.asc())
        .limit(50)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_sent(session: AsyncSession, reminder_id: int) -> None:
    reminder = await session.get(Reminder, reminder_id)
    if reminder:
        reminder.status = ReminderStatus.sent
        reminder.sent_at = utc_now_naive()


async def dismiss(session: AsyncSession, reminder_id: int) -> None:
    reminder = await session.get(Reminder, reminder_id)
    if reminder:
        reminder.status = ReminderStatus.dismissed
