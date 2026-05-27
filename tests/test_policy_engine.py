"""Unit tests for the pure policy engine."""
from __future__ import annotations

from app.policy_engine import (
    Action,
    ContactView,
    Message,
    PolicyContext,
    decide,
)


def _msg(text: str) -> Message:
    return Message(text=text, from_id="c1", channel="wa_cloud")


def test_hard_block_otp():
    d = decide(_msg("share your OTP please"), ContactView(id="c1"), PolicyContext())
    assert d.action is Action.alert
    assert any("hard_block" in r for r in d.reasons)


def test_hard_block_legal():
    d = decide(_msg("review the NDA"), ContactView(id="c1"), PolicyContext())
    assert d.action is Action.alert


def test_unknown_contact_drafts():
    d = decide(_msg("hi"), ContactView(id="c1", tier="unknown"), PolicyContext())
    assert d.action is Action.draft
    assert "unknown_contact" in d.reasons


def test_trivial_reply_auto_send_when_graduated():
    contact = ContactView(
        id="c1", tier="close", auto_send_enabled=True, successful_interactions=25
    )
    d = decide(_msg("ok"), contact, PolicyContext())
    assert d.action is Action.auto_send


def test_trivial_reply_blocked_in_quiet_hours():
    contact = ContactView(
        id="c1", tier="close", auto_send_enabled=True, successful_interactions=25
    )
    d = decide(_msg("ok"), contact, PolicyContext(quiet_hours=True))
    assert d.action is Action.draft
    assert "quiet_hours" in d.reasons


def test_not_graduated_drafts():
    contact = ContactView(
        id="c1", tier="close", auto_send_enabled=True, successful_interactions=3
    )
    d = decide(_msg("ok"), contact, PolicyContext())
    assert d.action is Action.draft
    assert any("needs_more_interactions" in r for r in d.reasons)


def test_long_message_to_known_contact_drafts():
    contact = ContactView(
        id="c1", tier="client", auto_send_enabled=True, successful_interactions=100
    )
    d = decide(
        _msg("Can we reschedule the kickoff to next Tuesday at 4pm for $5000?"),
        contact,
        PolicyContext(),
    )
    assert d.action is Action.draft
    assert "commitment_risk" in d.reasons


def test_blocked_topic_custom():
    d = decide(
        _msg("about the divorce paperwork"),
        ContactView(id="c1", tier="close"),
        PolicyContext(blocked_topics=("divorce",)),
    )
    assert d.action is Action.alert


def test_money_signal_forces_draft():
    contact = ContactView(
        id="c1", tier="close", auto_send_enabled=True, successful_interactions=100
    )
    d = decide(_msg("Please send the invoice for INR 25000"), contact, PolicyContext())
    assert d.action is Action.draft
    assert "commitment_risk" in d.reasons
