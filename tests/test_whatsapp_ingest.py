import hashlib
import hmac

from app.services.whatsapp_ingest import extract_messages, extract_sidecar_messages, signature_valid


def _payload():
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {
                                    "wa_id": "61400000000",
                                    "profile": {"name": "Asha"},
                                }
                            ],
                            "messages": [
                                {
                                    "from": "61400000000",
                                    "id": "wamid.123",
                                    "timestamp": "1779415200",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def test_extract_messages_normalizes_text_payload():
    messages = extract_messages(_payload())
    assert len(messages) == 1
    assert messages[0].external_id == "wamid.123"
    assert messages[0].from_external_id == "61400000000"
    assert messages[0].display_name == "Asha"
    assert messages[0].message_type == "text"
    assert messages[0].text == "hello"


def test_signature_validates_meta_header():
    secret = "app-secret"
    body = b'{"hello":"world"}'
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert signature_valid(
        body=body,
        signature_header=f"sha256={digest}",
        app_secret=secret,
    )


def test_signature_rejects_bad_header():
    assert not signature_valid(
        body=b"{}",
        signature_header="sha256=bad",
        app_secret="app-secret",
    )


def test_extract_sidecar_messages_normalizes_group_event_response():
    payload = {
        "messages": [
            {
                "external_id": "ABC123",
                "from_external_id": "61411111111@s.whatsapp.net",
                "display_name": "Asha",
                "message_type": "event_response",
                "text": "going",
                "received_at": "2026-06-09T09:21:00Z",
                "group_id": "120363408907704792@g.us",
                "group_name": "CHCC Members - T20 Cricket",
                "raw": {"event_response": {"response": 1, "extra_guest_count": 0}},
            }
        ]
    }

    messages = extract_sidecar_messages(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.external_id == "ABC123"
    assert msg.from_external_id == "61411111111@s.whatsapp.net"
    assert msg.display_name == "Asha"
    assert msg.message_type == "event_response"
    assert msg.text == "going"
    assert msg.group_id == "120363408907704792@g.us"
    assert msg.group_name == "CHCC Members - T20 Cricket"
    assert msg.raw["group_id"] == "120363408907704792@g.us"


def test_extract_sidecar_messages_preserves_snapshot_event_response():
    payload = {
        "messages": [
            {
                "external_id": "event-card:61411111111@s.whatsapp.net",
                "from_external_id": "61411111111@s.whatsapp.net",
                "message_type": "event_response",
                "text": "going",
                "received_at": "2026-06-09T09:21:00Z",
                "group_id": "120363408907704792@g.us",
                "event_response": {
                    "response": 1,
                    "normalized_response": "going",
                    "event_message_id": "event-card",
                },
                "raw": {
                    "event_message_id": "event-card",
                    "participant": "61411111111@s.whatsapp.net",
                },
            }
        ]
    }

    messages = extract_sidecar_messages(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.message_type == "event_response"
    assert msg.text == "going"
    assert msg.raw["event_response"]["response"] == 1
    assert msg.raw["event_response"]["event_message_id"] == "event-card"
