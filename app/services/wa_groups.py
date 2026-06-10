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


def _add_participant_id(result: set[str], value: object) -> None:
    jid = str(value or "").strip()
    if not jid:
        return
    result.add(jid)

    if jid.isdigit() and len(jid) >= 8:
        result.add(f"{jid}@s.whatsapp.net")


def participant_ids_from_payload(data: dict) -> set[str]:
    participants = data.get("participants") or []
    if not isinstance(participants, list):
        return set()

    result: set[str] = set()
    for item in participants:
        if not isinstance(item, dict):
            continue
        for key in (
            "id",
            "jid",
            "lid",
            "participant",
            "participantPn",
            "participantLid",
        ):
            _add_participant_id(result, item.get(key))

        aliases = item.get("aliases") or []
        if isinstance(aliases, list):
            for alias in aliases:
                _add_participant_id(result, alias)

    return result


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
    return participant_ids_from_payload(data)
