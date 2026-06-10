from app.services.wa_groups import participant_ids_from_payload


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
