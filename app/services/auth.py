"""Telegram auth: allowlist + PIN sessions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import PinSession

settings = get_settings()


def is_allowed(user_id: int) -> bool:
    return user_id in settings.allowed_user_ids


async def pin_unlock(session: AsyncSession, user_id: int, pin: str) -> bool:
    if not settings.admin_pin or pin != settings.admin_pin:
        return False
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.pin_session_minutes)
    stmt = select(PinSession).where(PinSession.user_id == user_id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        existing.expires_at = expires
    else:
        session.add(PinSession(user_id=user_id, expires_at=expires))
    return True


async def pin_valid(session: AsyncSession, user_id: int) -> bool:
    stmt = select(PinSession).where(PinSession.user_id == user_id)
    s = (await session.execute(stmt)).scalar_one_or_none()
    if not s:
        return False
    # naive vs aware compare safety
    exp = s.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


async def pin_lock(session: AsyncSession, user_id: int) -> None:
    stmt = select(PinSession).where(PinSession.user_id == user_id)
    s = (await session.execute(stmt)).scalar_one_or_none()
    if s:
        await session.delete(s)
