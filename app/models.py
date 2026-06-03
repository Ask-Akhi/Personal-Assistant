"""SQLAlchemy models — Phase 2: structured memory, audit log, outbox, cost ledger, drafts."""
from __future__ import annotations
import enum
from datetime import datetime
from sqlalchemy import (
    JSON, Boolean, DateTime, Enum, Float, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ── Audit ────────────────────────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasons: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# ── Structured memory ────────────────────────────────────────────────
class MemoryKind(str, enum.Enum):
    contact_preference = "contact_preference"
    relationship_context = "relationship_context"
    active_commitment = "active_commitment"
    blocked_topic = "blocked_topic"
    assistant_rule = "assistant_rule"
    note = "note"


class Memory(Base):
    __tablename__ = "memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[MemoryKind] = mapped_column(Enum(MemoryKind), index=True)
    subject: Mapped[str] = mapped_column(String(128), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (UniqueConstraint("kind", "subject", "key", name="uq_memory"),)


# ── Idempotency ──────────────────────────────────────────────────────
class ProcessedEvent(Base):
    __tablename__ = "processed_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_processed_event"),
    )


# ── Outbox ───────────────────────────────────────────────────────────
class OutboxStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    cancelled = "cancelled"
    failed = "failed"


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32))
    to: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus), default=OutboxStatus.pending, index=True
    )
    send_after: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


Index("ix_outbox_pending", Outbox.status, Outbox.send_after)


# ── Cost ledger ──────────────────────────────────────────────────────
class CostLedger(Base):
    __tablename__ = "cost_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    units: Mapped[float] = mapped_column(Float)
    usd: Mapped[float] = mapped_column(Float)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# ── PIN session ──────────────────────────────────────────────────────
class PinSession(Base):
    __tablename__ = "pin_session"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ── Contacts ─────────────────────────────────────────────────────────
class Contact(Base):
    __tablename__ = "contact"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tier: Mapped[str] = mapped_column(String(32), default="unknown")
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    language_pref: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tone_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ── Inbound messages ─────────────────────────────────────────────────
class InboundMessage(Base):
    __tablename__ = "inbound_message"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    from_external_id: Mapped[str] = mapped_column(String(128), index=True)
    message_type: Mapped[str] = mapped_column(String(32), default="unknown")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_inbound_message"),
    )


# ── Draft replies ────────────────────────────────────────────────────
class DraftStatus(str, enum.Enum):
    pending    = "pending"
    approved   = "approved"
    edited     = "edited"
    rejected   = "rejected"
    superseded = "superseded"


class Draft(Base):
    __tablename__ = "draft"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inbound_id: Mapped[int] = mapped_column(ForeignKey("inbound_message.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    to: Mapped[str] = mapped_column(String(128))
    suggestions: Mapped[list] = mapped_column(JSON)
    chosen_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DraftStatus] = mapped_column(
        Enum(DraftStatus), default=DraftStatus.pending, index=True
    )
    policy_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outbox_id: Mapped[int | None] = mapped_column(ForeignKey("outbox.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


# ── Commitments (Phase 3) ────────────────────────────────────────────
class CommitmentStatus(str, enum.Enum):
    open      = "open"
    reminded  = "reminded"
    done      = "done"
    dismissed = "dismissed"


class Commitment(Base):
    __tablename__ = "commitment"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    inbound_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_message.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    raw_snippet: Mapped[str] = mapped_column(Text)
    context_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CommitmentStatus] = mapped_column(
        Enum(CommitmentStatus), default=CommitmentStatus.open, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ── Reminders (Phase 3) ──────────────────────────────────────────────
class ReminderStatus(str, enum.Enum):
    pending   = "pending"
    sent      = "sent"
    snoozed   = "snoozed"
    dismissed = "dismissed"


class Reminder(Base):
    __tablename__ = "reminder"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    commitment_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment.id"), nullable=True, index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contact.id"), nullable=True, index=True
    )
    message: Mapped[str] = mapped_column(Text)
    fire_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus), default=ReminderStatus.pending, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


Index("ix_reminder_pending", Reminder.status, Reminder.fire_at)


# ── Cricket / Sports Events ──────────────────────────────────────────
class EventStatus(str, enum.Enum):
    open      = "open"       # accepting RSVPs
    closed    = "closed"     # cutoff reached, calling list sent
    cancelled = "cancelled"


class CricketEvent(Base):
    __tablename__ = "cricket_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(128))                      # "T20 Cricket"
    event_at: Mapped[datetime] = mapped_column(DateTime)                 # match datetime
    venue: Mapped[str | None] = mapped_column(String(256), nullable=True)
    group_wa_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # WA group id
    cutoff_hours: Mapped[float] = mapped_column(Float, default=36.0)     # hours before event
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus), default=EventStatus.open, index=True
    )
    announcement_wa_msg_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    calling_list_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


Index("ix_cricket_event_open", CricketEvent.status, CricketEvent.cutoff_at)


class RsvpStatus(str, enum.Enum):
    yes  = "yes"
    no   = "no"
    wnbo = "wnbo"   # Will Not Bowl - coming but not bowling
    maybe = "maybe"


class EventRsvp(Base):
    __tablename__ = "event_rsvp"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("cricket_event.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    inbound_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_message.id"), nullable=True
    )
    status: Mapped[RsvpStatus] = mapped_column(Enum(RsvpStatus), index=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)   # original message
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)  # extra context
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("event_id", "contact_id", name="uq_event_rsvp"),
    )


Index("ix_event_rsvp_event", EventRsvp.event_id, EventRsvp.status)
