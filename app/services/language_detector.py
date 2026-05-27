"""Language / script detector for English, Hindi (Devanagari), and Hinglish (Roman Hindi).

Pure function — no I/O, fully unit-testable.

Detection logic (cheap, no API call needed):
1. If ≥15 % of characters are Devanagari codepoints → "hi" (Hindi in Devanagari)
2. If the text contains strong Hindi Roman markers (common Hindi words in Latin script) → "hinglish"
3. Otherwise → "en"

The result is a LanguageResult dataclass with:
  - lang:       "en" | "hi" | "hinglish"
  - script:     "latin" | "devanagari" | "mixed"
  - confidence: 0.0 – 1.0
  - formality:  "formal" | "casual" | "unknown"  (heuristic)
  - emoji_density: float (emojis / total chars)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ── Devanagari block: U+0900–U+097F ─────────────────────────────────
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

# Common Hindi words written in Roman script (Hinglish markers)
_HINGLISH_MARKERS = frozenset([
    "hai", "hain", "ho", "tha", "thi", "the", "kya", "kaise", "kab", "kyun",
    "nahi", "nahin", "nhin", "acha", "accha", "thik", "theek", "bhai", "yaar",
    "kal", "aaj", "abhi", "phir", "bas", "mat", "kar", "karo", "karna",
    "bata", "batao", "dekh", "dekho", "sun", "suno", "bol", "bolo",
    "hoga", "hogi", "hoge", "gaya", "gayi", "gaye", "hua", "hui",
    "mujhe", "tumhe", "aapko", "mera", "tera", "apna", "unka",
    "kuch", "sab", "sirf", "bahut", "thoda", "zyada", "jaldi",
    "dono", "teeno", "sath", "saath", "wala", "wali", "waale",
    "matlab", "matlab", "samajh", "pata", "pataa",
    "arrey", "are", "arre", "yeh", "woh", "wo", "ye",
    "ji", "haan", "naa", "na", "hmm",
    "bhai", "didi", "bhaiya", "behan", "behen",
])

# Casual / informal signal words (English)
_CASUAL_EN = frozenset([
    "lol", "lmao", "haha", "hehe", "omg", "wtf", "idk", "tbh", "ngl",
    "btw", "fyi", "afaik", "imo", "imho", "rn", "asap", "thx", "ty",
    "gonna", "wanna", "gotta", "kinda", "sorta", "lemme", "gimme",
])

# Formal signal words
_FORMAL_SIGNALS = re.compile(
    r"\b(dear|regards|sincerely|please find|kindly|pursuant|herewith|"
    r"attached|enclos|invoice|proposal|contract|agreement|meeting|agenda)\b",
    re.IGNORECASE,
)

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0\U000024C2-\U0001F251]+",
    flags=re.UNICODE,
)


@dataclass(frozen=True)
class LanguageResult:
    lang: str           # "en" | "hi" | "hinglish"
    script: str         # "latin" | "devanagari" | "mixed"
    confidence: float
    formality: str      # "formal" | "casual" | "unknown"
    emoji_density: float


def detect(text: str) -> LanguageResult:
    """Detect language/script, formality, and emoji density of a message."""
    if not text or not text.strip():
        return LanguageResult("en", "latin", 0.5, "unknown", 0.0)

    cleaned = text.strip()
    total_chars = max(len(cleaned), 1)

    # ── Script detection ──────────────────────────────────────────────
    deva_chars = len(_DEVANAGARI_RE.findall(cleaned))
    deva_ratio = deva_chars / total_chars

    if deva_ratio >= 0.15:
        script = "devanagari" if deva_ratio >= 0.5 else "mixed"
        lang = "hi"
        confidence = min(0.6 + deva_ratio, 0.98)
        formality = _formality(cleaned)
        return LanguageResult(lang, script, confidence, formality, _emoji_density(cleaned, total_chars))

    # ── Hinglish detection (Latin-script Hindi) ───────────────────────
    words = re.findall(r"\b[a-zA-Z]+\b", cleaned.lower())
    if words:
        hinglish_hits = sum(1 for w in words if w in _HINGLISH_MARKERS)
        hinglish_ratio = hinglish_hits / len(words)
        if hinglish_ratio >= 0.15 or hinglish_hits >= 3:
            confidence = min(0.55 + hinglish_ratio * 2, 0.95)
            return LanguageResult(
                "hinglish", "latin", confidence,
                _formality(cleaned), _emoji_density(cleaned, total_chars)
            )

    # ── Default: English ──────────────────────────────────────────────
    return LanguageResult(
        "en", "latin", 0.85,
        _formality(cleaned), _emoji_density(cleaned, total_chars)
    )


def _formality(text: str) -> str:
    lower = text.lower()
    words = set(re.findall(r"\b[a-z]+\b", lower))
    if _FORMAL_SIGNALS.search(text):
        return "formal"
    if words & _CASUAL_EN or "!" * 2 in text or any(c * 3 in text for c in "?!"):
        return "casual"
    if len(text) < 20:
        return "casual"
    return "unknown"


def _emoji_density(text: str, total_chars: int) -> float:
    emoji_chars = sum(len(m) for m in _EMOJI_RE.findall(text))
    return round(emoji_chars / total_chars, 3)


def tone_profile_from_result(result: LanguageResult) -> dict:
    """Convert a LanguageResult into a tone_profile dict for the Contact model."""
    emoji_usage = "high" if result.emoji_density > 0.05 else (
        "medium" if result.emoji_density > 0.01 else "low"
    )
    return {
        "language": result.lang,
        "script": result.script,
        "formality": result.formality,
        "emoji_usage": emoji_usage,
    }
