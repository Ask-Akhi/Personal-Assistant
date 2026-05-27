"""Commitment extractor — pure function, no I/O.

Scans message text for signals that represent a commitment, date/time,
money amount, or action item that should be tracked.

Returns a list of CommitmentSignal dataclasses. Each signal has:
  - kind:        "date_time" | "money" | "action" | "deadline"
  - raw_snippet: the matched text fragment
  - confidence:  0.0 – 1.0

The caller (draft_manager / reminder_engine) decides what to persist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# ── Patterns ─────────────────────────────────────────────────────────

# Money: ₹, Rs, INR, $, USD followed by digits  OR  digits followed by unit
_MONEY_RE = re.compile(
    r"(?:₹|rs\.?|inr|usd|\$)\s*[\d,]+(?:\.\d+)?k?"
    r"|[\d,]+(?:\.\d+)?k?\s*(?:₹|rs\.?|inr|usd|rupees?|dollars?)",
    re.IGNORECASE,
)

# Date/time: tomorrow, next Monday, specific dates, times
_DATETIME_RE = re.compile(
    r"\b(?:"
    r"today|tomorrow|yesterday"
    r"|next\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|this\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(?:\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?)"
    r"|(?:\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:\s+\d{4})?)"
    r"|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:\s+\d{4})?)"
    r"|(?:\d{1,2}:\d{2}\s*(?:am|pm)?)"
    r"|(?:\d{1,2}\s*(?:am|pm))"
    r"|kal|parso|aaj"        # Hindi time words
    r")\b",
    re.IGNORECASE,
)

# Action/deadline keywords
_ACTION_RE = re.compile(
    r"\b(?:"
    r"please\s+\w+|can\s+you\s+\w+|could\s+you\s+\w+"
    r"|send\s+(?:me|the|us)\b|share\s+(?:the|your)\b"
    r"|confirm|let\s+me\s+know|update\s+(?:me|us)\b"
    r"|schedule|reschedule|book|cancel\s+the"
    r"|deadline|due\s+(?:by|on|date)"
    r"|will\s+(?:you|he|she|they)\b|you\s+will\b"
    r"|bata\s+do|bata\s+dena|bhej\s+do|bhej\s+dena"   # Hinglish action words
    r"|karo\s+please|kar\s+dena"
    r")\b",
    re.IGNORECASE,
)

# Strong commitment phrases ("I will", "We will", "I'll")
_COMMITMENT_RE = re.compile(
    r"\b(?:i\s+will\b|we\s+will\b|i'll\b|we'll\b|i\s+shall\b"
    r"|main\s+karunga\b|main\s+kar\s+deta\b|main\s+bhejta\b"   # Hindi
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CommitmentSignal:
    kind: str           # "date_time" | "money" | "action" | "commitment"
    raw_snippet: str    # the matched text
    confidence: float


def extract(text: str) -> list[CommitmentSignal]:
    """Return all commitment signals found in text."""
    if not text or not text.strip():
        return []

    results: list[CommitmentSignal] = []

    for m in _MONEY_RE.finditer(text):
        results.append(CommitmentSignal("money", m.group().strip(), 0.95))

    for m in _DATETIME_RE.finditer(text):
        results.append(CommitmentSignal("date_time", m.group().strip(), 0.85))

    for m in _ACTION_RE.finditer(text):
        results.append(CommitmentSignal("action", m.group().strip(), 0.75))

    for m in _COMMITMENT_RE.finditer(text):
        results.append(CommitmentSignal("commitment", m.group().strip(), 0.90))

    # Deduplicate overlapping matches by snippet
    seen: set[str] = set()
    unique: list[CommitmentSignal] = []
    for s in results:
        key = s.raw_snippet.lower()
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return unique


def has_commitment(text: str) -> bool:
    """Quick boolean check — used by policy_engine."""
    return bool(extract(text))


def summary(signals: Sequence[CommitmentSignal]) -> str:
    """One-line human-readable summary of extracted signals."""
    if not signals:
        return "none"
    parts = [f"{s.kind}:{s.raw_snippet[:40]}" for s in signals[:5]]
    return ", ".join(parts)
