"""Pure-function policy engine. No I/O. Fully unit-testable.

decide(message, contact, memory, rules) -> Decision

Decision.action ∈ {auto_send, draft, block, alert}
Every decision carries explainable reasons.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from enum import Enum
from typing import Iterable


class Action(str, Enum):
    auto_send = "auto_send"
    draft = "draft"
    block = "block"
    alert = "alert"


@dataclass(frozen=True)
class Message:
    text: str
    from_id: str
    channel: str  # "wa_cloud" | "wa_baileys" | "telegram"


@dataclass(frozen=True)
class ContactView:
    id: str
    tier: str = "unknown"            # close|family|client|vendor|unknown
    auto_send_enabled: bool = False
    successful_interactions: int = 0  # for graduation


@dataclass(frozen=True)
class PolicyContext:
    quiet_hours: bool = False
    blocked_topics: tuple[str, ...] = ()
    safe_reply_classes: tuple[str, ...] = (
        "ack", "thanks", "got_it", "will_check",
    )
    min_interactions_for_auto: int = 20


@dataclass
class Decision:
    action: Action
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0


# ── Heuristics ───────────────────────────────────────────────────────
HARD_BLOCK_TOKENS = (
    "otp", "password", "passcode", "cvv", "aadhaar", "aadhar",
    "bank account", "ifsc", "upi pin", "seed phrase", "private key",
)

LEGAL_TOKENS = ("contract", "nda", "agreement", "lawsuit", "legal notice")
MONEY_PATTERN = re.compile(r"(\$|usd|inr|rs\.?|rupees?)\s*\d|\b\d+(?:\.\d+)?\s*(usd|inr|rs\.?|rupees?)\b", re.IGNORECASE)
TIME_PATTERN = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"today|tomorrow|next week|next month|\d{1,2}[:.]\d{2}\s?(?:am|pm)?)\b",
    re.IGNORECASE,
)


def _lc(s: str) -> str:
    return (s or "").lower()


def _hard_block(text: str, blocked_topics: Iterable[str]) -> str | None:
    t = _lc(text)
    for tok in HARD_BLOCK_TOKENS:
        if tok in t:
            return f"hard_block:sensitive:{tok}"
    for tok in blocked_topics:
        if tok and tok.lower() in t:
            return f"hard_block:blocked_topic:{tok}"
    for tok in LEGAL_TOKENS:
        if tok in t:
            return f"hard_block:legal:{tok}"
    return None


def _looks_trivial(text: str) -> bool:
    t = _lc(text).strip().strip(".!?")
    return t in {
        "ok", "okay", "k", "thanks", "thank you", "ty",
        "got it", "noted", "sure", "cool",
        "theek hai", "ji", "haan", "haa",
    } or len(t) <= 3


def _has_commitment_signal(text: str) -> bool:
    t = _lc(text)
    return bool(MONEY_PATTERN.search(t) or TIME_PATTERN.search(t)) or any(
        token in t
        for token in ("schedule", "reschedule", "book", "invoice", "payment", "quote", "deadline")
    )


def decide(
    msg: Message,
    contact: ContactView,
    ctx: PolicyContext,
) -> Decision:
    reasons: list[str] = []

    # 1) Hard blocks
    blocked = _hard_block(msg.text, ctx.blocked_topics)
    if blocked:
        return Decision(Action.alert, [blocked], confidence=1.0)

    # 2) Quiet hours → never auto-send
    if ctx.quiet_hours:
        reasons.append("quiet_hours")

    # 3) Unknown contacts → always draft
    if contact.tier == "unknown":
        return Decision(Action.draft, [*reasons, "unknown_contact"], confidence=0.9)

    # 4) Commitments, timing, or money always stay human-reviewed.
    if _has_commitment_signal(msg.text):
        return Decision(
            Action.draft,
            [*reasons, "commitment_risk"],
            confidence=0.9,
        )

    # 5) Auto-send eligibility
    eligible_auto = (
        contact.auto_send_enabled
        and not ctx.quiet_hours
        and contact.successful_interactions >= ctx.min_interactions_for_auto
        and _looks_trivial(msg.text)
    )
    if eligible_auto:
        return Decision(
            Action.auto_send,
            [*reasons, "tier:" + contact.tier, "trivial_reply", "graduated"],
            confidence=0.85,
        )

    # 6) Default: draft for approval
    reasons.append("default_draft")
    if not contact.auto_send_enabled:
        reasons.append("auto_send_off")
    if contact.successful_interactions < ctx.min_interactions_for_auto:
        reasons.append(
            f"needs_more_interactions:{contact.successful_interactions}/{ctx.min_interactions_for_auto}"
        )
    return Decision(Action.draft, reasons, confidence=0.7)
