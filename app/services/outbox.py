"""Outbox helpers for delayed send, cancel, and idempotent release."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Outbox, OutboxStatus


def _send_after() -> datetime:
    settings = get_settings()
    return datetime.now(timezone.utc) + timedelta(seconds=settings.undo_window_seconds)


async def queue_message(
    session: AsyncSession,
    *,
    channel: str,
    to: str,
    body: str,
    idempotency_key: str | None = None,
) -> Outbox:
    message = Outbox(
        channel=channel,
        to=to,
        body=body,
        idempotency_key=idempotency_key or str(uuid4()),
        send_after=_send_after(),
    )
    session.add(message)
    await session.flush()
    return message


async def cancel_message(session: AsyncSession, outbox_id: int) -> bool:
    message = await session.get(Outbox, outbox_id)
    if not message or message.status is not OutboxStatus.pending:
        return False
    message.status = OutboxStatus.cancelled
    return True


async def due_messages(session: AsyncSession, *, now: datetime | None = None) -> list[Outbox]:
    current = now or datetime.now(timezone.utc)
    stmt = (
        select(Outbox)
        .where(Outbox.status == OutboxStatus.pending, Outbox.send_after <= current)
        .order_by(Outbox.send_after.asc(), Outbox.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_sent(session: AsyncSession, outbox_id: int) -> bool:
    message = await session.get(Outbox, outbox_id)
    if not message or message.status is not OutboxStatus.pending:
        return False
    message.status = OutboxStatus.sent
    message.attempts += 1
    return True


async def mark_failed(session: AsyncSession, outbox_id: int, *, error: str) -> bool:
    """Increment attempts and store error.  Leaves status=pending so the
    worker retries on next cycle — unless the worker decides it has hit
    MAX_ATTEMPTS and calls this with error='max_attempts_exceeded', in which
    case it sets status=failed itself before calling this."""
    message = await session.get(Outbox, outbox_id)
    if not message:
        return False
    message.attempts += 1
    message.last_error = error[:1000]
    # Only hard-fail when the worker explicitly gives up
    if error == "max_attempts_exceeded":
        message.status = OutboxStatus.failed
    # else stay pending — will be retried on next poll
    return True
