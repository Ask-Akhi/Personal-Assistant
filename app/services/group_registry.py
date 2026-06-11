# -*- coding: utf-8 -*-
"""WhatsApp Group Registry — auto-discovers and smart-matches groups.

PI learns group IDs automatically from the first message it receives
from each group. No manual WHATSAPP_GROUP_ID config needed.

Groups are matched to tasks by keywords in their name:
  - "cricket" / "chcc" / "t20"  -> cricket weekly event task
  - (future: "football", "running", etc.)
"""
from __future__ import annotations

import re
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import log
from app.models import Memory, MemoryKind


# Keyword -> task mapping
_GROUP_TASK_MAP = {
    "cricket":  "weekly_cricket",
    "chcc":     "weekly_cricket",
    "t20":      "weekly_cricket",
    "c.h.c.c":  "weekly_cricket",
    "castle hill": "weekly_cricket",
    # Add more here as you create new recurring tasks
    # "football": "weekly_football",
    # "running":  "weekly_run",
}


def _infer_task(group_name: str) -> str | None:
    """Return the task key for a group name based on keywords."""
    lower = group_name.lower()
    for keyword, task in _GROUP_TASK_MAP.items():
        if keyword in lower:
            return task
    return None


def _extract_group_id(raw: dict) -> str | None:
    """Extract WA group ID from raw message payload if it's a group message."""
    for key in ("group_id", "remote_jid", "remoteJid"):
        value = str(raw.get(key) or "").strip()
        if value.endswith("@g.us"):
            return value

    # Group messages: "from" is the sender's number,
    # but the group ID appears in metadata or context
    # In Cloud API, group messages have a "context" or the "to" field is the group ID
    # Actually the group ID is in value.metadata.display_phone_number is the bot's number
    # For group messages, the group ID is in the raw "chat" field or we get it from
    # the webhook value.contacts[].wa_id matching the group format XXXXXXXXXX@g.us

    # Check if the sender itself is a group (ends with @g.us)
    from_id = str(raw.get("from") or "")
    if from_id.endswith("@g.us"):
        return from_id

    # Check context for group ID
    context = raw.get("context") or {}
    if context.get("from", "").endswith("@g.us"):
        return context["from"]

    return None


async def auto_register_group(
    session: AsyncSession,
    raw_message: dict,
    group_name: str | None = None,
) -> str | None:
    """
    Called for every inbound message.
    If message is from a group, store the group ID + infer its task.
    Returns the group_id if it's a group message, else None.
    """
    group_id = _extract_group_id(raw_message)
    if not group_id:
        return None

    # Check if already registered
    existing = (await session.execute(
        select(Memory).where(
            Memory.kind == MemoryKind.assistant_rule,
            Memory.subject == "wa_group",
            Memory.key == group_id,
        )
    )).scalar_one_or_none()

    task = _infer_task(group_name or "") if group_name else None
    value = group_name or group_id

    if existing:
        stored_task = existing.value.rsplit(" | task:", 1)[1].strip() if " | task:" in existing.value else None
        name_changed = group_name and group_name not in existing.value
        task_improved = task and stored_task != task
        if name_changed or task_improved:
            base_name = group_name or existing.value.split(" | task:")[0].strip()
            existing.value = f"{base_name} | task:{task or stored_task or 'unknown'}"
            await session.flush()
        return group_id

    # New group — register it
    entry = Memory(
        kind=MemoryKind.assistant_rule,
        subject="wa_group",
        key=group_id,
        value=f"{value} | task:{task or 'unknown'}",
    )
    session.add(entry)
    await session.flush()

    log.info(
        "group_registry.registered",
        group_id=group_id,
        name=group_name,
        task=task,
    )

    # Notify via Telegram
    await _notify_new_group(group_id, group_name, task)
    return group_id


async def get_group_for_task(session: AsyncSession, task: str) -> str | None:
    """Return the WA group ID registered for a given task key."""
    rows = (await session.execute(
        select(Memory).where(
            Memory.kind == MemoryKind.assistant_rule,
            Memory.subject == "wa_group",
        ).order_by(Memory.updated_at.desc(), Memory.created_at.desc())
    )).scalars().all()

    for row in rows:
        if f"task:{task}" in row.value:
            return row.key  # key is the group_id

    return None


async def get_task_for_group(session: AsyncSession, group_id: str) -> str | None:
    """Return the task key registered for a WA group, if known."""
    if not group_id:
        return None
    row = (await session.execute(
        select(Memory).where(
            Memory.kind == MemoryKind.assistant_rule,
            Memory.subject == "wa_group",
            Memory.key == group_id,
        )
    )).scalar_one_or_none()
    if not row or " | task:" not in row.value:
        return None
    return row.value.rsplit(" | task:", 1)[1].strip() or None


async def list_groups(session: AsyncSession) -> list[dict]:
    """Return all registered groups."""
    rows = (await session.execute(
        select(Memory).where(
            Memory.kind == MemoryKind.assistant_rule,
            Memory.subject == "wa_group",
        ).order_by(Memory.created_at)
    )).scalars().all()

    result = []
    for row in rows:
        parts = row.value.split(" | task:")
        name = parts[0].strip()
        task = parts[1].strip() if len(parts) > 1 else "unknown"
        result.append({"group_id": row.key, "name": name, "task": task})
    return result


async def _notify_new_group(
    group_id: str,
    group_name: str | None,
    task: str | None,
) -> None:
    """Notify Telegram when a new group is auto-discovered."""
    from app.services.telegram_notify import send_admin_message
    task_line = f"Auto-matched task: <b>{task}</b>" if task else "No task matched yet."
    await send_admin_message(
        f"<b>New WA group discovered!</b>\n"
        f"Name: <b>{group_name or 'Unknown'}</b>\n"
        f"ID: <code>{group_id}</code>\n"
        f"{task_line}\n\n"
        f"Use /groups to see all registered groups."
    )
