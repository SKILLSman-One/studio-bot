from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    SendDocument,
    SendMediaGroup,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from studio_bot.auth import AdminAuth, hash_password
from studio_bot.config import Settings
from studio_bot.payroll import Entry, Snapshot
from studio_bot.store import Store
from studio_bot.ui import UI


@pytest.fixture
def store(tmp_path):
    instance = Store(
        tmp_path / "studio.sqlite3",
        clock=lambda: datetime(2026, 10, 16, 9, tzinfo=timezone.utc).timestamp(),
    )
    yield instance
    instance.close()


@pytest.fixture
def people(store):
    for uid, name in ((10, "Аня"), (20, "Мария")):
        store.register(uid, name, "", "Тест")
        store.approve_person(uid, f"m{uid}", 99)
    return store


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        if isinstance(method, SendMediaGroup):
            assert len(method.media) == 2
            return [
                Message(
                    message_id=len(self.calls) + i,
                    date=datetime.now(timezone.utc),
                    chat=Chat(id=int(method.chat_id), type="private"),
                )
                for i in range(len(method.media))
            ]
        if isinstance(method, (SendMessage, SendDocument)):
            if isinstance(method, SendMessage):
                assert len(method.text.encode("utf-16-le")) // 2 <= 4096
            if method.reply_markup:
                for row in method.reply_markup.inline_keyboard:
                    for button in row:
                        assert len(button.callback_data.encode()) <= 64
            return Message(
                message_id=len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        raise AssertionError(type(method).__name__)

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True
    ):
        yield b""


@pytest.fixture
async def ui(tmp_path, people):
    bot = Bot("12345:" + "x" * 35, session=RecordingSession())
    bots = {"model": bot, "admin": bot}
    settings = Settings(tmp_path, admin_ids=(99,), password_hash=hash_password("correct-password"))
    auth = AdminAuth(people, settings, clock=people.clock)
    today = datetime(2026, 10, 16, tzinfo=timezone.utc)
    snapshot = Snapshot(
        (
            Entry("a", today.date(), "m10", "Аня", Decimal("125.50"), Decimal("251.00")),
            Entry("b", today.date(), "m20", "Мария", Decimal("900.00"), Decimal("1500.00")),
            Entry("c", today.date().replace(month=9), "m10", "Аня", Decimal("200"), Decimal("400")),
        ),
        today,
    )
    payroll = SimpleNamespace(get=AsyncMock(return_value=snapshot))
    controller = UI(settings, people, payroll, bots, auth)
    yield controller
    await controller.dispatcher.storage.close()
    await bot.session.close()


async def message(ui, text, uid=10, role="model", chat_type="private"):
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=uid, type=chat_type),
            from_user=User(id=uid, is_bot=False, first_name="Test"),
            text=text,
        ),
    )
    await ui.dispatcher.feed_update(ui.bots[role], update)


async def click(ui, data, uid=10, role="model", chat_type="private", epoch=None):
    update = Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="q",
            from_user=User(id=uid, is_bot=False, first_name="Test"),
            chat_instance="x",
            data=("a:" if role == "admin" else "m:")
            + (ui.store.callback_epoch if epoch is None else epoch)
            + ":"
            + data,
            message=Message(
                message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=uid, type=chat_type)
            ),
        ),
    )
    await ui.dispatcher.feed_update(ui.bots[role], update)


def texts(ui, role="model"):
    return "\n".join(
        call.text
        for call in ui.bots[role].session.calls
        if isinstance(call, SendMessage)
        and (int(call.chat_id) == 99 if role == "admin" else int(call.chat_id) != 99)
    )
