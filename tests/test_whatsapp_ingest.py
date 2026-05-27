import hashlib
import hmac

from app.services.whatsapp_ingest import extract_messages, signature_valid


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
