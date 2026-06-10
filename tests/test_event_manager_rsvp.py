from app.models import Contact
from app.services.event_manager import _is_real_rsvp_contact


def test_real_rsvp_contact_accepts_person_jid():
    contact = Contact(external_id="61411111111@s.whatsapp.net", display_name="Asha")
    assert _is_real_rsvp_contact(contact) is True


def test_real_rsvp_contact_rejects_group_jid():
    contact = Contact(external_id="120363408907704792@g.us", display_name="CHCC Members")
    assert _is_real_rsvp_contact(contact) is False


def test_real_rsvp_contact_rejects_broadcast_jid():
    contact = Contact(external_id="120363408907704792@broadcast", display_name="Broadcast")
    assert _is_real_rsvp_contact(contact) is False
