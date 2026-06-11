from app.services.wa_groups import participant_ids_from_payload
from app.services.group_registry import _extract_group_id


def test_participant_ids_from_payload_includes_aliases():
    payload = {
        "participants": [
            {
                "id": "111111111111111@lid",
                "aliases": [
                    "111111111111111@lid",
                    "61411111111@s.whatsapp.net",
                ],
            }
        ]
    }

    ids = participant_ids_from_payload(payload)

    assert "111111111111111@lid" in ids
    assert "61411111111@s.whatsapp.net" in ids


def test_participant_ids_from_payload_expands_bare_phone_numbers():
    ids = participant_ids_from_payload({"participants": [{"id": "61411111111"}]})

    assert "61411111111" in ids
    assert "61411111111@s.whatsapp.net" in ids


def test_group_registry_extracts_sidecar_group_id():
    raw = {
        "group_id": "120363408907704792@g.us",
        "remote_jid": "120363408907704792@g.us",
    }

    assert _extract_group_id(raw) == "120363408907704792@g.us"
