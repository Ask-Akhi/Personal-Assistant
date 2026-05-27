from datetime import datetime, timedelta, timezone

from app.services.whatsapp import send_policy, service_window_open


def test_service_window_open_with_recent_inbound():
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    last_inbound = now - timedelta(hours=2)
    assert service_window_open(last_inbound, now=now) is True


def test_service_window_closed_without_recent_inbound():
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    last_inbound = now - timedelta(hours=25)
    policy = send_policy(last_inbound, now=now)
    assert policy.can_send_freeform is False
    assert policy.requires_template is True
    assert "template_required" in policy.reasons


def test_service_window_closed_without_history():
    policy = send_policy(None, now=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
    assert policy.can_send_freeform is False
    assert policy.requires_template is True
