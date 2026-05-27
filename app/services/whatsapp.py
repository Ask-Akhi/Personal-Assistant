"""Helpers for WhatsApp Cloud API constraints."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


SERVICE_WINDOW_HOURS = 24


@dataclass(frozen=True)
class WhatsAppSendPolicy:
    can_send_freeform: bool
    requires_template: bool
    reasons: tuple[str, ...]


def service_window_open(last_inbound_at: datetime | None, *, now: datetime | None = None) -> bool:
    if last_inbound_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    inbound = (
        last_inbound_at
        if last_inbound_at.tzinfo is not None
        else last_inbound_at.replace(tzinfo=timezone.utc)
    )
    return current - inbound <= timedelta(hours=SERVICE_WINDOW_HOURS)


def send_policy(last_inbound_at: datetime | None, *, now: datetime | None = None) -> WhatsAppSendPolicy:
    if service_window_open(last_inbound_at, now=now):
        return WhatsAppSendPolicy(
            can_send_freeform=True,
            requires_template=False,
            reasons=("service_window_open",),
        )
    return WhatsAppSendPolicy(
        can_send_freeform=False,
        requires_template=True,
        reasons=("service_window_closed", "template_required"),
    )
