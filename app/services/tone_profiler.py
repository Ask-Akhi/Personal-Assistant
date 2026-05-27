"""Tone profiler — updates a contact's tone_profile after each detected message.

Pure DB update, no LLM call. Merges the latest language detection result
into the existing profile using a simple rolling update:
  - language / script: replace with latest (most recent message wins)
  - formality: update only if confidence > 0.7
  - emoji_usage: rolling average of last 10 messages (stored in profile)
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact
from app.services.language_detector import LanguageResult


async def update(
    session: AsyncSession,
    contact: Contact,
    result: LanguageResult,
) -> None:
    """Merge language detection result into contact.tone_profile."""
    existing: dict = contact.tone_profile or {}

    emoji_history: list[float] = existing.get("emoji_history", [])
    emoji_history.append(result.emoji_density)
    emoji_history = emoji_history[-10:]  # keep last 10
    avg_emoji = sum(emoji_history) / len(emoji_history)

    emoji_usage = "high" if avg_emoji > 0.05 else ("medium" if avg_emoji > 0.01 else "low")

    updated: dict = {
        **existing,
        "language": result.lang,
        "script": result.script,
        "emoji_usage": emoji_usage,
        "emoji_history": emoji_history,
    }

    # Only update formality if we have decent confidence
    if result.formality != "unknown" and result.confidence >= 0.7:
        updated["formality"] = result.formality
    elif "formality" not in updated:
        updated["formality"] = "unknown"

    contact.tone_profile = updated
    contact.language_pref = result.lang
    await session.flush()
