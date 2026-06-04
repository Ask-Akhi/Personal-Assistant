"""WhatsApp Cloud API — outbound message sender.

Only call after confirming service_window_open() — free-form text messages
can only be sent within 24 h of the last inbound message from that contact.
Outside that window, use approved templates (not implemented yet).
"""
from __future__ import annotations

import re
import asyncio

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.logging_setup import log

WA_API_BASE = "https://graph.facebook.com/v19.0"


class WhatsAppSendError(RuntimeError):
    pass


class TemporaryGroupSenderError(WhatsAppSendError):
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
    if to.endswith("@g.us"):
        return await _send_group_text(to, text, settings)
    if not settings.whatsapp_cloud_token or not settings.whatsapp_phone_number_id:
        raise RuntimeError("WHATSAPP_CLOUD_TOKEN or WHATSAPP_PHONE_NUMBER_ID not set")
    if not re.fullmatch(r"\+?\d{8,15}", to):
        raise WhatsAppSendError(
            f"Invalid WhatsApp Cloud API recipient '{to}'. Use an E.164 phone number "
            "without spaces, not another Telegram command."
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


async def _send_group_text(to: str, text: str, settings) -> str:
    if not settings.whatsapp_group_sender_url:
        raise WhatsAppSendError(
            "Automatic WhatsApp group posting is not configured yet. Set "
            "WHATSAPP_GROUP_SENDER_URL to a group-capable sender service."
        )

    await _wake_group_sender(settings)

    headers = {"Content-Type": "application/json"}
    if settings.whatsapp_group_sender_token:
        headers["Authorization"] = f"Bearer {settings.whatsapp_group_sender_token}"

    payload = {"group_id": to, "text": text}
    response = None
    last_error = None
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.post(
                    settings.whatsapp_group_sender_url.rstrip("/"),
                    json=payload,
                    headers=headers,
                )
        except httpx.TransportError as exc:
            last_error = exc
            if attempt < 3:
                await asyncio.sleep(4 * (attempt + 1))
                continue
            raise TemporaryGroupSenderError(
                f"WhatsApp group sender is unreachable: {exc}"
            ) from exc

        if response.status_code in {502, 503, 504}:
            last_error = response
            if attempt < 3:
                await asyncio.sleep(6 * (attempt + 1))
                continue
            break
        break

    if response is None and last_error:
        raise TemporaryGroupSenderError(str(last_error))

    if response.is_error:
        error_detail = _extract_error_detail(response)
        log.error(
            "wa_sender.group_error",
            status=response.status_code,
            body=error_detail[:500],
            to=to,
        )
        if response.status_code in {502, 503, 504}:
            raise TemporaryGroupSenderError(
                "WhatsApp group sender is waking up or unavailable on Render. "
                f"HTTP {response.status_code} - {error_detail[:500]}"
            )
        raise WhatsAppSendError(
            f"WhatsApp group sender rejected send to {to}: HTTP {response.status_code} - "
            f"{error_detail[:700]}"
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    message_id = str(
        data.get("message_id")
        or data.get("id")
        or data.get("wa_message_id")
        or "group-send-ok"
    )
    log.info("wa_sender.group_sent", to=to, wa_message_id=message_id)
    return message_id


async def _wake_group_sender(settings) -> None:
    base_url = settings.whatsapp_group_sender_url.rstrip("/")
    if base_url.endswith("/send"):
        base_url = base_url[: -len("/send")]
    health_url = f"{base_url}/healthz"

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(health_url)
            if response.is_success:
                try:
                    payload = response.json()
                except ValueError:
                    return
                if payload.get("connected") is False:
                    raise WhatsAppSendError(
                        "WhatsApp group sender is running but not connected. "
                        "Open the sidecar URL, scan the QR, and retry."
                    )
                return
            if response.status_code not in {502, 503, 504}:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(5 * (attempt + 1))


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
