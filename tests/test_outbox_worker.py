"""Tests for the outbox worker drain logic."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Outbox, OutboxStatus


def _make_outbox(
    outbox_id: int = 1,
    status: OutboxStatus = OutboxStatus.pending,
    attempts: int = 0,
    channel: str = "wa_cloud",
    to: str = "919900000001",
    body: str = "Hello!",
) -> Outbox:
    item = Outbox()
    item.id = outbox_id
    item.channel = channel
    item.to = to
    item.body = body
    item.status = status
    item.attempts = attempts
    item.last_error = None
    item.send_after = datetime.now(timezone.utc) - timedelta(seconds=60)
    return item


@pytest.mark.asyncio
async def test_send_one_success():
    item = _make_outbox()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=item)

    with patch("app.jobs.outbox_worker.session_scope") as mock_scope, \
         patch("app.jobs.outbox_worker.wa_sender.send_text",
               new_callable=AsyncMock, return_value="wamid.123"), \
         patch("app.jobs.outbox_worker.outbox_svc.mark_sent",
               new_callable=AsyncMock, return_value=True), \
         patch("app.jobs.outbox_worker.audit.record", new_callable=AsyncMock), \
         patch("app.jobs.outbox_worker.send_admin_message", new_callable=AsyncMock):
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.jobs import outbox_worker
        await outbox_worker._send_one(1)

        outbox_worker.wa_sender.send_text.assert_awaited_once_with("919900000001", "Hello!")


@pytest.mark.asyncio
async def test_send_one_skips_cancelled():
    item = _make_outbox(status=OutboxStatus.cancelled)
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=item)

    with patch("app.jobs.outbox_worker.session_scope") as mock_scope, \
         patch("app.jobs.outbox_worker.wa_sender.send_text",
               new_callable=AsyncMock) as mock_send:
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.jobs import outbox_worker
        await outbox_worker._send_one(1)
        mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_one_skips_non_wa_cloud_channel():
    item = _make_outbox(channel="wa_baileys")
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=item)

    with patch("app.jobs.outbox_worker.session_scope") as mock_scope, \
         patch("app.jobs.outbox_worker.wa_sender.send_text",
               new_callable=AsyncMock) as mock_send:
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.jobs import outbox_worker
        await outbox_worker._send_one(1)
        mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_one_gives_up_at_max_attempts():
    item = _make_outbox(attempts=3)  # already at MAX_ATTEMPTS
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=item)

    with patch("app.jobs.outbox_worker.session_scope") as mock_scope, \
         patch("app.jobs.outbox_worker.outbox_svc.mark_failed",
               new_callable=AsyncMock) as mock_fail, \
         patch("app.jobs.outbox_worker.audit.record", new_callable=AsyncMock), \
         patch("app.jobs.outbox_worker.send_admin_message", new_callable=AsyncMock), \
         patch("app.jobs.outbox_worker.wa_sender.send_text",
               new_callable=AsyncMock) as mock_send:
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.jobs import outbox_worker
        await outbox_worker._send_one(1)

        mock_fail.assert_awaited_once()
        mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_one_records_failure_and_retries():
    item = _make_outbox(attempts=1)
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=item)

    with patch("app.jobs.outbox_worker.session_scope") as mock_scope, \
         patch("app.jobs.outbox_worker.wa_sender.send_text",
               new_callable=AsyncMock, side_effect=Exception("timeout")), \
         patch("app.jobs.outbox_worker.outbox_svc.mark_failed",
               new_callable=AsyncMock) as mock_fail, \
         patch("app.jobs.outbox_worker.audit.record", new_callable=AsyncMock), \
         patch("app.jobs.outbox_worker.send_admin_message", new_callable=AsyncMock):
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.jobs import outbox_worker
        await outbox_worker._send_one(1)

        # mark_failed called with the actual error (not max_attempts_exceeded)
        call_kwargs = mock_fail.call_args
        assert call_kwargs.kwargs.get("error") != "max_attempts_exceeded"
