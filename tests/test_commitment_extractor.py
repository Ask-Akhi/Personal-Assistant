"""Tests for commitment extractor — pure function, no I/O."""
from __future__ import annotations

import pytest
from app.services.commitment_extractor import extract, has_commitment, summary


def test_extracts_money_inr():
    signals = extract("Please send the payment of ₹25000 by Friday")
    kinds = [s.kind for s in signals]
    assert "money" in kinds


def test_extracts_money_usd():
    signals = extract("The invoice is for $1500 USD")
    kinds = [s.kind for s in signals]
    assert "money" in kinds


def test_extracts_date_tomorrow():
    signals = extract("Let's meet tomorrow at 5pm")
    kinds = [s.kind for s in signals]
    assert "date_time" in kinds


def test_extracts_date_next_monday():
    signals = extract("Can we reschedule to next Monday?")
    kinds = [s.kind for s in signals]
    assert "date_time" in kinds


def test_extracts_action_please_send():
    signals = extract("please send me the report")
    kinds = [s.kind for s in signals]
    assert "action" in kinds


def test_extracts_commitment_i_will():
    signals = extract("I will call you back in an hour")
    kinds = [s.kind for s in signals]
    assert "commitment" in kinds


def test_hinglish_action():
    signals = extract("bhai bata do kab free ho")
    kinds = [s.kind for s in signals]
    assert "action" in kinds


def test_no_signals_in_simple_ack():
    signals = extract("ok thanks")
    assert signals == []


def test_has_commitment_true():
    assert has_commitment("meeting tomorrow at 3pm") is True


def test_has_commitment_false():
    assert has_commitment("haha nice") is False


def test_summary_not_empty():
    signals = extract("please send the invoice for ₹5000 by next Friday")
    s = summary(signals)
    assert s != "none"
    assert len(s) > 0


def test_deduplicates_overlapping():
    # Same snippet should not appear twice
    signals = extract("meet tomorrow and also tomorrow at 5pm")
    snippets = [s.raw_snippet.lower() for s in signals]
    assert len(snippets) == len(set(snippets))
