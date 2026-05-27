"""Phase 2 tests — draft lifecycle (no real DB, no real Claude/WA calls)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from app.services import draft_manager
from app.models import DraftStatus


# ── helpers ──────────────────────────────────────────────────────────
def _make_contact(tier="client"):
    c = MagicMock()
    c.id = 1
    c.external_id = "919900000001"
    c.display_name = "Test User"
    c.tier = tier
    c.auto_send_enabled = False
    c.tone_profile = None
    c.last_inbound_at = datetime.now(timezone.utc)
    return c


def _make_inbound(text="Hello, are you available?"):
    m = MagicMock()
    m.id = 10
    m.text = text
    m.message_type = "text"
    m.channel = "wa_cloud"
    return m


def _make_draft(status=DraftStatus.pending, suggestions=None):
    d = MagicMock()
    d.id = 99
    d.status = status
    d.suggestions = suggestions or [
        {"tone": "concise", "text": "Yes, available at 5pm."},
        {"tone": "warm",    "text": "Sure! Happy to connect at 5pm."},
        {"tone": "formal",  "text": "Yes, I am available at 17:00."},
    ]
    d.channel = "wa_cloud"
    d.to = "919900000001"
    d.inbound_id = 10
    d.contact_id = 1
    d.policy_action = "draft"
    d.policy_reasons = ["default_draft"]
    d.outbox_id = None
    return d


# ── policy routing ────────────────────────────────────────────────────
def test_hard_block_does_not_create_draft():
    """Sensitive message → policy=alert → no draft created."""
    from app.policy_engine import Action, ContactView, Message, PolicyContext, decide
    msg = Message(text="Please share your OTP", from_id="c1", channel="wa_cloud")
    contact = ContactView(id="c1", tier="client")
    ctx = PolicyContext()
    decision = decide(msg, contact, ctx)
    assert decision.action is Action.alert


def test_draft_decision_for_unknown_contact():
    from app.policy_engine import Action, ContactView, Message, PolicyContext, decide
    msg = Message(text="Hi, saw your ad", from_id="c99", channel="wa_cloud")
    contact = ContactView(id="c99", tier="unknown")
    decision = decide(msg, contact, PolicyContext())
    assert decision.action is Action.draft
    assert "unknown_contact" in decision.reasons


def test_commitment_message_always_drafts():
    from app.policy_engine import Action, ContactView, Message, PolicyContext, decide
    msg = Message(
        text="Can we schedule a call tomorrow at 3pm for $500?",
        from_id="c1", channel="wa_cloud",
    )
    contact = ContactView(id="c1", tier="close", auto_send_enabled=True,
                          successful_interactions=100)
    decision = decide(msg, contact, PolicyContext())
    assert decision.action is Action.draft
    assert "commitment_risk" in decision.reasons


# ── approve_draft ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_approve_draft_queues_outbox():
    draft = _make_draft()
    outbox_item = MagicMock()
    outbox_item.id = 42

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=draft)

    async def _fake_queue(*args, **kwargs):
        return outbox_item

    with patch("app.services.draft_manager.session_scope") as mock_scope, \
         patch("app.services.draft_manager.outbox_svc.queue_message", side_effect=_fake_queue), \
         patch("app.services.draft_manager.audit.record", new_callable=AsyncMock):

        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await draft_manager.approve_draft(99, 1, uid=12345)

    assert "42" in result  # outbox id mentioned in reply
    assert draft.status == DraftStatus.approved
    assert draft.chosen_index == 1
    assert draft.final_text == "Sure! Happy to connect at 5pm."


@pytest.mark.asyncio
async def test_approve_draft_invalid_index():
    draft = _make_draft()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=draft)

    with patch("app.services.draft_manager.session_scope") as mock_scope:
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await draft_manager.approve_draft(99, 99, uid=12345)  # out of range

    assert "Invalid" in result


@pytest.mark.asyncio
async def test_approve_already_decided():
    draft = _make_draft(status=DraftStatus.approved)
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=draft)

    with patch("app.services.draft_manager.session_scope") as mock_scope:
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await draft_manager.approve_draft(99, 0, uid=12345)

    assert "already decided" in result


# ── reject_draft ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reject_draft():
    draft = _make_draft()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=draft)

    with patch("app.services.draft_manager.session_scope") as mock_scope, \
         patch("app.services.draft_manager.audit.record", new_callable=AsyncMock):

        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await draft_manager.reject_draft(99, uid=12345)

    assert "rejected" in result.lower()
    assert draft.status == DraftStatus.rejected


# ── edit_draft ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_edit_draft_queues_outbox():
    draft = _make_draft()
    outbox_item = MagicMock()
    outbox_item.id = 55

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=draft)

    async def _fake_queue(*args, **kwargs):
        return outbox_item

    with patch("app.services.draft_manager.session_scope") as mock_scope, \
         patch("app.services.draft_manager.outbox_svc.queue_message", side_effect=_fake_queue), \
         patch("app.services.draft_manager.audit.record", new_callable=AsyncMock):

        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await draft_manager.edit_draft(99, "Custom reply text", uid=12345)

    assert "55" in result
    assert draft.status == DraftStatus.edited
    assert draft.final_text == "Custom reply text"


# ── cancel_outbox ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cancel_outbox_success():
    draft = _make_draft()
    draft.outbox_id = 77

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=draft)

    with patch("app.services.draft_manager.session_scope") as mock_scope, \
         patch("app.services.draft_manager.outbox_svc.cancel_message",
               new_callable=AsyncMock, return_value=True), \
         patch("app.services.draft_manager.audit.record", new_callable=AsyncMock):

        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await draft_manager.cancel_outbox(99, uid=12345)

    assert "Cancelled" in result


@pytest.mark.asyncio
async def test_cancel_outbox_too_late():
    draft = _make_draft()
    draft.outbox_id = 77

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=draft)

    with patch("app.services.draft_manager.session_scope") as mock_scope, \
         patch("app.services.draft_manager.outbox_svc.cancel_message",
               new_callable=AsyncMock, return_value=False), \
         patch("app.services.draft_manager.audit.record", new_callable=AsyncMock):

        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await draft_manager.cancel_outbox(99, uid=12345)

    assert "Too late" in result or "already sent" in result.lower()
