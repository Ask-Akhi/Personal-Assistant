"""FastAPI gateway. Webhooks land here in later phases."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import html
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import engine, session_scope
from app.logging_setup import log, setup_logging
from app.models import Base
from app.services import audit, idempotency, telegram_notify, whatsapp_ingest
from app.services import draft_manager

setup_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Auto-create tables on first run (Alembic preferred in prod).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("api.startup", env=settings.app_env)
    yield
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
            # Phase 2: policy check + Claude draft + Telegram approval card
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
