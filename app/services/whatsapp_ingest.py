"""Normalize WhatsApp Cloud API webhook payloads into local records."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, InboundMessage
from app.services.time_utils import as_utc_naive, utc_now_naive


@dataclass(frozen=True)
class NormalizedWhatsAppMessage:
    external_id: str
    from_external_id: str
    display_name: str | None
    message_type: str
    text: str | None
    received_at: datetime
    raw: dict
    group_id: str | None = None       # set if message came from a WA group
    group_name: str | None = None     # group display name if available


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
            # Extract group metadata if present
            metadata = value.get("metadata") or {}
            group_name = metadata.get("display_phone_number")  # fallback
            # Real group name comes from conversation subject (not always available)
            conv = value.get("conversation") or {}
            group_name = conv.get("origin", {}).get("type") or group_name

            for message in value.get("messages") or []:
                from_external_id = str(message.get("from") or "")
                if not from_external_id:
                    continue
                message_id = str(message.get("id") or "")
                if not message_id:
                    continue

                # Detect group: group messages have a "context" with group metadata
                # or the "to" field in value.metadata is a group ID
                raw_group_id: str | None = None
                raw_group_name: str | None = None
                msg_context = message.get("context") or {}
                # If metadata phone_number_id is a group, it ends with @g.us
                meta_phone = metadata.get("phone_number_id", "")
                if from_external_id.endswith("@g.us"):
                    raw_group_id = from_external_id
                elif msg_context.get("from", "").endswith("@g.us"):
                    raw_group_id = msg_context["from"]
                # Try to get group name from referral or metadata
                referral = message.get("referral") or {}
                raw_group_name = referral.get("source_id") or group_name

                results.append(
                    NormalizedWhatsAppMessage(
                        external_id=message_id,
                        from_external_id=from_external_id,
                        display_name=profile_by_wa_id.get(from_external_id),
                        message_type=str(message.get("type") or "unknown"),
                        text=_message_text(message),
                        received_at=_message_time(message.get("timestamp")),
                        raw=message,
                        group_id=raw_group_id,
                        group_name=raw_group_name,
                    )
                )
    return results


def extract_sidecar_messages(payload: dict) -> list[NormalizedWhatsAppMessage]:
    """
    Normalize messages forwarded from the WhatsApp group sidecar.

    Expected payload shape:
      {
        "messages": [
          {
            "external_id": "...",
            "from_external_id": "...",
            "display_name": "...",
            "message_type": "text" | "event_response",
            "text": "...",
            "received_at": "2026-06-09T09:21:00Z",
            "group_id": "120...@g.us",
            "group_name": "CHCC Members - T20 Cricket",
            "raw": {...}
          }
        ]
      }
    """
    results: list[NormalizedWhatsAppMessage] = []
    for item in payload.get("messages") or []:
        group_id = str(item.get("group_id") or "").strip() or None
        if not group_id:
            continue

        external_id = str(item.get("external_id") or item.get("id") or "").strip()
        if not external_id:
            continue

        from_external_id = str(
            item.get("from_external_id")
            or item.get("participant")
            or item.get("sender_jid")
            or item.get("from")
            or ""
        ).strip()
        if not from_external_id:
            continue

        received_at = _parse_received_at(item.get("received_at"))
        message_type = str(item.get("message_type") or "unknown").strip() or "unknown"
        text = item.get("text")
        if text is not None:
            text = str(text)
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        event_response = item.get("event_response") or raw.get("event_response")
        participant_aliases = (
            item.get("participant_aliases")
            or item.get("aliases")
            or raw.get("participant_aliases")
            or []
        )

        results.append(
            NormalizedWhatsAppMessage(
                external_id=external_id,
                from_external_id=from_external_id,
                display_name=str(item.get("display_name")).strip() if item.get("display_name") else None,
                message_type=message_type,
                text=text,
                received_at=received_at,
                raw=raw | {
                    "group_id": group_id,
                    "group_name": item.get("group_name"),
                    "source": "sidecar",
                    "event_response": event_response,
                    "participant_aliases": participant_aliases,
                },
                group_id=group_id,
                group_name=str(item.get("group_name")).strip() if item.get("group_name") else None,
            )
        )
    return results


async def persist_message(
    session: AsyncSession,
    message: NormalizedWhatsAppMessage,
) -> tuple[Contact, InboundMessage | None]:
    from app.services.group_registry import auto_register_group

    contact = await _upsert_contact(session, message)

    # Auto-register group if this is a group message
    if message.group_id:
        await auto_register_group(
            session,
            raw_message=message.raw,
            group_name=message.group_name,
        )

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
        received_at=as_utc_naive(message.received_at),
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
        contact.last_inbound_at = as_utc_naive(message.received_at)
        await session.flush()
        return contact

    contact = Contact(
        external_id=message.from_external_id,
        display_name=message.display_name,
        last_inbound_at=as_utc_naive(message.received_at),
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
        return as_utc_naive(datetime.fromtimestamp(int(str(timestamp)), tz=timezone.utc))
    except (TypeError, ValueError, OSError):
        return utc_now_naive()


def _parse_received_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return as_utc_naive(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return as_utc_naive(datetime.fromisoformat(text))
        except ValueError:
            pass
    return utc_now_naive()
