import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from studio_bot.config import UserError
from studio_bot.payroll import Entry, Snapshot, SourceIssue
from studio_bot.runtime import deliver_once, payroll_loop


async def test_delivery_flood_wait_and_recipient_block_are_saved(people):
    with people.db:
        people.db.execute("DELETE FROM outbox")
        people.enqueue("model", 10, "Тест 1")
        people.enqueue("model", 20, "Тест 2")
    method = SendMessage(chat_id=10, text="x")
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=[
                TelegramRetryAfter(method, "wait", 30),
                TelegramForbiddenError(method, "blocked"),
            ]
        )
    )
    await deliver_once(people, {"model": bot})
    rows = people.all("SELECT * FROM outbox ORDER BY id")
    assert rows[0]["state"] == "pending" and rows[0]["retry_at"] == people.clock() + 31
    assert rows[1]["state"] == "failed"
    bot.send_message.reset_mock()
    await deliver_once(people, {"model": bot})
    bot.send_message.assert_not_called()


async def test_delivery_success_and_network_retry(people):
    with people.db:
        people.db.execute("DELETE FROM outbox")
        people.enqueue("model", 10, "Сохранённый ответ")
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=ConnectionError()))
    await deliver_once(people, {"model": bot})
    row = people.one("SELECT * FROM outbox")
    assert row["state"] == "pending"
    now = people.clock()
    people.clock = lambda: now + 10
    bot.send_message.side_effect = None
    await deliver_once(people, {"model": bot})
    assert people.one("SELECT state FROM outbox")["state"] == "sent"


async def test_payroll_logs_real_read_status_once_and_never_private_data(monkeypatch, capsys):
    snapshot = Snapshot(
        (
            Entry(
                "private-id",
                date(2026, 11, 1),
                "private-model",
                "PRIVATE_NAME",
                None,
                Decimal("12345.67"),
                share_available=False,
            ),
        ),
        datetime.now(timezone.utc),
    )
    payroll = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                snapshot,
                snapshot,
                UserError("SECRET_HTTP_TOKEN"),
                snapshot,
            ]
        )
    )
    delay = AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()])
    monkeypatch.setattr("studio_bot.runtime.asyncio.sleep", delay)
    with pytest.raises(asyncio.CancelledError):
        await payroll_loop(payroll)
    out = capsys.readouterr().out
    assert out.count("Google Sheets:") == 3
    assert out.count("чтение успешно") == 2
    assert "2026-11" in out and "без процента: 1" in out
    assert "чтение не удалось (UserError)" in out
    assert not any(
        secret in out
        for secret in ("PRIVATE_NAME", "12345.67", "SECRET_HTTP_TOKEN", "private-model")
    )
    assert all(call.kwargs == {"force": True} for call in payroll.get.call_args_list)


async def test_payroll_partial_status_does_not_claim_all_sources_valid(monkeypatch, capsys):
    snapshot = Snapshot(
        (),
        datetime.now(timezone.utc),
        (),
        issues=(SourceIssue(date(2026, 11, 1), date(2026, 11, 30), "PRIVATE_DETAILS"),),
    )
    payroll = SimpleNamespace(get=AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        "studio_bot.runtime.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await payroll_loop(payroll)
    out = capsys.readouterr().out
    assert "чтение частичное" in out and "недоступных источников: 1" in out
    assert "успешно" not in out and "PRIVATE_DETAILS" not in out
