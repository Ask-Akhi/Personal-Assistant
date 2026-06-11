from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Contact, CricketEvent, EventRsvp, EventStatus, InboundMessage, Memory, MemoryKind, RsvpStatus
import app.services.event_manager as event_manager
from app.services.event_manager import (
    _is_real_rsvp_contact,
    _rsvp_inbound_group_id,
    backfill_event_rsvps_from_inbounds,
    get_group_filtered_rsvps,
    handle_possible_rsvp,
)


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


def test_rsvp_inbound_group_id_accepts_remote_jid_group():
    inbound = InboundMessage(raw={"remote_jid": "120363408907704792@g.us"})

    assert _rsvp_inbound_group_id(inbound) == "120363408907704792@g.us"


@pytest.mark.asyncio
async def test_get_group_filtered_rsvps_keeps_event_scoped_row_without_group_marker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
        )
        contact = Contact(
            external_id="61411111111@s.whatsapp.net",
            display_name="Asha",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="manual",
            external_id="manual-1",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="text",
            text="yes",
            raw={"source": "manual"},
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        session.add(
            EventRsvp(
                event_id=event.id,
                contact_id=contact.id,
                inbound_id=inbound.id,
                status=RsvpStatus.yes,
                raw_text="yes",
            )
        )
        await session.commit()

        rows = await get_group_filtered_rsvps(session, event)

        assert len(rows) == 1
        assert rows[0][1].external_id == "61411111111@s.whatsapp.net"

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_group_filtered_rsvps_excludes_explicit_wrong_group(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
        )
        contact = Contact(
            external_id="61411111111@s.whatsapp.net",
            display_name="Asha",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-1",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="text",
            text="yes",
            raw={"group_id": "999999999999999999@g.us"},
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        session.add(
            EventRsvp(
                event_id=event.id,
                contact_id=contact.id,
                inbound_id=inbound.id,
                status=RsvpStatus.yes,
                raw_text="yes",
            )
        )
        await session.commit()

        rows = await get_group_filtered_rsvps(session, event)

        assert rows == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_group_filtered_rsvps_excludes_explicit_wrong_remote_jid(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
        )
        contact = Contact(
            external_id="61411111111@s.whatsapp.net",
            display_name="Asha",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-remote-wrong",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="text",
            text="yes",
            raw={"remote_jid": "999999999999999999@g.us"},
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        session.add(
            EventRsvp(
                event_id=event.id,
                contact_id=contact.id,
                inbound_id=inbound.id,
                status=RsvpStatus.yes,
                raw_text="yes",
            )
        )
        await session.commit()

        rows = await get_group_filtered_rsvps(session, event)

        assert rows == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_group_filtered_rsvps_keeps_group_vote_despite_participant_alias_mismatch(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
        )
        contact = Contact(
            external_id="111437993250983@lid",
            display_name="ptebydrahmed",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-non-member",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="event_response",
            text="going",
            raw={
                "group_id": "120363408907704792@g.us",
                "participant_aliases": ["111437993250983@lid"],
                "event_response": {"response": 1},
            },
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        session.add(
            EventRsvp(
                event_id=event.id,
                contact_id=contact.id,
                inbound_id=inbound.id,
                status=RsvpStatus.yes,
                raw_text="going",
            )
        )
        await session.commit()

        rows = await get_group_filtered_rsvps(session, event)

        assert len(rows) == 1
        assert rows[0][1].external_id == "111437993250983@lid"

    await engine.dispose()


@pytest.mark.asyncio
async def test_handle_possible_rsvp_rejects_wrong_group_vote(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
        )
        contact = Contact(
            external_id="61411111111@s.whatsapp.net",
            display_name="Asha",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-2",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="text",
            text="yes",
            raw={"group_id": "999999999999999999@g.us"},
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        handled = await handle_possible_rsvp(session, contact, inbound)
        rows = await session.execute(EventRsvp.__table__.select())

        assert handled is False
        assert rows.all() == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_handle_possible_rsvp_accepts_matching_group_despite_participant_alias_mismatch(monkeypatch):
    async def fake_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(event_manager, "_notify_rsvp", fake_notify)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
        )
        contact = Contact(
            external_id="111437993250983@lid",
            display_name="ptebydrahmed",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-non-member-2",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="event_response",
            text="going",
            raw={
                "group_id": "120363408907704792@g.us",
                "participant_aliases": ["111437993250983@lid"],
                "event_response": {"response": 1},
            },
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        handled = await handle_possible_rsvp(session, contact, inbound)
        rows = await session.execute(EventRsvp.__table__.select())

        assert handled is True
        assert len(rows.all()) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_handle_possible_rsvp_accepts_event_card_result_for_bound_group_even_if_card_id_differs(monkeypatch):
    async def fake_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(event_manager, "_notify_rsvp", fake_notify)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
            announcement_wa_msg_id="plain-text-announcement-id",
        )
        contact = Contact(
            external_id="61411111111@s.whatsapp.net",
            display_name="Asha",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="event-response:wa-event-card-id:61411111111@s.whatsapp.net:going:1780000000000",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="event_response",
            text="going",
            raw={
                "group_id": "120363408907704792@g.us",
                "event_message_id": "wa-event-card-id",
                "event_response": {"response": 1, "event_message_id": "wa-event-card-id"},
            },
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        handled = await handle_possible_rsvp(session, contact, inbound)
        rows = await session.execute(EventRsvp.__table__.select())

        assert handled is True
        assert len(rows.all()) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_handle_possible_rsvp_rebinds_registered_weekly_cricket_group(monkeypatch):
    async def fake_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(event_manager, "_notify_rsvp", fake_notify)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="old-wrong-group@g.us",
        )
        contact = Contact(
            external_id="61411111111@s.whatsapp.net",
            display_name="Asha",
        )
        group_memory = Memory(
            kind=MemoryKind.assistant_rule,
            subject="wa_group",
            key="120363408907704792@g.us",
            value="CHCC Members | task:weekly_cricket",
        )
        session.add_all([event, contact, group_memory])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-rebind",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="text",
            text="yes",
            raw={"group_id": "120363408907704792@g.us"},
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        handled = await handle_possible_rsvp(session, contact, inbound)
        rows = await session.execute(EventRsvp.__table__.select())

        assert handled is True
        assert event.group_wa_id == "120363408907704792@g.us"
        assert len(rows.all()) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_event_rsvps_recovers_stored_group_votes():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id=None,
            created_at=datetime(2026, 6, 9, 8, 0, 0),
        )
        contact = Contact(
            external_id="61411111111@s.whatsapp.net",
            display_name="Asha",
        )
        group_memory = Memory(
            kind=MemoryKind.assistant_rule,
            subject="wa_group",
            key="120363408907704792@g.us",
            value="CHCC Members | task:weekly_cricket",
        )
        session.add_all([event, contact, group_memory])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-backfill",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="text",
            text="yes",
            raw={"group_id": "120363408907704792@g.us"},
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        repaired = await backfill_event_rsvps_from_inbounds(session, event)
        rows = await get_group_filtered_rsvps(session, event)

        assert repaired == 1
        assert event.group_wa_id == "120363408907704792@g.us"
        assert len(rows) == 1
        assert rows[0][0].status == RsvpStatus.yes

    await engine.dispose()


@pytest.mark.asyncio
async def test_handle_possible_rsvp_accepts_group_member_alias(monkeypatch):
    async def fake_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(event_manager, "_notify_rsvp", fake_notify)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        event = CricketEvent(
            title="CHCC Members - T20 Cricket",
            event_at=datetime(2026, 6, 14, 8, 0, 0),
            status=EventStatus.open,
            group_wa_id="120363408907704792@g.us",
        )
        contact = Contact(
            external_id="111437993250983@lid",
            display_name="Asha",
        )
        session.add_all([event, contact])
        await session.flush()

        inbound = InboundMessage(
            channel="wa_cloud",
            external_id="msg-member",
            contact_id=contact.id,
            from_external_id=contact.external_id,
            message_type="event_response",
            text="going",
            raw={
                "group_id": "120363408907704792@g.us",
                "participant_aliases": ["111437993250983@lid"],
                "event_response": {"response": 1},
            },
            received_at=datetime(2026, 6, 10, 8, 0, 0),
        )
        session.add(inbound)
        await session.flush()

        handled = await handle_possible_rsvp(session, contact, inbound)
        rows = await session.execute(EventRsvp.__table__.select())

        assert handled is True
        assert len(rows.all()) == 1

    await engine.dispose()
