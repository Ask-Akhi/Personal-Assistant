"""Telegram bot — the secure control plane.

Commands (Phase 1 + 2):
  /start, /ping, /whoami
  /unlock <pin>           — start PIN session
  /lock                   — end PIN session
  /note <text>            — quick memory note
  /notes [subject]        — list recent memory entries
  /memory rm <id>         — delete memory entry (PIN required)
  /audit [n]              — last n audit log entries
  /inbox [n]              — last n inbound WhatsApp messages
  /quiet                  — show quiet-hours status
  /drafts [n]             — list pending drafts
  /cancel <outbox_id>     — cancel outbox item inside undo window

Inline button callbacks (Phase 2):
  draft:approve:<id>:<idx>  — send suggestion i
  draft:edit:<id>           — enter edit flow (reply with new text)
  draft:regen:<id>          — regenerate suggestions
  draft:reject:<id>         — discard draft
  draft:cancel:<id>         — undo after approval (within 30s window)
"""
from __future__ import annotations

import asyncio
from functools import wraps

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
from app.models import AuditLog, Base, Contact, Draft, DraftStatus, InboundMessage, MemoryKind
from app.services import audit, auth, draft_manager, memory
from app.services.quiet_hours import is_quiet_now

setup_logging()
settings = get_settings()


# ── Decorators ───────────────────────────────────────────────────────
def allowlist_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not auth.is_allowed(uid):
            log.warning("bot.denied", user_id=uid)
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


# ── Phase 1 commands ─────────────────────────────────────────────────
@allowlist_only
async def cmd_start(update: Update, _ctx):
    await update.message.reply_text(
        "🤖 Personal Assistant — Phase 2 (Drafting)\n"
        "Commands: /ping /whoami /note /notes /audit /inbox /quiet /drafts\n"
        "Destructive commands require /unlock <pin>."
    )


@allowlist_only
async def cmd_ping(update: Update, _ctx):
    await update.message.reply_text("pong ✅")


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


# ── Phase 2 commands ─────────────────────────────────────────────────
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


# ── Phase 2 inline button callbacks ──────────────────────────────────
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


# ── Edit-reply plain text handler ────────────────────────────────────
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
        await update.message.reply_text("Empty text — edit cancelled.")
        return
    msg = await draft_manager.edit_draft(draft_id, new_text, uid)
    await update.message.reply_text(msg)


# ── Bootstrap ────────────────────────────────────────────────────────
async def _ensure_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    if not settings.allowed_user_ids:
        raise SystemExit("TELEGRAM_ALLOWED_USER_IDS not set (get yours from @userinfobot)")

    asyncio.get_event_loop().run_until_complete(_ensure_schema())

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
    app.add_handler(CommandHandler("drafts",     cmd_drafts))
    app.add_handler(CommandHandler("cancel",     cmd_cancel_outbox))
    app.add_handler(CommandHandler("canceledit", cmd_canceledit))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^draft:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_reply))

    log.info("bot.starting", allowed=len(settings.allowed_user_ids))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
