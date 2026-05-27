"""Structured memory API. Every entry is editable + explainable."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Memory, MemoryKind


async def upsert(
    session: AsyncSession,
    *,
    kind: MemoryKind,
    subject: str,
    key: str,
    value: str,
) -> Memory:
    stmt = select(Memory).where(
        Memory.kind == kind, Memory.subject == subject, Memory.key == key
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        existing.value = value
        return existing
    m = Memory(kind=kind, subject=subject, key=key, value=value)
    session.add(m)
    return m


async def list_for(
    session: AsyncSession, *, subject: str | None = None, kind: MemoryKind | None = None
) -> list[Memory]:
    stmt = select(Memory)
    if subject:
        stmt = stmt.where(Memory.subject == subject)
    if kind:
        stmt = stmt.where(Memory.kind == kind)
    stmt = stmt.order_by(Memory.updated_at.desc()).limit(100)
    return list((await session.execute(stmt)).scalars().all())


async def delete(session: AsyncSession, memory_id: int) -> bool:
    m = await session.get(Memory, memory_id)
    if not m:
        return False
    await session.delete(m)
    return True
