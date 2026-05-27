"""Reminder worker — polls for due reminders and fires them to Telegram.

Run as a long-lived Render background worker.
Poll interval: 60 seconds (reminders don't need sub-minute precision).
"""
from __future__ import annotations

import asyncio

from app.db import engine, session_scope
from app.logging_setup import log, setup_logging
from app.models import Base
from app.services import audit, reminder_engine
from app.services.telegram_notify import send_admin_message

setup_logging()

POLL_INTERVAL = 60  # seconds


async def _fire_once() -> int:
    async with session_scope() as s:
        reminders = await reminder_engine.due_reminders(s)

    count = 0
    for reminder in reminders:
        async with session_scope() as s:
            r = await s.get(type(reminder), reminder.id)
            if not r:
                continue
            await send_admin_message(r.message)
            await reminder_engine.mark_sent(s, r.id)
            await audit.record(
                s,
                actor="reminder_worker",
                action="reminder_sent",
                target=str(r.id),
                payload={"commitment_id": r.commitment_id},
            )
            count += 1
            log.info("reminder_worker.sent", reminder_id=r.id)

    return count


async def run() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("reminder_worker.started", poll_interval=POLL_INTERVAL)
    while True:
        try:
            n = await _fire_once()
            if n:
                log.info("reminder_worker.fired", count=n)
        except Exception as exc:
            log.error("reminder_worker.loop_error", error=str(exc))
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())
