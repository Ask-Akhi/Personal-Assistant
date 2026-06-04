"""FastAPI gateway — all workers run as background tasks inside this process.

Single-process deployment: API + Telegram bot + outbox + reminders + daily brief
all co-exist here so we only need ONE free Render web service ($0/month on free tier).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import html
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.error import TelegramError

from app.config import get_settings
from app.db import engine, session_scope
from app.logging_setup import log, setup_logging
from app.models import Base
from app.services import audit, idempotency, telegram_notify, whatsapp_ingest
from app.services import draft_manager, event_manager

setup_logging()
settings = get_settings()

_background_tasks: list[asyncio.Task] = []


async def _run_bot():
    """Run Telegram bot in background (polling mode)."""
    from app.bot import build_app

    bot_app = build_app()

    try:
        await bot_app.initialize()
        await bot_app.start()

        # Clear any stale webhook configuration before polling.
        try:
            await bot_app.bot.delete_webhook(drop_pending_updates=True)
            log.info("bot.webhook_cleared")
        except TelegramError as exc:
            log.warning("bot.webhook_clear_failed", error=str(exc))

        try:
            me = await bot_app.bot.get_me()
            log.info("bot.identity", username=me.username, bot_id=me.id)
        except TelegramError as exc:
            log.warning("bot.identity_failed", error=str(exc))

        def _polling_error_callback(exc: TelegramError) -> None:
            log.warning("bot.polling_error", error=str(exc))

        await bot_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            poll_interval=1.0,
            bootstrap_retries=-1,
            error_callback=_polling_error_callback,
        )
        log.info("bot.started")

        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        try:
            await bot_app.updater.stop()
        except Exception:
            pass
        try:
            await bot_app.stop()
        except Exception:
            pass
        try:
            await bot_app.shutdown()
        except Exception:
            pass
        raise
    except Exception as exc:
        log.error("bot.crashed", error=str(exc), exc_info=True)


async def _run_outbox_worker():
    from app.jobs.outbox_worker import run
    await run()


async def _run_reminder_worker():
    from app.jobs.reminder_worker import run
    await run()


async def _run_event_scheduler():
    """Check every 5 minutes for events that have passed their cutoff."""
    from app.jobs.event_scheduler import run_once
    while True:
        try:
            await run_once()
        except Exception as exc:
            log.error("event_scheduler.error", error=str(exc))
        await asyncio.sleep(300)  # 5 minutes


async def _run_weekly_event_poster():
    """Post T20 Cricket event every Tuesday 7 PM AET for Saturday."""
    from app.jobs.weekly_event_poster import run
    await run()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Auto-create tables on first run
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("api.startup", env=settings.app_env)

    # Start all background workers as asyncio tasks
    _background_tasks.append(asyncio.create_task(_run_bot(), name="bot"))
    _background_tasks.append(asyncio.create_task(_run_outbox_worker(), name="outbox"))
    _background_tasks.append(asyncio.create_task(_run_reminder_worker(), name="reminders"))
    _background_tasks.append(asyncio.create_task(_run_event_scheduler(), name="event_scheduler"))
    _background_tasks.append(asyncio.create_task(_run_weekly_event_poster(), name="weekly_poster"))
    log.info("workers.started", count=len(_background_tasks))

    yield

    # Graceful shutdown
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    log.info("api.shutdown")


app = FastAPI(title="Personal AI Assistant - Phase 2 Drafting", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "env": settings.app_env}


@app.get("/readyz")
async def readyz():
    checks = {
        "telegram_bot_token": bool(settings.telegram_bot_token),
        "telegram_allowed_user_ids": bool(settings.allowed_user_ids),
        "whatsapp_verify_token": bool(settings.whatsapp_verify_token),
        "whatsapp_app_secret": bool(settings.whatsapp_app_secret),
        "whatsapp_cloud_token": bool(settings.whatsapp_cloud_token),
        "whatsapp_phone_number_id": bool(settings.whatsapp_phone_number_id),
        "anthropic_api_key": bool(settings.anthropic_api_key),
    }
    required_now = (
        "telegram_bot_token",
        "telegram_allowed_user_ids",
        "whatsapp_verify_token",
    )
    missing_required = [key for key in required_now if not checks[key]]
    missing_recommended = [
        key
        for key in (
            "whatsapp_app_secret",
            "whatsapp_cloud_token",
            "whatsapp_phone_number_id",
        )
        if not checks[key]
    ]
    return {
        "ok": not missing_required,
        "env": settings.app_env,
        "checks": checks,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
    }


# ── Stubs for later phases ───────────────────────────────────────────
@app.get("/webhooks/whatsapp")
async def wa_verify(request: Request):
    """Meta verification handshake. Activated in Phase 1."""
    params = dict(request.query_params)
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.whatsapp_verify_token
    ):
        return JSONResponse(int(params.get("hub.challenge", "0")))
    return JSONResponse({"error": "forbidden"}, status_code=403)


@app.post("/webhooks/whatsapp")
async def wa_inbound(request: Request):
    body = await request.body()
    if not whatsapp_ingest.signature_valid(
        body=body,
        signature_header=request.headers.get("x-hub-signature-256"),
        app_secret=settings.whatsapp_app_secret,
    ):
        return JSONResponse({"error": "bad_signature"}, status_code=403)

    payload = json.loads(body or b"{}")
    messages = whatsapp_ingest.extract_messages(payload)

    if not messages:
        event_id = _extract_event_id(payload)
        async with session_scope() as s:
            is_new = await idempotency.claim_event(
                s,
                source="wa_cloud",
                external_id=event_id,
            )
            await audit.record(
                s,
                actor="webhook:wa_cloud",
                action="webhook_ignored" if is_new else "webhook_duplicate",
                target=event_id,
            )
        return {"ok": True, "messages": 0, "duplicate": not is_new}

    processed = 0
    duplicates = 0
    for message in messages:
        async with session_scope() as s:
            is_new = await idempotency.claim_event(
                s,
                source="wa_cloud",
                external_id=message.external_id,
            )
            if not is_new:
                duplicates += 1
                await audit.record(
                    s,
                    actor="webhook:wa_cloud",
                    action="inbound_duplicate",
                    target=message.external_id,
                )
                continue

            contact, inbound = await whatsapp_ingest.persist_message(s, message)
            processed += 1
            await audit.record(
                s,
                actor="webhook:wa_cloud",
                action="inbound_mirrored",
                target=message.external_id,
                payload={
                    "contact_id": contact.id,
                    "from": message.from_external_id,
                    "type": message.message_type,
                },
            )

        if inbound is not None:
            await telegram_notify.send_admin_message(_mirror_text(contact, inbound))

            # Phase 3: auto-detect event announcement (runs on every message)
            async with session_scope() as s:
                await event_manager.auto_detect_and_create_event(s, contact, inbound)

            # Phase 2/3: check if this is an RSVP for the active event
            async with session_scope() as s:
                is_rsvp = await event_manager.handle_possible_rsvp(s, contact, inbound)

            if not is_rsvp:
                # Normal message -- policy check + AI draft + Telegram approval card
                await draft_manager.handle_inbound(contact, inbound)

    return {
        "ok": True,
        "messages": len(messages),
        "processed": processed,
        "duplicates": duplicates,
    }


def _mirror_text(contact, inbound) -> str:
    name = contact.display_name or contact.external_id
    body = inbound.text or f"[{inbound.message_type} message]"
    return (
        "<b>WhatsApp inbound</b>\n"
        f"From: {html.escape(name)}\n"
        f"Contact: <code>{html.escape(contact.external_id)}</code>\n"
        f"Message: {html.escape(body[:1500])}"
    )


def _extract_event_id(payload: dict) -> str:
    messages = whatsapp_ingest.extract_messages(payload)
    if messages:
        return messages[0].external_id
    entries = payload.get("entry") or []
    for entry in entries:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for status in value.get("statuses") or []:
                sid = status.get("id")
                if sid:
                    return f"status:{sid}"
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"payload:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
