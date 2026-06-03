"""WhatsApp Cloud API — outbound message sender.

Only call after confirming service_window_open() — free-form text messages
can only be sent within 24 h of the last inbound message from that contact.
Outside that window, use approved templates (not implemented yet).
"""
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logging_setup import log

WA_API_BASE = "https://graph.facebook.com/v19.0"


class WhatsAppSendError(RuntimeError):
    pass


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def send_text(to: str, text: str) -> str:
    """Send a free-form text message.  Returns the WA message id."""
    settings = get_settings()
    if not settings.whatsapp_cloud_token or not settings.whatsapp_phone_number_id:
        raise RuntimeError("WHATSAPP_CLOUD_TOKEN or WHATSAPP_PHONE_NUMBER_ID not set")
    if to.endswith("@g.us"):
        raise WhatsAppSendError(
            "WhatsApp Cloud API Graph /messages cannot send to group IDs (@g.us). "
            "Use the manual copy text, or wire a WhatsApp Web/Baileys-style group sender."
        )

    url = f"{WA_API_BASE}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text, "preview_url": False},
    }
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_cloud_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json=payload, headers=headers)

    if response.is_error:
        error_detail = _extract_error_detail(response)
        log.error(
            "wa_sender.error",
            status=response.status_code,
            body=error_detail[:500],
            to=to,
        )
        raise WhatsAppSendError(
            f"WhatsApp API rejected send to {to}: HTTP {response.status_code} - "
            f"{error_detail[:700]}"
        )

    data = response.json()
    message_id: str = (
        (data.get("messages") or [{}])[0].get("id") or ""
    )
    log.info("wa_sender.sent", to=to, wa_message_id=message_id)
    return message_id


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return str(data)
    parts = [
        str(error.get("message") or "").strip(),
        str(error.get("type") or "").strip(),
        f"code={error.get('code')}" if error.get("code") is not None else "",
        f"subcode={error.get('error_subcode')}" if error.get("error_subcode") is not None else "",
        str(error.get("error_data") or "").strip(),
    ]
    return " | ".join(part for part in parts if part)
