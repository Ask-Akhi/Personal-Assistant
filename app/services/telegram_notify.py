"""Small Telegram notification helper used by background/API processes."""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.logging_setup import log


async def send_admin_message(text: str) -> None:
    settings = get_settings()
    if not settings.telegram_mirror_enabled:
        return
    if not settings.telegram_bot_token or not settings.allowed_user_ids:
        log.warning("telegram_notify.skipped", reason="missing_token_or_allowlist")
        return

    async with httpx.AsyncClient(timeout=10) as client:
        for user_id in settings.allowed_user_ids:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": user_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.is_error:
                log.warning(
                    "telegram_notify.failed",
                    user_id=user_id,
                    status_code=response.status_code,
                    body=response.text[:500],
                )
