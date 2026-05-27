"""Monthly cost cap enforcement. Call before every paid API request."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CostLedger


class CostCapExceeded(Exception):
    pass


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_spend(session: AsyncSession, provider: str) -> float:
    stmt = select(func.coalesce(func.sum(CostLedger.usd), 0.0)).where(
        CostLedger.provider == provider, CostLedger.ts >= _month_start_utc()
    )
    return float((await session.execute(stmt)).scalar_one())


def _cap(provider: str) -> float:
    s = get_settings()
    return {
        "anthropic":  s.cost_cap_anthropic,
        "openai":     s.cost_cap_openai,
        "elevenlabs": s.cost_cap_elevenlabs,
        "gemini":     s.cost_cap_gemini,
        "groq":       s.cost_cap_groq,
    }.get(provider, 0.0)


async def check(session: AsyncSession, provider: str, projected_usd: float = 0.0) -> None:
    spent = await month_spend(session, provider)
    if spent + projected_usd > _cap(provider):
        raise CostCapExceeded(
            f"{provider} cap exceeded: spent ${spent:.2f} + projected ${projected_usd:.2f} "
            f"> cap ${_cap(provider):.2f}"
        )


async def record(
    session: AsyncSession,
    *,
    provider: str,
    units: float,
    usd: float,
    meta: dict | None = None,
) -> None:
    session.add(CostLedger(provider=provider, units=units, usd=usd, meta=meta))
