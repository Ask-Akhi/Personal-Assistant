"""Normalize WhatsApp Cloud API webhook payloads into local records."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, InboundMessage


@dataclass(frozen=True)
class NormalizedWhatsAppMessage:
    external_id: str
    from_external_id: str
    display_name: str | None
    message_type: str
    text: str | None
    received_at: datetime
    raw: dict


def signature_valid(*, body: bytes, signature_header: str | None, app_secret: str) -> bool:
    if not app_secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def extract_messages(payload: dict) -> list[NormalizedWhatsAppMessage]:
    results: list[NormalizedWhatsAppMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            profile_by_wa_id = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                from_external_id = str(message.get("from") or "")
                if not from_external_id:
                    continue
                message_id = str(message.get("id") or "")
                if not message_id:
                    continue
                results.append(
                    NormalizedWhatsAppMessage(
                        external_id=message_id,
                        from_external_id=from_external_id,
                        display_name=profile_by_wa_id.get(from_external_id),
                        message_type=str(message.get("type") or "unknown"),
                        text=_message_text(message),
                        received_at=_message_time(message.get("timestamp")),
                        raw=message,
                    )
                )
    return results


async def persist_message(
    session: AsyncSession,
    message: NormalizedWhatsAppMessage,
) -> tuple[Contact, InboundMessage | None]:
    contact = await _upsert_contact(session, message)
    existing = (
        await session.execute(
            select(InboundMessage).where(
                InboundMessage.channel == "wa_cloud",
                InboundMessage.external_id == message.external_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return contact, None

    inbound = InboundMessage(
        channel="wa_cloud",
        external_id=message.external_id,
        contact_id=contact.id,
        from_external_id=message.from_external_id,
        message_type=message.message_type,
        text=message.text,
        raw=message.raw,
        received_at=message.received_at,
    )
    session.add(inbound)
    await session.flush()
    return contact, inbound


async def _upsert_contact(
    session: AsyncSession,
    message: NormalizedWhatsAppMessage,
) -> Contact:
    contact = (
        await session.execute(
            select(Contact).where(Contact.external_id == message.from_external_id)
        )
    ).scalar_one_or_none()
    if contact:
        if message.display_name:
            contact.display_name = message.display_name
        contact.last_inbound_at = message.received_at
        await session.flush()
        return contact

    contact = Contact(
        external_id=message.from_external_id,
        display_name=message.display_name,
        last_inbound_at=message.received_at,
    )
    session.add(contact)
    await session.flush()
    return contact


def _message_text(message: dict) -> str | None:
    message_type = message.get("type")
    if message_type == "text":
        return (message.get("text") or {}).get("body")
    if message_type == "button":
        return (message.get("button") or {}).get("text")
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        button_reply = interactive.get("button_reply") or {}
        list_reply = interactive.get("list_reply") or {}
        return button_reply.get("title") or list_reply.get("title")
    return None


def _message_time(timestamp: object) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(timestamp)), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)
