"""Draft lifecycle manager.

Flow:
  1. inbound message arrives (already persisted)
  2. policy_engine.decide() → action
     - block/alert → no draft, just audit
     - draft/auto_send → generate suggestions via Claude
  3. Draft row created (status=pending)
  4. Telegram approval card sent with inline buttons
  5. Bot callback:
     - Approve[0/1/2]  → final_text = suggestion[i], status=approved → Outbox
     - Edit            → ask for text, then queue
     - Regen           → call Claude again, update suggestions, re-post card
     - Reject          → status=rejected
  6. Outbox worker (later) drains and calls wa_sender.send_text()
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import log
from app.models import Contact, Draft, DraftStatus, InboundMessage, OutboxStatus
from app.policy_engine import Action, ContactView, Message, PolicyContext, decide
from app.services import audit, drafting, outbox as outbox_svc
from app.services.quiet_hours import is_quiet_now
from app.services.whatsapp import service_window_open
from app.services import language_detector, tone_profiler, commitment_extractor, reminder_engine


# ── Public entry point called from wa_inbound webhook ───────────────

async def handle_inbound(contact: Contact, inbound: InboundMessage) -> None:
    """Run policy + Phase 3 enrichment + draft generation + Telegram card."""
    settings = get_settings()

    ctx = PolicyContext(quiet_hours=is_quiet_now())
    msg = Message(
        text=inbound.text or "",
        from_id=contact.external_id,
        channel=inbound.channel,
    )
    contact_view = ContactView(
        id=str(contact.id),
        tier=contact.tier,
        auto_send_enabled=contact.auto_send_enabled,
        successful_interactions=0,
    )

    decision = decide(msg, contact_view, ctx)

    async with session_scope() as s:
        await audit.record(
            s,
            actor="policy_engine",
            action="policy_decision",
            target=str(inbound.id),
            reasons={"action": decision.action.value, "reasons": decision.reasons},
            payload={"confidence": decision.confidence},
        )

    if decision.action in (Action.block, Action.alert):
        await _send_alert_card(contact, inbound, decision)
        return

    # ── Phase 3: language detection + tone profile + commitments ─────
    lang_result = language_detector.detect(inbound.text or "")
    signals = commitment_extractor.extract(inbound.text or "")

    async with session_scope() as s:
        contact_row = await s.get(type(contact), contact.id)
        if contact_row:
            await tone_profiler.update(s, contact_row, lang_result)
            await audit.record(
                s,
                actor="draft_manager",
                action="language_detected",
                target=str(inbound.id),
                payload={
                    "lang": lang_result.lang,
                    "script": lang_result.script,
                    "formality": lang_result.formality,
                    "confidence": round(lang_result.confidence, 2),
                },
            )
        if signals and contact_row:
            inbound_row = await s.get(type(inbound), inbound.id)
            if inbound_row:
                await reminder_engine.persist_signals(
                    s,
                    contact=contact_row,
                    inbound=inbound_row,
                    signals=signals,
                )
                await audit.record(
                    s,
                    actor="draft_manager",
                    action="commitments_extracted",
                    target=str(inbound.id),
                    payload={
                        "count": len(signals),
                        "summary": commitment_extractor.summary(signals),
                    },
                )

    # ── Check 24-hour service window ──────────────────────────────────
    can_reply = service_window_open(contact.last_inbound_at)
    if not can_reply:
        async with session_scope() as s:
            await audit.record(
                s,
                actor="draft_manager",
                action="draft_skipped_no_service_window",
                target=str(inbound.id),
            )
        await _notify_no_service_window(contact, inbound)
        return

    # ── Generate LLM drafts ───────────────────────────────────────────
    async with session_scope() as s:
        try:
            suggestions = await drafting.generate(contact, inbound, session=s)
        except Exception as exc:
            log.error("draft_manager.generate_failed", error=str(exc), inbound_id=inbound.id)
            await audit.record(
                s,
                actor="draft_manager",
                action="draft_generate_failed",
                target=str(inbound.id),
                reasons={"error": str(exc)[:500]},
            )
            await _notify_draft_error(contact, inbound, str(exc))
            return

        draft = Draft(
            inbound_id=inbound.id,
            contact_id=contact.id,
            channel=inbound.channel,
            to=contact.external_id,
            suggestions=[{"tone": sg.tone, "text": sg.text} for sg in suggestions],
            policy_action=decision.action.value,
            policy_reasons=decision.reasons,
        )
        s.add(draft)
        await s.flush()

        await audit.record(
            s,
            actor="draft_manager",
            action="draft_created",
            target=str(draft.id),
            payload={"inbound_id": inbound.id, "contact_id": contact.id},
        )

    # Send Telegram approval card (outside session — HTTP call)
    await _send_approval_card(contact, inbound, draft)


# ── Telegram approval card helpers ──────────────────────────────────

async def _send_approval_card(
    contact: Contact,
    inbound: InboundMessage,
    draft: Draft,
) -> None:
    from app.services import telegram_notify  # avoid circular
    import httpx

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.allowed_user_ids:
        return

    name = contact.display_name or contact.external_id
    body = inbound.text or f"[{inbound.message_type}]"

    suggestions = draft.suggestions or []
    draft_lines = "\n\n".join(
        f"<b>[{i+1}] {html.escape(s['tone'].upper())}</b>\n{html.escape(s['text'])}"
        for i, s in enumerate(suggestions)
    )

    text = (
        f"✉️ <b>Draft reply needed</b>  (draft #{draft.id})\n"
        f"From: <b>{html.escape(name)}</b>  <code>{html.escape(contact.external_id)}</code>\n"
        f"Policy: <code>{draft.policy_action}</code>\n\n"
        f"📩 <b>Inbound:</b>\n{html.escape(body[:600])}\n\n"
        f"💬 <b>Suggestions:</b>\n{draft_lines}"
    )

    # Build inline keyboard
    approve_buttons = [
        {"text": f"✅ Send [{i+1}]", "callback_data": f"draft:approve:{draft.id}:{i}"}
        for i in range(len(suggestions))
    ]
    action_row = [
        {"text": "✏️ Edit", "callback_data": f"draft:edit:{draft.id}"},
        {"text": "🔄 Regen", "callback_data": f"draft:regen:{draft.id}"},
        {"text": "❌ Reject", "callback_data": f"draft:reject:{draft.id}"},
    ]
    keyboard = {"inline_keyboard": [approve_buttons, action_row]}

    async with httpx.AsyncClient(timeout=10) as client:
        for uid in settings.allowed_user_ids:
            resp = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": uid,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": keyboard,
                },
            )
            if resp.is_error:
                log.warning("draft_manager.card_send_failed", uid=uid, status=resp.status_code)
            else:
                # Store telegram_message_id for later card edits
                tg_msg_id = resp.json().get("result", {}).get("message_id")
                if tg_msg_id:
                    async with session_scope() as s:
                        db_draft = await s.get(Draft, draft.id)
                        if db_draft:
                            db_draft.telegram_message_id = tg_msg_id


async def _send_alert_card(contact: Contact, inbound: InboundMessage, decision) -> None:
    from app.services.telegram_notify import send_admin_message
    name = contact.display_name or contact.external_id
    body = inbound.text or f"[{inbound.message_type}]"
    reasons = ", ".join(decision.reasons)
    await send_admin_message(
        f"🚨 <b>BLOCKED message</b>\n"
        f"From: <b>{html.escape(name)}</b>\n"
        f"Reason: <code>{html.escape(reasons)}</code>\n"
        f"Message: {html.escape(body[:400])}"
    )


async def _notify_no_service_window(contact: Contact, inbound: InboundMessage) -> None:
    from app.services.telegram_notify import send_admin_message
    name = contact.display_name or contact.external_id
    await send_admin_message(
        f"⏰ <b>24 h window closed</b> — cannot reply free-form to <b>{html.escape(name)}</b>.\n"
        f"You'd need to send a template message.\n"
        f"Inbound: {html.escape((inbound.text or '')[:200])}"
    )


async def _notify_draft_error(contact: Contact, inbound: InboundMessage, error: str) -> None:
    from app.services.telegram_notify import send_admin_message
    name = contact.display_name or contact.external_id
    await send_admin_message(
        f"⚠️ <b>Draft generation failed</b> for <b>{html.escape(name)}</b>\n"
        f"Error: <code>{html.escape(error[:300])}</code>"
    )


# ── Callback handlers (called from bot.py) ──────────────────────────

async def approve_draft(draft_id: int, suggestion_index: int, uid: int) -> str:
    """Approve a suggestion, queue in outbox, return confirmation text."""
    async with session_scope() as s:
        draft = await s.get(Draft, draft_id)
        if not draft or draft.status != DraftStatus.pending:
            return "⚠️ Draft not found or already decided."

        suggestions = draft.suggestions or []
        if suggestion_index >= len(suggestions):
            return "⚠️ Invalid suggestion index."

        chosen = suggestions[suggestion_index]
        draft.final_text = chosen["text"]
        draft.chosen_index = suggestion_index
        draft.status = DraftStatus.approved

        outbox_item = await outbox_svc.queue_message(
            s,
            channel=draft.channel,
            to=draft.to,
            body=draft.final_text,
        )
        draft.outbox_id = outbox_item.id
        await s.flush()

        await audit.record(
            s,
            actor=f"user:{uid}",
            action="draft_approved",
            target=str(draft_id),
            payload={"suggestion_index": suggestion_index, "outbox_id": outbox_item.id},
        )
    return f"✅ Queued (outbox #{outbox_item.id}, sends in ~{get_settings().undo_window_seconds}s)"


async def edit_draft(draft_id: int, new_text: str, uid: int) -> str:
    """Save edited text, queue in outbox."""
    async with session_scope() as s:
        draft = await s.get(Draft, draft_id)
        if not draft or draft.status != DraftStatus.pending:
            return "⚠️ Draft not found or already decided."

        draft.final_text = new_text.strip()
        draft.status = DraftStatus.edited

        outbox_item = await outbox_svc.queue_message(
            s,
            channel=draft.channel,
            to=draft.to,
            body=draft.final_text,
        )
        draft.outbox_id = outbox_item.id
        await s.flush()

        await audit.record(
            s,
            actor=f"user:{uid}",
            action="draft_edited",
            target=str(draft_id),
            payload={"outbox_id": outbox_item.id},
        )
    return f"✅ Edited & queued (outbox #{outbox_item.id})"


async def reject_draft(draft_id: int, uid: int) -> str:
    async with session_scope() as s:
        draft = await s.get(Draft, draft_id)
        if not draft or draft.status != DraftStatus.pending:
            return "⚠️ Draft not found or already decided."
        draft.status = DraftStatus.rejected
        await audit.record(
            s,
            actor=f"user:{uid}",
            action="draft_rejected",
            target=str(draft_id),
        )
    return "❌ Draft rejected."


async def regen_draft(draft_id: int, uid: int) -> tuple[str, Draft | None]:
    """Re-run Claude, supersede old draft, create new one.  Returns (status_msg, new_draft)."""
    async with session_scope() as s:
        old = await s.get(Draft, draft_id)
        if not old or old.status != DraftStatus.pending:
            return "⚠️ Draft not found or already decided.", None

        inbound = await s.get(InboundMessage, old.inbound_id)
        contact = await s.get(Contact, old.contact_id)
        if not inbound or not contact:
            return "⚠️ Original message not found.", None

        old.status = DraftStatus.superseded
        await s.flush()

        try:
            suggestions = await drafting.generate(contact, inbound, session=s)
        except Exception as exc:
            return f"⚠️ Regen failed: {exc}", None

        new_draft = Draft(
            inbound_id=inbound.id,
            contact_id=contact.id,
            channel=old.channel,
            to=old.to,
            suggestions=[{"tone": sg.tone, "text": sg.text} for sg in suggestions],
            policy_action=old.policy_action,
            policy_reasons=old.policy_reasons,
        )
        s.add(new_draft)
        await s.flush()

        await audit.record(
            s,
            actor=f"user:{uid}",
            action="draft_regenerated",
            target=str(new_draft.id),
            payload={"superseded_draft_id": draft_id},
        )

    return "🔄 Regenerated!", new_draft


async def cancel_outbox(draft_id: int, uid: int) -> str:
    """Cancel the outbox item for an approved draft (undo window only)."""
    async with session_scope() as s:
        draft = await s.get(Draft, draft_id)
        if not draft or not draft.outbox_id:
            return "⚠️ No outbox item linked to this draft."
        cancelled = await outbox_svc.cancel_message(s, draft.outbox_id)
        if cancelled:
            draft.status = DraftStatus.rejected
        await audit.record(
            s,
            actor=f"user:{uid}",
            action="outbox_cancelled",
            target=str(draft.outbox_id),
            reasons={"cancelled": cancelled},
        )
    return "🛑 Cancelled." if cancelled else "⚠️ Too late — already sent or not pending."
