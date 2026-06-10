from app.models import Contact, InboundMessage
from app.services.event_manager import _is_real_rsvp_contact, _rsvp_inbound_group_id


def test_real_rsvp_contact_accepts_person_jid():
    contact = Contact(external_id="61411111111@s.whatsapp.net", display_name="Asha")
    assert _is_real_rsvp_contact(contact) is True


def test_real_rsvp_contact_accepts_bare_phone_number():
    contact = Contact(external_id="61411111111", display_name="Asha")
    assert _is_real_rsvp_contact(contact) is True


def test_real_rsvp_contact_rejects_plain_username():
    contact = Contact(external_id="ptebydrahmed", display_name="ptebydrahmed")
    assert _is_real_rsvp_contact(contact) is False


def test_real_rsvp_contact_rejects_group_jid():
    contact = Contact(external_id="120363408907704792@g.us", display_name="CHCC Members")
    assert _is_real_rsvp_contact(contact) is False


def test_real_rsvp_contact_rejects_broadcast_jid():
    contact = Contact(external_id="120363408907704792@broadcast", display_name="Broadcast")
    assert _is_real_rsvp_contact(contact) is False


def test_rsvp_inbound_group_id_reads_sidecar_group():
    inbound = InboundMessage(raw={"group_id": "120363408907704792@g.us"})

    assert _rsvp_inbound_group_id(inbound) == "120363408907704792@g.us"


def test_rsvp_inbound_group_id_rejects_missing_raw_group():
    inbound = InboundMessage(raw={"source": "sidecar"})

    assert _rsvp_inbound_group_id(inbound) is None
