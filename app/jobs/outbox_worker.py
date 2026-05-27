"""Outbox worker — drains approved drafts and sends them via WhatsApp Cloud API.

Run as a long-lived Render background worker (separate from the bot and API).
Poll interval: every 5 seconds.  On send success, notifies Telegram with a
"✅ Sent" confirmation so you always know what went out.

Safety guarantees:
- Respects the 30-second undo window (send_after).
- Skips items that are no longer pending (cancelled between approval and drain).
- Max 3 attempts per item before marking failed and alerting Telegram.
- Only handles channel="wa_cloud" for now (Baileys in Phase 7).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.db import engine, session_scope
from app.logging_setup import log, setup_logging
from app.models import Base, Outbox, OutboxStatus
from app.services import audit, outbox as outbox_svc, wa_sender
from app.services.telegram_notify import send_admin_message

setup_logging()

POLL_INTERVAL = 5       # seconds between drain cycles
MAX_ATTEMPTS  = 3       # give up after this many failures


async def _drain_once() -> int:
    """Process one batch of due outbox items.  Returns count processed."""
    async with session_scope() as s:
        items: list[Outbox] = await outbox_svc.due_messages(s)

    processed = 0
    for item in items:
        await _send_one(item.id)
        processed += 1

    return processed


async def _send_one(outbox_id: int) -> None:
    async with session_scope() as s:
        item = await s.get(Outbox, outbox_id)
        if not item or item.status != OutboxStatus.pending:
            return                          # cancelled between listing and now

        if item.attempts >= MAX_ATTEMPTS:
            await outbox_svc.mark_failed(s, outbox_id, error="max_attempts_exceeded")
            await audit.record(
                s, actor="outbox_worker", action="outbox_give_up",
                target=str(outbox_id),
                reasons={"attempts": item.attempts},
            )
            await send_admin_message(
                f"⚠️ <b>Outbox item #{outbox_id} permanently failed</b>\n"
                f"To: <code>{item.to}</code>\n"
                f"Body: {item.body[:200]}\n"
                f"Last error: {item.last_error or 'unknown'}"
            )
            return

        if item.channel != "wa_cloud":
            # Baileys and other channels handled in Phase 7
            log.debug("outbox_worker.skip_channel", channel=item.channel, id=outbox_id)
            return

        try:
            wa_message_id = await wa_sender.send_text(item.to, item.body)
            await outbox_svc.mark_sent(s, outbox_id)
            await audit.record(
                s, actor="outbox_worker", action="outbox_sent",
                target=str(outbox_id),
                payload={"wa_message_id": wa_message_id, "to": item.to},
            )
            log.info("outbox_worker.sent", outbox_id=outbox_id, to=item.to)

        except Exception as exc:
            error_str = str(exc)[:500]
            await outbox_svc.mark_failed(s, outbox_id, error=error_str)
            await audit.record(
                s, actor="outbox_worker", action="outbox_send_failed",
                target=str(outbox_id),
                reasons={"error": error_str, "attempt": item.attempts + 1},
            )
            log.warning("outbox_worker.send_failed", outbox_id=outbox_id, error=error_str)
            await send_admin_message(
                f"⚠️ <b>Send failed</b> (attempt {item.attempts + 1}/{MAX_ATTEMPTS})\n"
                f"Outbox #{outbox_id} → <code>{item.to}</code>\n"
                f"Error: <code>{error_str[:300]}</code>"
            )
            return

    # Notify Telegram that the message was actually sent (outside the session)
    async with session_scope() as s:
        item = await s.get(Outbox, outbox_id)
        if item and item.status == OutboxStatus.sent:
            await send_admin_message(
                f"✅ <b>Sent</b> → <code>{item.to}</code>\n"
                f"{item.body[:300]}"
            )


async def run() -> None:
    """Main loop. Runs forever until interrupted."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    log.info("outbox_worker.started", poll_interval=POLL_INTERVAL)

    while True:
        try:
            n = await _drain_once()
            if n:
                log.info("outbox_worker.drained", count=n)
        except Exception as exc:
            log.error("outbox_worker.loop_error", error=str(exc))
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())
