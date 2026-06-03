"""Daily brief job -- Phase 2.
Enqueued by Render Cron at 02:30 UTC (08:00 IST).
Cron jobs cannot access persistent disks -- they only read/write Postgres.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select
from app.db import engine, session_scope
from app.logging_setup import log, setup_logging
from app.models import AuditLog, Base, Commitment, CommitmentStatus, Draft, DraftStatus, InboundMessage, Memory, Outbox, OutboxStatus, Reminder, ReminderStatus
from app.services.time_utils import utc_now_naive
from app.services.telegram_notify import send_admin_message

setup_logging()


async def build_brief() -> str:
    since = utc_now_naive() - timedelta(hours=24)
    async with session_scope() as s:
        audit_count = (
            await s.execute(select(func.count(AuditLog.id)).where(AuditLog.ts >= since))
        ).scalar_one()
        inbound_count = (
            await s.execute(select(func.count(InboundMessage.id)).where(InboundMessage.received_at >= since))
        ).scalar_one()
        notes_total = (await s.execute(select(func.count(Memory.id)))).scalar_one()

        drafts_created = (
            await s.execute(select(func.count(Draft.id)).where(Draft.created_at >= since))
        ).scalar_one()
        drafts_pending = (
            await s.execute(select(func.count(Draft.id)).where(Draft.status == DraftStatus.pending))
        ).scalar_one()
        drafts_approved = (
            await s.execute(select(func.count(Draft.id)).where(
                Draft.status.in_([DraftStatus.approved, DraftStatus.edited]),
                Draft.created_at >= since,
            ))
        ).scalar_one()
        drafts_rejected = (
            await s.execute(select(func.count(Draft.id)).where(
                Draft.status == DraftStatus.rejected,
                Draft.created_at >= since,
            ))
        ).scalar_one()

        outbox_sent = (
            await s.execute(select(func.count(Outbox.id)).where(
                Outbox.status == OutboxStatus.sent,
                Outbox.created_at >= since,
            ))
        ).scalar_one()
        outbox_failed = (
            await s.execute(select(func.count(Outbox.id)).where(
                Outbox.status == OutboxStatus.failed,
                Outbox.created_at >= since,
            ))
        ).scalar_one()
        open_commitments = (
            await s.execute(
                select(func.count(Commitment.id)).where(Commitment.status == CommitmentStatus.open)
            )
        ).scalar_one()
        pending_reminders = (
            await s.execute(
                select(func.count(Reminder.id)).where(Reminder.status == ReminderStatus.pending)
            )
        ).scalar_one()

    date_str = datetime.now(tz=timezone.utc).strftime("%a %d %b")
    lines = [
        f"Daily Brief -- {date_str}",
        "",
        "Messages (24h)",
        f"  Inbound: {inbound_count}",
        f"  Drafts created: {drafts_created}",
        f"  Approved/Edited: {drafts_approved}",
        f"  Rejected: {drafts_rejected}",
        f"  Pending decisions: {drafts_pending}",
        "",
        "Outbox (24h)",
        f"  Sent: {outbox_sent}  Failed: {outbox_failed}",
        "",
        "Commitments & Reminders",
        f"  Open commitments: {open_commitments}",
        f"  Pending reminders: {pending_reminders}",
        "",
        "Memory",
        f"  Total entries: {notes_total}",
        "",
        f"Audit events (24h): {audit_count}",
    ]
    if drafts_pending:
        lines.append(f"\nACTION NEEDED: {drafts_pending} draft(s) awaiting your decision. Use /inbox to review.")
    return "\n".join(lines)


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    brief = await build_brief()
    log.info("daily_brief.built")
    async with session_scope() as s:
        s.add(AuditLog(actor="cron", action="daily_brief", payload={"text": brief[:1000]}))
    await send_admin_message(brief)


if __name__ == "__main__":
    asyncio.run(main())
