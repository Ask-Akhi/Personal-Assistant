# -*- coding: utf-8 -*-
"""Telegram bot - the secure control plane.

Commands (Phase 1 + 2 + 3):
  /start, /ping, /whoami
  /unlock <pin>           - start PIN session
  /lock                   - end PIN session
  /note <text>            - quick memory note
  /notes [subject]        - list recent memory entries
  /memory rm <id>         - delete memory entry (PIN required)
  /audit [n]              - last n audit log entries
  /inbox [n]              - last n inbound WhatsApp messages
  /quiet                  - show quiet-hours status
  /drafts [n]             - list pending drafts
  /cancel <outbox_id>     - cancel outbox item inside undo window
  /newevent <title> | <YYYY-MM-DD HH:MM> | [venue] | [group_wa_id] | [cutoff_hours]
                          - create a new cricket/sports event
  /announce [group_wa_id] - post this Saturday's event NOW (creates if needed, re-sends if exists)
  /votes                  - show live RSVPs for the active event
  /rsvpdebug              - show RSVP ingestion/filter diagnostics
  /callinglist            - preview + manually send calling list
  /clearrsvps             - delete all stored RSVPs for the active event (PIN required)
  /wagroups               - list live WhatsApp groups from the sidecar
  /closeevent             - cancel the active event without sending

Inline button callbacks (Phase 2):
  draft:approve:<id>:<idx>  - send suggestion i
  draft:edit:<id>           - enter edit flow (reply with new text)
  draft:regen:<id>          - regenerate suggestions
  draft:reject:<id>         - discard draft
  draft:cancel:<id>         - undo after approval (within 30s window)

Inline button callbacks (Phase 3 events):
  event:send:<id>           - immediately send calling list to WA group
  event:cancel:<id>         - cancel auto-send of calling list
"""
from __future__ import annotations

import asyncio
import base64
import re
from functools import wraps

from datetime import datetime
import httpx
import pytz
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import get_settings
from app.db import engine, session_scope
from app.logging_setup import log, setup_logging
from app.models import (
    AuditLog, Base, Contact, CricketEvent, Draft, DraftStatus,
    EventRsvp, EventStatus, InboundMessage, MemoryKind, RsvpStatus,
)
from app.services import audit, auth, draft_manager, event_manager, memory
from app.services.quiet_hours import is_quiet_now
from app.services.time_utils import as_utc_aware

setup_logging()
settings = get_settings()
AET = pytz.timezone("Australia/Sydney")


# - Decorators -
def allowlist_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        chat_id = update.effective_chat.id if update.effective_chat else None
        log.info(
            "bot.command_received",
            command=getattr(getattr(update, "message", None), "text", None) or getattr(getattr(update, "callback_query", None), "data", None),
            user_id=uid,
            chat_id=chat_id,
        )
        if not auth.is_allowed(uid):
            log.warning("bot.denied", user_id=uid)
            if update.message:
                await update.message.reply_text(
                    f"Not authorised. Add this Telegram ID to TELEGRAM_ALLOWED_USER_IDS: {uid}"
                )
            return
        return await fn(update, ctx)
    return wrapper


def pin_required(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        async with session_scope() as s:
            ok = await auth.pin_valid(s, uid)
        if not ok:
            await update.message.reply_text(
                "🔒 PIN required. Use `/unlock <pin>` first.", parse_mode="Markdown"
            )
            return
        return await fn(update, ctx)
    return wrapper


# - Phase 1 commands -
@allowlist_only
async def cmd_start(update: Update, _ctx):
    await update.message.reply_text(
        "🤖 Personal Assistant - Phase 2 (Drafting)\n"
        "Commands: /ping /whoami /note /notes /audit /inbox /quiet /drafts /wagroups\n"
        "Destructive commands require /unlock <pin>."
    )


@allowlist_only
async def cmd_ping(update: Update, _ctx):
    chat_id = update.effective_chat.id if update.effective_chat else None
    log.info("bot.ping_handled", chat_id=chat_id, user_id=update.effective_user.id if update.effective_user else 0)
    await _ctx.bot.send_message(chat_id=chat_id, text="pong ✅")
    log.info("bot.ping_sent", chat_id=chat_id)


async def handle_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("bot.handler_error", error=str(ctx.error), exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            f"Command failed: {str(ctx.error)[:300]}"
        )


@allowlist_only
async def cmd_whoami(update: Update, _ctx):
    u = update.effective_user
    async with session_scope() as s:
        unlocked = await auth.pin_valid(s, u.id)
    await update.message.reply_text(
        f"id: `{u.id}`\nname: {u.full_name}\nunlocked: {unlocked}",
        parse_mode="Markdown",
    )


@allowlist_only
async def cmd_unlock(update: Update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: /unlock <pin>")
        return
    pin = ctx.args[0]
    uid = update.effective_user.id
    async with session_scope() as s:
        ok = await auth.pin_unlock(s, uid, pin)
        await audit.record(s, actor=f"user:{uid}", action="pin_unlock_attempt", reasons={"ok": ok})
    try:
        await update.message.delete()
    except Exception:
        pass
    await ctx.bot.send_message(chat_id=update.effective_chat.id,
                               text="🔓 Unlocked." if ok else "❌ Bad PIN.")


@allowlist_only
async def cmd_lock(update: Update, _ctx):
    uid = update.effective_user.id
    async with session_scope() as s:
        await auth.pin_lock(s, uid)
        await audit.record(s, actor=f"user:{uid}", action="pin_lock")
    await update.message.reply_text("🔒 Locked.")


@allowlist_only
async def cmd_note(update: Update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: /note <text>")
        return
    text = " ".join(ctx.args)
    uid = update.effective_user.id
    async with session_scope() as s:
        m = await memory.upsert(
            s, kind=MemoryKind.note, subject="global",
            key=f"note-{int(asyncio.get_event_loop().time() * 1000)}",
            value=text,
        )
        await s.flush()
        await audit.record(s, actor=f"user:{uid}", action="memory_note",
                           target=str(m.id), payload={"value": text[:200]})
    await update.message.reply_text("📝 Saved.")


@allowlist_only
async def cmd_notes(update: Update, ctx):
    subject = ctx.args[0] if ctx.args else None
    async with session_scope() as s:
        rows = await memory.list_for(s, subject=subject)
    if not rows:
        await update.message.reply_text("(empty)")
        return
    out = "\n".join(
        f"`{r.id}` [{r.kind.value}] {r.subject}/{r.key}: {r.value[:120]}"
        for r in rows[:20]
    )
    await update.message.reply_text(out, parse_mode="Markdown")


@allowlist_only
@pin_required
async def cmd_memory(update: Update, ctx):
    if len(ctx.args) >= 2 and ctx.args[0] == "rm":
        mid = int(ctx.args[1])
        uid = update.effective_user.id
        async with session_scope() as s:
            ok = await memory.delete(s, mid)
            await audit.record(s, actor=f"user:{uid}", action="memory_delete",
                               target=str(mid), reasons={"ok": ok})
        await update.message.reply_text("🗑️ Deleted." if ok else "Not found.")
        return
    await update.message.reply_text("Usage: /memory rm <id>")


@allowlist_only
async def cmd_audit(update: Update, ctx):
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    n = min(n, 50)
    async with session_scope() as s:
        rows = (await s.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(n)
        )).scalars().all()
    if not rows:
        await update.message.reply_text("(empty)")
        return
    out = "\n".join(
        f"{r.ts:%m-%d %H:%M} {r.actor} → {r.action}" + (f" [{r.target}]" if r.target else "")
        for r in rows
    )
    await update.message.reply_text(f"```\n{out}\n```", parse_mode="Markdown")


@allowlist_only
async def cmd_inbox(update: Update, ctx):
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    n = min(n, 25)
    async with session_scope() as s:
        rows = (await s.execute(
            select(InboundMessage, Contact)
            .join(Contact, InboundMessage.contact_id == Contact.id)
            .order_by(InboundMessage.received_at.desc())
            .limit(n)
        )).all()
    if not rows:
        await update.message.reply_text("(empty)")
        return
    lines = [
        f"{inb.received_at:%m-%d %H:%M} {(c.display_name or c.external_id)}: "
        f"{(inb.text or f'[{inb.message_type}]')[:120]}"
        for inb, c in rows
    ]
    await update.message.reply_text("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")


@allowlist_only
async def cmd_quiet(update: Update, _ctx):
    await update.message.reply_text(
        f"Quiet hours active: {is_quiet_now()} "
        f"({settings.quiet_hours_start:02d}:00-{settings.quiet_hours_end:02d}:00 {settings.timezone})"
    )


# - Phase 2 commands -
@allowlist_only
async def cmd_drafts(update: Update, ctx):
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    n = min(n, 25)
    async with session_scope() as s:
        rows = (await s.execute(
            select(Draft, Contact)
            .join(Contact, Draft.contact_id == Contact.id)
            .where(Draft.status == DraftStatus.pending)
            .order_by(Draft.created_at.desc())
            .limit(n)
        )).all()
    if not rows:
        await update.message.reply_text("No pending drafts ✅")
        return
    lines = []
    for draft, contact in rows:
        name = contact.display_name or contact.external_id
        first_text = (draft.suggestions or [{}])[0].get("text", "")[:80]
        lines.append(f"#{draft.id} -> {name}: {first_text}...")
    await update.message.reply_text("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")


@allowlist_only
async def cmd_cancel_outbox(update: Update, ctx):
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: /cancel <outbox_id>")
        return
    from app.services import outbox as outbox_svc
    outbox_id = int(ctx.args[0])
    uid = update.effective_user.id
    async with session_scope() as s:
        cancelled = await outbox_svc.cancel_message(s, outbox_id)
        await audit.record(s, actor=f"user:{uid}", action="outbox_cancelled",
                           target=str(outbox_id), reasons={"cancelled": cancelled})
    await update.message.reply_text("🛑 Cancelled." if cancelled else "Too late or not found.")


@allowlist_only
async def cmd_canceledit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("awaiting_edit", None)
    ctx.user_data.pop("editing_draft_id", None)
    await update.message.reply_text("Edit cancelled.")


# - Phase 2 inline button callbacks -
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id if query.from_user else 0
    if not auth.is_allowed(uid):
        await query.answer("Not authorised.")
        return
    await query.answer()

    parts = (query.data or "").split(":")
    if len(parts) < 3 or parts[0] != "draft":
        return

    action, draft_id = parts[1], int(parts[2])

    if action == "approve":
        idx = int(parts[3]) if len(parts) > 3 else 0
        msg = await draft_manager.approve_draft(draft_id, idx, uid)
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🛑 Undo", callback_data=f"draft:cancel:{draft_id}")
                ]])
            )
        except Exception:
            pass
        await query.message.reply_text(msg)

    elif action == "edit":
        ctx.user_data["awaiting_edit"] = True
        ctx.user_data["editing_draft_id"] = draft_id
        await query.message.reply_text(
            f"✏️ Send your replacement text for draft #{draft_id}.\n"
            "Use /canceledit to abort."
        )

    elif action == "regen":
        try:
            await query.edit_message_text(
                query.message.text_html + "\n\n⏳ Regenerating...", parse_mode="HTML"
            )
        except Exception:
            pass
        status_msg, new_draft = await draft_manager.regen_draft(draft_id, uid)
        if new_draft:
            async with session_scope() as s:
                contact = await s.get(Contact, new_draft.contact_id)
                inbound = await s.get(InboundMessage, new_draft.inbound_id)
            await draft_manager._send_approval_card(contact, inbound, new_draft)
        await query.message.reply_text(status_msg)

    elif action == "reject":
        msg = await draft_manager.reject_draft(draft_id, uid)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(msg)

    elif action == "cancel":
        msg = await draft_manager.cancel_outbox(draft_id, uid)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(msg)


# - Phase 2/3 Event callbacks (calling list approve/cancel) -
async def handle_event_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id if query.from_user else 0
    if not auth.is_allowed(uid):
        await query.answer("Not authorised.")
        return
    await query.answer()

    parts = (query.data or "").split(":")
    # pattern: event:send:<id>  or  event:cancel:<id>
    if len(parts) < 3 or parts[0] != "event":
        return

    action, event_id = parts[1], int(parts[2])

    async with session_scope() as s:
        db_event = await s.get(CricketEvent, event_id)
        if not db_event:
            await query.message.reply_text("⚠️ Event not found.")
            return

        if action == "send":
            if db_event.status == EventStatus.closed:
                await query.message.reply_text("Already sent.")
                return
            # Build fresh calling list and send immediately
            await event_manager.backfill_event_rsvps_from_inbounds(s, db_event)
            calling_list = await event_manager.build_calling_list(s, db_event)
            db_event.calling_list_text = calling_list
            await audit.record(s, actor=f"user:{uid}", action="calling_list_manual_send",
                               target=str(event_id))

        elif action == "cancel":
            if db_event.status != EventStatus.open:
                await query.message.reply_text("⚠️ Event already closed/cancelled.")
                return
            db_event.status = EventStatus.cancelled
            await audit.record(s, actor=f"user:{uid}", action="calling_list_cancelled",
                               target=str(event_id))
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text(f"🛑 Calling list send cancelled for *{db_event.title}*.",
                                           parse_mode="Markdown")
            return

    if action == "send":
        # Send to WA group
        from app.services import wa_sender
        group_id = db_event.group_wa_id
        if not group_id:
            from app.services.group_registry import get_group_for_task
            async with session_scope() as s:
                group_id = await get_group_for_task(s, "weekly_cricket")
                if group_id:
                    ev = await s.get(CricketEvent, event_id)
                    if ev:
                        ev.group_wa_id = group_id

        if group_id:
            try:
                await wa_sender.send_text(group_id, calling_list)
                async with session_scope() as s:
                    ev = await s.get(CricketEvent, event_id)
                    if ev:
                        ev.calling_list_text = calling_list
                        ev.group_wa_id = group_id
                        ev.status = EventStatus.closed
                await query.message.reply_text(
                    f"✅ Calling list sent to WhatsApp group `{group_id}`!",
                    parse_mode="Markdown",
                )
            except Exception as exc:
                await query.message.reply_text(
                    f"⚠️ WA send failed: `{str(exc)[:200]}`\n\nCopy manually:\n```\n{calling_list[:3000]}\n```",
                    parse_mode="Markdown"
                )
        else:
            await query.message.reply_text(
                f"📋 No WA group ID set. Copy the calling list manually:\n```\n{calling_list[:3000]}\n```",
                parse_mode="Markdown"
            )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


def _sidecar_base_url() -> str | None:
    url = (get_settings().whatsapp_group_sender_url or "").strip()
    if not url:
        return None
    if url.endswith("/send"):
        return url[: -len("/send")].rstrip("/")
    return url.rstrip("/")


async def _fetch_live_groups() -> list[dict]:
    base_url = _sidecar_base_url()
    if not base_url:
        raise RuntimeError("WHATSAPP_GROUP_SENDER_URL not configured")

    headers = {}
    token = (get_settings().whatsapp_group_sender_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{base_url}/groups", headers=headers)
    if response.is_error:
        raise RuntimeError(
            f"Group sender error: HTTP {response.status_code} {response.text[:200]}"
        )

    data = response.json()
    groups = data.get("groups") or []
    if not isinstance(groups, list):
        return []
    return [g for g in groups if isinstance(g, dict)]


async def _fetch_sidecar_health() -> dict:
    base_url = _sidecar_base_url()
    if not base_url:
        raise RuntimeError("WHATSAPP_GROUP_SENDER_URL not configured")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{base_url}/healthz")
    if response.is_error:
        raise RuntimeError(
            f"Group sender health error: HTTP {response.status_code} {response.text[:200]}"
        )
    data = response.json()
    return data if isinstance(data, dict) else {}


def _group_callback_data(event_id: int, group_id: str) -> str:
    encoded = base64.urlsafe_b64encode(group_id.encode("utf-8")).decode("ascii").rstrip("=")
    return f"wagroup:set:{event_id}:{encoded}"


def _fmt_aet(value: datetime, fmt: str) -> str:
    return as_utc_aware(value).astimezone(AET).strftime(fmt)


@allowlist_only
async def cmd_wagroups(update: Update, _ctx):
    """Show live WhatsApp groups from the sidecar and allow choosing one for the active event."""
    async with session_scope() as s:
        ev = await event_manager.resolve_event_for_control_plane(s)
    if not ev:
        await update.message.reply_text("No active event. Create one with /newevent first.")
        return

    try:
        groups = await _fetch_live_groups()
    except Exception as exc:
        await update.message.reply_text(
            f"⚠️ Could not load WhatsApp groups: `{str(exc)[:250]}`",
            parse_mode="Markdown",
        )
        return

    if not groups:
        await update.message.reply_text("No WhatsApp groups returned by the sidecar yet.")
        return

    rows = []
    for group in groups[:24]:
        group_id = str(group.get("id") or "").strip()
        subject = str(group.get("subject") or "Unknown").strip()
        participants = group.get("participants") or 0
        if not group_id.endswith("@g.us"):
            continue
        if group.get("is_community"):
            # Community container — can't send messages here; shown greyed out
            label = f"🏘️ {subject[:24]} (community)"
        elif group.get("community_jid"):
            # Sub-group inside a community — this is the correct target
            label = f"📋 {subject[:26]} ({participants})"
        else:
            label = f"{subject[:28]} ({participants})"
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=_group_callback_data(ev.id, group_id),
            )
        ])

    if not rows:
        await update.message.reply_text("No group IDs ending in @g.us were returned.")
        return

    await update.message.reply_text(
        f"Choose the WhatsApp group to bind to *{ev.title}*:\n"
        f"_(📋 = sub-group inside a community — use this one)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows[:20]),
    )


@allowlist_only
async def cmd_groupdebug(update: Update, _ctx):
    """Show raw group list from sidecar including JIDs and community flags."""
    try:
        groups = await _fetch_live_groups()
    except Exception as exc:
        await update.message.reply_text(f"⚠️ {exc}", parse_mode="HTML")
        return

    if not groups:
        await update.message.reply_text("No groups returned.")
        return

    lines = []
    for g in groups[:30]:
        gid = g.get("id", "?")
        subj = g.get("subject", "?")
        pts = g.get("participants", 0)
        flags = []
        if g.get("is_community"):
            flags.append("COMMUNITY")
        if g.get("community_jid"):
            flags.append(f"sub-of:{g['community_jid'][:20]}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"<code>{gid}</code>\n  {subj} ({pts}){flag_str}")

    await update.message.reply_text(
        "<b>WA Groups (raw):</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
    )


async def handle_wagroup_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id if query.from_user else 0
    if not auth.is_allowed(uid):
        await query.answer("Not authorised.")
        return
    await query.answer()

    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[0] != "wagroup" or parts[1] != "set":
        return

    event_id = int(parts[2])
    padding = "=" * (-len(parts[3]) % 4)
    group_id = base64.urlsafe_b64decode((parts[3] + padding).encode("ascii")).decode("utf-8")

    try:
        groups = await _fetch_live_groups()
        selected = next((g for g in groups if str(g.get("id") or "") == group_id), {})
        group_name = str(selected.get("subject") or group_id).lstrip("↳ ").strip()
    except Exception:
        group_name = group_id

    async with session_scope() as s:
        ev = await s.get(CricketEvent, event_id)
        if not ev:
            await query.message.reply_text("⚠️ Event not found.")
            return
        ev.group_wa_id = group_id
        await audit.record(
            s,
            actor=f"user:{uid}",
            action="event_group_assigned",
            target=str(event_id),
            payload={"group_id": group_id, "group_name": group_name},
        )
        from app.services.group_registry import auto_register_group
        await auto_register_group(
            s,
            raw_message={"from": group_id},
            group_name=group_name,
        )

    await query.message.reply_text(
        f"✅ Bound *{ev.title}* to `{group_id}`",
        parse_mode="Markdown",
    )


# - Edit-reply plain text handler -
@allowlist_only
async def handle_edit_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_edit"):
        return
    draft_id = ctx.user_data.pop("editing_draft_id", None)
    ctx.user_data.pop("awaiting_edit", None)
    if not draft_id:
        return
    uid = update.effective_user.id
    new_text = (update.message.text or "").strip()
    if not new_text:
        await update.message.reply_text("Empty text - edit cancelled.")
        return
    msg = await draft_manager.edit_draft(draft_id, new_text, uid)
    await update.message.reply_text(msg)


# - Phase 2/3 Event commands -

@allowlist_only
async def cmd_newevent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /newevent <title> | <YYYY-MM-DD HH:MM> | [venue] | [group_wa_id] | [cutoff_hours]
    Example: /newevent T20 Cricket | 2026-06-07 06:30 | Oval Ground | 120363XXXXXX@g.us | 36
    """
    text = " ".join(ctx.args) if ctx.args else ""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: `/newevent <title> | <YYYY-MM-DD HH:MM> | [venue] | [group_wa_id] | [cutoff_hours]`\n"
            "Example: `/newevent T20 Cricket | 2026-06-07 06:30 | Oval Ground | 120363XXX@g.us | 36`",
            parse_mode="Markdown"
        )
        return

    from datetime import datetime, timezone, timedelta
    title = parts[0]
    try:
        event_at_naive = datetime.strptime(parts[1], "%Y-%m-%d %H:%M")
        event_at = event_at_naive.replace(tzinfo=timezone.utc)
    except ValueError:
        await update.message.reply_text("⚠️ Date format must be `YYYY-MM-DD HH:MM` (UTC)")
        return

    venue = parts[2] if len(parts) > 2 else None
    group_wa_id = parts[3] if len(parts) > 3 else None
    cutoff_hours = (
        float(parts[4])
        if len(parts) > 4 and parts[4].replace(".", "").isdigit()
        else get_settings().event_default_cutoff_hours
    )

    cutoff_at = event_at - timedelta(hours=cutoff_hours)

    uid = update.effective_user.id
    async with session_scope() as s:
        # Close any previously open events
        from sqlalchemy import select as _select
        open_events = (await s.execute(
            _select(CricketEvent).where(CricketEvent.status == EventStatus.open)
        )).scalars().all()
        for ev in open_events:
            ev.status = EventStatus.cancelled

        new_event = CricketEvent(
            title=title,
            event_at=event_at.replace(tzinfo=None),
            venue=venue,
            group_wa_id=group_wa_id,
            cutoff_hours=cutoff_hours,
            cutoff_at=cutoff_at.replace(tzinfo=None),
            status=EventStatus.open,
        )
        s.add(new_event)
        await s.flush()
        event_id = new_event.id

        await audit.record(s, actor=f"user:{uid}", action="event_created",
                           target=str(event_id),
                           payload={"title": title, "event_at": str(event_at),
                                    "cutoff_hours": cutoff_hours})

    venue_line = f"\n📍 Venue: {venue}" if venue else ""
    group_line = f"\n📱 WA Group: `{group_wa_id}`" if group_wa_id else "\n⚠️ No WA group ID set - calling list will be sent to Telegram only."

    await update.message.reply_text(
        f"✅ *Event created!* (#{event_id})\n"
        f"🏏 *{title}*\n"
        f"📅 {_fmt_aet(event_at.replace(tzinfo=None), '%a, %b %-d at %-I:%M %p AET')}"
        f"{venue_line}\n"
        f"⏰ Cutoff: {_fmt_aet(cutoff_at.replace(tzinfo=None), '%a, %b %-d at %-I:%M %p AET')} ({cutoff_hours}h before)"
        f"{group_line}\n\n"
        f"Now send the event announcement to your WhatsApp group. RSVPs will be tracked automatically!",
        parse_mode="Markdown"
    )


@allowlist_only
async def cmd_votes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show current RSVP count for the active event."""
    async with session_scope() as s:
        ev = await event_manager.resolve_event_for_control_plane(s)
        if not ev:
            await update.message.reply_text("No active event. Create one with /newevent")
            return
        await event_manager.backfill_event_rsvps_from_inbounds(s, ev)
        rows = await event_manager.get_group_filtered_rsvps(s, ev)

    if not rows:
        await update.message.reply_text(
            f"🏏 *{ev.title}* - No RSVPs yet.\n"
            f"📅 {_fmt_aet(ev.event_at, '%a, %b %-d')} | "
            f"Cutoff: {_fmt_aet(ev.cutoff_at, '%a, %b %-d %-I:%M %p AET') if ev.cutoff_at else 'N/A'}",
            parse_mode="Markdown"
        )
        return

    going = [(r, c) for r, c in rows if r.status in (RsvpStatus.yes, RsvpStatus.wnbo)]
    not_coming = [(r, c) for r, c in rows if r.status == RsvpStatus.no]
    maybe = [(r, c) for r, c in rows if r.status == RsvpStatus.maybe]

    def fmt(pairs):
        return ", ".join(
            f"{c.display_name or c.external_id}" + (" _(wnbo)_" if r.status == RsvpStatus.wnbo else "")
            for r, c in pairs
        )

    lines = [f"🏏 *{ev.title}* - Live RSVPs\n"]
    if going:
        lines.append(f"✅ *Going ({len(going)}):* {fmt(going)}")
    if maybe:
        lines.append(f"🤔 *Maybe ({len(maybe)}):* {fmt(maybe)}")
    if not_coming:
        lines.append(f"❌ *Not coming ({len(not_coming)}):* {fmt(not_coming)}")

    cutoff_str = _fmt_aet(ev.cutoff_at, "%a %b %-d %-I:%M %p AET") if ev.cutoff_at else "N/A"
    lines.append(f"\n⏰ Cutoff: {cutoff_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@allowlist_only
async def cmd_rsvpdebug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show RSVP ingestion/filter diagnostics for the current event."""
    from app.services.wa_groups import fetch_group_participant_ids

    async with session_scope() as s:
        ev = await event_manager.resolve_event_for_control_plane(s)
        if not ev:
            await update.message.reply_text("No active or recent event found.")
            return

        await event_manager.backfill_event_rsvps_from_inbounds(s, ev)
        raw_rows = (await s.execute(
            select(EventRsvp, Contact, InboundMessage)
            .join(Contact, Contact.id == EventRsvp.contact_id)
            .outerjoin(InboundMessage, InboundMessage.id == EventRsvp.inbound_id)
            .where(EventRsvp.event_id == ev.id)
            .order_by(EventRsvp.created_at)
        )).all()
        real_rows = await event_manager.get_rsvps(s, ev.id)
        filtered_rows = await event_manager.get_group_filtered_rsvps(s, ev)

    participant_count = "n/a"
    participant_error = None
    if ev.group_wa_id:
        try:
            participant_count = str(len(await fetch_group_participant_ids(ev.group_wa_id)))
        except Exception as exc:
            participant_error = str(exc)[:160]

    try:
        health = await _fetch_sidecar_health()
    except Exception as exc:
        health = {"error": str(exc)[:160]}

    forward = health.get("forward_stats") if isinstance(health.get("forward_stats"), dict) else {}
    sample = []
    for rsvp, contact, inbound in raw_rows[:8]:
        raw = inbound.raw if inbound and isinstance(inbound.raw, dict) else {}
        aliases = raw.get("participant_aliases") if isinstance(raw.get("participant_aliases"), list) else []
        inbound_group = raw.get("group_id") or raw.get("remote_jid") or raw.get("remoteJid") or "n/a"
        alias_text = f" aliases={aliases[:3]}" if aliases else ""
        reason = event_manager.rsvp_filter_reason(ev, contact, inbound)
        sample.append(
            f"- {contact.display_name or contact.external_id}: {rsvp.status.value} "
            f"[{contact.external_id}] group={inbound_group}{alias_text} -> {reason}"
        )

    sidecar_seen = int(forward.get("seenMessages") or 0)
    sidecar_attempts = int(forward.get("attempts") or 0)
    lines = [
        f"RSVP debug for event #{ev.id}",
        f"title: {ev.title}",
        f"group: {ev.group_wa_id or 'not set'}",
        f"sidecar connected: {health.get('connected')}",
        f"sidecar assistant callback configured: {health.get('assistant_api_configured')}",
        f"sidecar history sync: {health.get('sync_full_history')}",
        f"sidecar seen: messages={forward.get('seenMessages')} last_seen={forward.get('lastSeenAt')}",
        f"sidecar skipped: no_key={forward.get('skippedNoKey')} from_me={forward.get('skippedFromMe')} non_group={forward.get('skippedNonGroup')} no_content={forward.get('skippedNoContent')} non_person={forward.get('skippedNonPerson')}",
        f"sidecar forwards: attempts={forward.get('attempts')} success={forward.get('successes')} last_count={forward.get('lastForwardedCount')} last_status={forward.get('lastStatus')}",
        f"participant aliases fetched: {participant_count}",
        f"db rows: raw={len(raw_rows)} real={len(real_rows)} group_filtered={len(filtered_rows)}",
    ]
    recent = health.get("recent_group_messages") if isinstance(health.get("recent_group_messages"), list) else []
    if recent:
        lines.append("recent sidecar messages:")
        for item in recent[-3:]:
            msg_keys = ",".join(item.get("message_keys") or [])
            lines.append(
                f"- group={item.get('group_id')} from_me={item.get('from_me')} "
                f"event_responses={item.get('event_responses')} msg_keys={msg_keys or 'none'}"
            )
    if health.get("connected") is True and sidecar_seen == 0 and sidecar_attempts == 0:
        lines.append("warning: sidecar is connected but has not seen/forwarded group messages since startup")
    if health.get("assistant_api_configured") is False:
        lines.append("warning: sidecar ASSISTANT_API_URL is not configured, so WhatsApp votes cannot sync into PI")
    if raw_rows and not filtered_rows:
        lines.append("warning: RSVP rows exist but calling-list filter excluded all of them; see row reasons below")
    if participant_error:
        lines.append(f"participant error: {participant_error}")
    if health.get("error"):
        lines.append(f"health error: {health['error']}")
    if sample:
        lines.append("sample raw rows:")
        lines.extend(sample)

    await update.message.reply_text("\n".join(lines[:24]))


@allowlist_only
async def cmd_callinglist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Preview or manually send the calling list for the active event."""
    async with session_scope() as s:
        ev = await event_manager.resolve_event_for_control_plane(s)
        if not ev:
            await update.message.reply_text("No active event.")
            return
        await event_manager.backfill_event_rsvps_from_inbounds(s, ev)
        calling_list = await event_manager.build_calling_list(s, ev)

    if ev.status != EventStatus.open:
        prefix = f"📋 *Preview - {ev.title}* (most recent event)\n\n"
    else:
        prefix = f"📋 *Preview - {ev.title}*\n\n"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Send to WA Group", callback_data=f"event:send:{ev.id}"),
        InlineKeyboardButton("🛑 Cancel", callback_data=f"event:cancel:{ev.id}"),
    ]])
    await update.message.reply_text(
        f"{prefix}```\n{calling_list}\n```",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


@allowlist_only
async def cmd_announce(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Manually trigger this week's event announcement right now.
    Usage: /announce [group_wa_id]
    If a group_wa_id is given, it overrides auto-discovery.
    The event is created for the coming Saturday 6:30 AM AET.
    If an event already exists, the announcement is re-sent without duplicating the DB row.
    """
    from app.jobs.weekly_event_poster import (
        _next_saturday, _already_exists_for, _get_existing_for, _create_and_announce, AET
    )
    from app.services.group_registry import get_group_for_task
    from app.services import wa_sender
    import pytz

    override_group = ctx.args[0].strip() if ctx.args else None
    if override_group and override_group.startswith("/"):
        await update.message.reply_text(
            "Usage: /announce [whatsapp_number]\n"
            "Send /announce by itself, then send /testrsvp commands separately."
        )
        return

    now = datetime.now(AET)
    saturday = _next_saturday(now)

    # If already exists, just re-post the announcement (don't create duplicate)
    async with session_scope() as s:
        saturday_utc = saturday.astimezone(pytz.utc).replace(tzinfo=None)
        ev = await event_manager.resolve_event_for_control_plane(s, preferred_event_at=saturday_utc)
        if ev is None:
            # check if one exists (even closed) for this Saturday
            from app.jobs.weekly_event_poster import _already_exists_for as _ae
            already = await _ae(saturday)
        else:
            already = True

    if not already:
        await update.message.reply_text("⏳ Creating event and posting announcement...")
        try:
            await _create_and_announce(saturday)
            await update.message.reply_text("✅ Event created and announcement posted!")
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Error: `{str(exc)[:300]}`", parse_mode="Markdown")
        return

    # Event already exists — re-send announcement only
    await update.message.reply_text("ℹ️ Event already exists. Re-sending announcement to group...")

    async with session_scope() as s:
        saturday_utc = saturday.astimezone(pytz.utc).replace(tzinfo=None)
        ev = await event_manager.resolve_event_for_control_plane(s, preferred_event_at=saturday_utc)
        if ev:
            db_event = await s.get(CricketEvent, ev.id)
            if db_event and db_event.status != EventStatus.open:
                db_event.status = EventStatus.open
            ev = db_event or ev

    if not ev:
        await update.message.reply_text(
            "⚠️ I can see a weekly event should exist, but I could not load its row safely. "
            "I did not recreate anything, so your existing list stays untouched."
        )
        return

    # Resolve group ID
    group_id = override_group
    if not group_id:
        async with session_scope() as s:
            group_id = await get_group_for_task(s, "weekly_cricket")
    if not group_id:
        group_id = get_settings().whatsapp_group_id or None
    if not group_id and ev.group_wa_id:
        group_id = ev.group_wa_id

    if group_id:
        async with session_scope() as s:
            db_event = await s.get(CricketEvent, ev.id)
            if db_event:
                db_event.group_wa_id = group_id

    import pytz as _pytz
    AET_tz = _pytz.timezone("Australia/Sydney")
    ev_aet = ev.event_at.replace(tzinfo=_pytz.utc).astimezone(AET_tz)
    day_str = ev_aet.strftime("%A, %d %b %Y")
    start_str = ev_aet.strftime("%-I:%M %p")
    end_str = ev_aet.replace(hour=10, minute=30).strftime("%-I:%M %p")
    cutoff_aet = ev.cutoff_at.replace(tzinfo=_pytz.utc).astimezone(AET_tz) if ev.cutoff_at else None
    cutoff_str = cutoff_aet.strftime("%a %d %b, %-I:%M %p AET") if cutoff_aet else "N/A"

    announcement = (
        f"Hi CHCC Members! \U0001f44b\n\n"
        f"\U0001f3cf *T20 Cricket - This Saturday!*\n\n"
        f"\U0001f4c5 {day_str}\n"
        f"\u23f0 {start_str} - {end_str}\n"
        f"\U0001f4cd Coolong Reserve\n\n"
        f"Please reply in this group message thread:\n"
        f"\u2705 *YES* - Coming\n"
        f"\U0001f3cf *WNBO* - Coming but Will Not Bowl\n"
        f"\u274c *NO* - Can't make it\n"
        f"\u26a0\ufe0f Please type the reply; WhatsApp event-card taps are not visible to the automation.\n\n"
        f"\U0001f4cb Calling list will be shared by {cutoff_str}. See you on the field! \U0001f3c6"
    )

    if group_id:
        try:
            wa_msg_id = await wa_sender.send_text(group_id, announcement)
            async with session_scope() as s:
                db_event = await s.get(CricketEvent, ev.id)
                if db_event:
                    db_event.group_wa_id = group_id
                    db_event.announcement_wa_msg_id = wa_msg_id
            await update.message.reply_text(f"✅ Announcement re-sent to group `{group_id}`!", parse_mode="Markdown")
        except Exception as exc:
            await update.message.reply_text(
                f"⚠️ WA send failed: `{str(exc)[:200]}`\n\nCopy manually:\n```\n{announcement}\n```",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            f"⚠️ No WA group ID found. Copy and paste manually:\n\n```\n{announcement}\n```\n\n"
            f"Use `/wagroups` to pick a WhatsApp group from the sidecar, or use: `/announce <group_wa_id>`.",
            parse_mode="Markdown"
        )


class _NoActiveEventError(Exception):
    """Raised when a manual RSVP is attempted with no open event."""


def _manual_contact_jid(name: str) -> str:
    """Build a filter-valid synthetic JID for a manually entered name.

    Must satisfy event_manager._is_real_rsvp_contact(): contains '@' and does
    NOT end in @g.us / @broadcast. We use a stable, unique-per-name address so
    re-entering the same name updates the existing RSVP instead of duplicating.
    """
    slug = re.sub(r"[^a-z0-9]+", "", name.lower()) or "guest"
    return f"{slug}@manual.local"


async def _record_manual_rsvp(name: str, status, vote_raw: str):
    """Create/refresh a synthetic contact + inbound and upsert the RSVP.

    The inbound carries the active event's group_wa_id in raw so the
    group-filtered calling list keeps it. Raises _NoActiveEventError if there
    is no open event.
    """
    from app.models import InboundMessage, RsvpStatus
    from app.services.time_utils import utc_now_naive

    jid = _manual_contact_jid(name)
    async with session_scope() as s:
        ev = await event_manager.get_active_event(s)
        if not ev:
            raise _NoActiveEventError()

        contact = (await s.execute(
            select(Contact).where(Contact.external_id == jid)
        )).scalar_one_or_none()
        if not contact:
            contact = Contact(external_id=jid, display_name=name)
            s.add(contact)
            await s.flush()
        elif contact.display_name != name:
            contact.display_name = name

        inbound = InboundMessage(
            channel="manual",
            external_id=f"manual_{ev.id}_{jid}",
            contact_id=contact.id,
            from_external_id=jid,
            message_type="text",
            text=vote_raw,
            raw={"group_id": ev.group_wa_id, "source": "manual"},
            received_at=utc_now_naive(),
        )
        # Upsert inbound by (channel, external_id) so repeats don't error.
        existing_inbound = (await s.execute(
            select(InboundMessage).where(
                InboundMessage.channel == "manual",
                InboundMessage.external_id == inbound.external_id,
            )
        )).scalar_one_or_none()
        if existing_inbound:
            existing_inbound.text = vote_raw
            existing_inbound.raw = {"group_id": ev.group_wa_id, "source": "manual"}
            inbound = existing_inbound
        else:
            s.add(inbound)
            await s.flush()

        await event_manager.upsert_rsvp(
            s, event=ev, contact=contact, inbound=inbound,
            status=status, raw_text=vote_raw,
            note="Will Not Bowl" if status == RsvpStatus.wnbo else None,
        )


@allowlist_only
async def cmd_testrsvp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Inject a manual RSVP into the active event from Telegram.
    Usage: /testrsvp <Name> <yes|no|wnbo|maybe>
    Example: /testrsvp "Akhi" yes
             /testrsvp "Raj" wnbo
             /testrsvp "Sam" no
    """
    from app.models import RsvpStatus

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/testrsvp <Name> <yes|no|wnbo|maybe>`\n"
            "Example: `/testrsvp Akhi yes`",
            parse_mode="Markdown"
        )
        return

    name = ctx.args[0].strip()
    vote_raw = ctx.args[1].strip().lower()

    status_map = {
        "yes": RsvpStatus.yes,
        "y": RsvpStatus.yes,
        "no": RsvpStatus.no,
        "n": RsvpStatus.no,
        "wnbo": RsvpStatus.wnbo,
        "maybe": RsvpStatus.maybe,
        "m": RsvpStatus.maybe,
    }
    status = status_map.get(vote_raw)
    if not status:
        await update.message.reply_text(
            f"❌ Unknown vote `{vote_raw}`. Use: yes / no / wnbo / maybe",
            parse_mode="Markdown"
        )
        return

    try:
        await _record_manual_rsvp(name, status, vote_raw)
    except _NoActiveEventError:
        await update.message.reply_text("❌ No active event. Run `/announce` first.", parse_mode="Markdown")
        return

    emoji = {"yes": "✅", "no": "❌", "wnbo": "🏏", "maybe": "🤔"}.get(vote_raw, "📝")
    await update.message.reply_text(
        f"{emoji} RSVP recorded!\n"
        f"*{name}* → `{vote_raw.upper()}`\n\n"
        f"Run `/votes` to see all RSVPs or `/callinglist` to preview.",
        parse_mode="Markdown"
    )


@allowlist_only
async def cmd_bulkrsvp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Add multiple RSVPs at once.
    Usage: /bulkrsvp yes: Name1, Name2, Name3 | wnbo: Name4 | no: Name5, Name6 | maybe: Name7
    Example: /bulkrsvp yes: Akhi, Hitesh, Venu | wnbo: Manbir | no: Jogesh, Ankur | maybe: Giri
    """
    args_text = " ".join(ctx.args).strip() if ctx.args else ""
    if not args_text:
        await update.message.reply_text(
            "Usage: <code>/bulkrsvp yes: Name1, Name2 | wnbo: Name3 | no: Name4 | maybe: Name5</code>",
            parse_mode="HTML"
        )
        return

    async with session_scope() as s:
        ev = await event_manager.get_active_event(s)
        if not ev:
            await update.message.reply_text(
                "No active event. Run /announce first to create one."
            )
            return

    results = []
    errors = []

    # Parse sections split by |
    sections = args_text.split("|")
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if ":" not in section:
            errors.append(f"Skipped (no colon): {section}")
            continue
        status_raw, names_raw = section.split(":", 1)
        status_raw = status_raw.strip().lower()
        status_map = {
            "yes": "yes", "coming": "yes", "y": "yes",
            "no": "no", "n": "no",
            "wnbo": "wnbo", "w": "wnbo",
            "maybe": "maybe", "m": "maybe",
        }
        status = status_map.get(status_raw)
        if not status:
            errors.append(f"Unknown status '{status_raw}'")
            continue
        names = [n.strip() for n in names_raw.split(",") if n.strip()]
        for name in names:
            try:
                from app.models import RsvpStatus
                status_enum = RsvpStatus[status]
                await _record_manual_rsvp(name, status_enum, status)
                results.append(f"✅ {name} → {status.upper()}")
            except _NoActiveEventError:
                errors.append(f"❌ {name}: no active event")
            except Exception as exc:
                errors.append(f"❌ {name}: {exc}")

    reply = f"<b>Bulk RSVP done ({len(results)} added)</b>\n"
    if results:
        reply += "\n".join(results)
    if errors:
        reply += "\n\n⚠️ Errors:\n" + "\n".join(errors)
    await update.message.reply_text(reply, parse_mode="HTML")


@allowlist_only
async def cmd_cleartestrsvps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove all manually entered RSVPs from the active event (for clean re-testing)."""
    from app.models import EventRsvp

    async with session_scope() as s:
        ev = await event_manager.get_active_event(s)
        if not ev:
            await update.message.reply_text("No active event.")
            return

        # Manual contacts use the synthetic @manual.local domain.
        manual_contacts = (await s.execute(
            select(Contact).where(Contact.external_id.like("%@manual.local"))
        )).scalars().all()

        count = 0
        for tc in manual_contacts:
            rsvps = (await s.execute(
                select(EventRsvp).where(EventRsvp.contact_id == tc.id, EventRsvp.event_id == ev.id)
            )).scalars().all()
            for r in rsvps:
                await s.delete(r)
                count += 1

    await update.message.reply_text(
        f"🗑️ Cleared {count} manual RSVP(s). Real WhatsApp votes are untouched."
    )


@allowlist_only
@pin_required
async def cmd_clearrsvps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove all stored RSVPs from the active event."""
    uid = update.effective_user.id if update.effective_user else 0
    async with session_scope() as s:
        ev = await event_manager.resolve_event_for_control_plane(s)
        if not ev:
            await update.message.reply_text("No active or recent event found.")
            return

        rows = (await s.execute(
            select(EventRsvp).where(EventRsvp.event_id == ev.id)
        )).scalars().all()
        count = len(rows)
        for row in rows:
            await s.delete(row)
        await audit.record(
            s,
            actor=f"user:{uid}",
            action="event_rsvps_cleared",
            target=str(ev.id),
            payload={"count": count, "title": ev.title},
        )

    await update.message.reply_text(
        f"Cleared {count} stored RSVP row(s) for {ev.title}."
    )


@allowlist_only
async def cmd_sethost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set the organiser name shown first in the calling list: /sethost <name>"""
    name = " ".join(ctx.args).strip() if ctx.args else ""
    if not name:
        await update.message.reply_text("Usage: /sethost <name>\nExample: /sethost Abhishek")
        return
    async with session_scope() as s:
        ev = await event_manager.resolve_event_for_control_plane(s)
        if not ev:
            await update.message.reply_text("No active or recent event found.")
            return
        ev.host_name = name
        ev_title = ev.title
    await update.message.reply_text(
        f"✅ Organiser set to <b>{name}</b> for <b>{ev_title}</b>.\n"
        f"They will appear first in the calling list.",
        parse_mode="HTML",
    )


@allowlist_only
async def cmd_closeevent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually close the active event without sending the calling list."""
    uid = update.effective_user.id
    async with session_scope() as s:
        ev = await event_manager.get_active_event(s)
        if not ev:
            await update.message.reply_text("No active event to close.")
            return
        ev.status = EventStatus.cancelled
        await audit.record(s, actor=f"user:{uid}", action="event_manually_closed",
                           target=str(ev.id))
    await update.message.reply_text(f"🛑 Event *{ev.title}* closed.", parse_mode="Markdown")


# - Bootstrap -
async def _ensure_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@allowlist_only
async def cmd_groups(update: Update, _ctx):
    """List all auto-discovered WhatsApp groups."""
    from app.services.group_registry import list_groups
    async with session_scope() as s:
        groups = await list_groups(s)
    if not groups:
        await update.message.reply_text(
            "No WhatsApp groups discovered yet.\n"
            "Send any message from your WA group and PI will auto-register it."
        )
        return
    lines = ["<b>Registered WhatsApp Groups</b>\n"]
    for g in groups:
        task_tag = f"  [{g['task']}]" if g["task"] != "unknown" else "  [no task]"
        lines.append(f"<b>{g['name']}</b>{task_tag}\n<code>{g['group_id']}</code>\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def build_app():
    """Build and return the PTB Application (for embedding in main.py lifespan)."""
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(settings.telegram_bot_token).build()

    # Phase 1
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ping",    cmd_ping))
    app.add_handler(CommandHandler("whoami",  cmd_whoami))
    app.add_handler(CommandHandler("unlock",  cmd_unlock))
    app.add_handler(CommandHandler("lock",    cmd_lock))
    app.add_handler(CommandHandler("note",    cmd_note))
    app.add_handler(CommandHandler("notes",   cmd_notes))
    app.add_handler(CommandHandler("memory",  cmd_memory))
    app.add_handler(CommandHandler("audit",   cmd_audit))
    app.add_handler(CommandHandler("inbox",   cmd_inbox))
    app.add_handler(CommandHandler("quiet",   cmd_quiet))
    # Phase 2
    app.add_handler(CommandHandler("drafts",      cmd_drafts))
    app.add_handler(CommandHandler("cancel",      cmd_cancel_outbox))
    app.add_handler(CommandHandler("canceledit",  cmd_canceledit))
    app.add_handler(CallbackQueryHandler(handle_callback,       pattern=r"^draft:"))
    app.add_handler(CallbackQueryHandler(handle_event_callback, pattern=r"^event:"))
    # Phase 3 - Event automation
    app.add_handler(CommandHandler("newevent",       cmd_newevent))
    app.add_handler(CommandHandler("announce",       cmd_announce))
    app.add_handler(CommandHandler("votes",          cmd_votes))
    app.add_handler(CommandHandler("rsvpdebug",      cmd_rsvpdebug))
    app.add_handler(CommandHandler("callinglist",    cmd_callinglist))
    app.add_handler(CommandHandler("wagroups",       cmd_wagroups))
    app.add_handler(CommandHandler("groupdebug",     cmd_groupdebug))
    app.add_handler(CommandHandler("sethost",         cmd_sethost))
    app.add_handler(CommandHandler("closeevent",      cmd_closeevent))
    app.add_handler(CommandHandler("groups",          cmd_groups))
    app.add_handler(CommandHandler("testrsvp",        cmd_testrsvp))
    app.add_handler(CommandHandler("cleartestrsvps",  cmd_cleartestrsvps))
    app.add_handler(CommandHandler("clearrsvps",      cmd_clearrsvps))
    app.add_handler(CommandHandler("bulkrsvp",        cmd_bulkrsvp))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_reply))
    app.add_handler(CallbackQueryHandler(handle_wagroup_callback, pattern=r"^wagroup:"))
    app.add_error_handler(handle_error)

    return app


def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    if not settings.allowed_user_ids:
        raise SystemExit("TELEGRAM_ALLOWED_USER_IDS not set (get yours from @userinfobot)")

    asyncio.get_event_loop().run_until_complete(_ensure_schema())

    app = build_app()

    log.info("bot.starting", allowed=len(settings.allowed_user_ids))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
