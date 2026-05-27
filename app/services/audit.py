"""Audit log helper. Every sensitive action goes here."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    reasons: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            target=target,
            reasons=reasons,
            payload=payload,
        )
    )
