from __future__ import annotations

import httpx

from app.config import get_settings


def _sidecar_base_url() -> str | None:
    url = (get_settings().whatsapp_group_sender_url or "").strip()
    if not url:
        return None
    if url.endswith("/send"):
        return url[: -len("/send")].rstrip("/")
    return url.rstrip("/")


def _auth_headers() -> dict[str, str]:
    token = (get_settings().whatsapp_group_sender_token or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def fetch_group_participant_ids(group_id: str) -> set[str]:
    base_url = _sidecar_base_url()
    if not base_url or not group_id:
        return set()

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{base_url}/participants",
            params={"group_id": group_id},
            headers=_auth_headers(),
        )

    if response.is_error:
        raise RuntimeError(
            f"Group sender participant lookup failed: HTTP {response.status_code} {response.text[:200]}"
        )

    data = response.json()
    participants = data.get("participants") or []
    if not isinstance(participants, list):
        return set()

    result: set[str] = set()
    for item in participants:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("id") or "").strip()
        if jid:
            result.add(jid)
    return result
