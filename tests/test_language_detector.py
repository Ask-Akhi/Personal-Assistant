"""Tests for language detector — pure function, no I/O."""
from __future__ import annotations

import pytest
from app.services.language_detector import detect, tone_profile_from_result


def test_devanagari_detected_as_hindi():
    result = detect("नमस्ते, आप कैसे हैं?")
    assert result.lang == "hi"
    assert result.script in ("devanagari", "mixed")
    assert result.confidence > 0.7


def test_hinglish_detected():
    result = detect("bhai kal milte hain kya, theek hai?")
    assert result.lang == "hinglish"
    assert result.script == "latin"
    assert result.confidence > 0.6


def test_english_detected():
    result = detect("Hey, can we schedule a call tomorrow at 5 PM?")
    assert result.lang == "en"
    assert result.confidence > 0.7


def test_formal_english():
    result = detect("Dear Sir, please find the attached invoice for your review.")
    assert result.lang == "en"
    assert result.formality == "formal"


def test_casual_english():
    result = detect("lol omg that's so funny haha")
    assert result.formality == "casual"


def test_short_message_casual():
    result = detect("ok")
    assert result.formality == "casual"


def test_emoji_density():
    result = detect("😂😂😂 so funny bro")
    assert result.emoji_density > 0.0


def test_empty_string_returns_default():
    result = detect("")
    assert result.lang == "en"
    assert result.confidence == 0.5


def test_mixed_devanagari_latin():
    result = detect("यार please kal 5 baje आना")
    assert result.lang in ("hi", "hinglish")


def test_tone_profile_shape():
    result = detect("bhai theek hai, kal milte hain")
    profile = tone_profile_from_result(result)
    assert "language" in profile
    assert "formality" in profile
    assert "emoji_usage" in profile
    assert profile["emoji_usage"] in ("low", "medium", "high")
