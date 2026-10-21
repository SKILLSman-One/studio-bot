from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from studio_bot.runtime import deliver_once


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
