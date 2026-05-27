"""Idempotency helpers for inbound webhooks and outbound sends."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProcessedEvent


def payload_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def claim_event(session: AsyncSession, *, source: str, external_id: str) -> bool:
    stmt = select(ProcessedEvent).where(
        ProcessedEvent.source == source,
        ProcessedEvent.external_id == external_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        return False

    try:
        async with session.begin_nested():
            session.add(ProcessedEvent(source=source, external_id=external_id))
            await session.flush()
    except IntegrityError:
        return False
    return True
