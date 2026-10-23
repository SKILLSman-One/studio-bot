import sqlite3
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import SendMessage
from conftest import click, message, texts

from studio_bot.config import UserError
from studio_bot.runtime import deliver_once
from studio_bot.store import SCHEMA, Store


async def confirm_access(ui, target, choice):
    await click(ui, f"accessask:{target}:{choice}", 99, "admin")
    token = ui.pending[("admin", 99)][3]
    await click(ui, f"accessconfirm:{token}", 99, "admin")
    return token


async def test_admin_ban_unban_preserves_profile_history_and_releases_resources(ui):
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "room:0", 10)
    now = ui.store.clock()
    bid = ui.store.book(10, "2026-10-19", 0, now + 1000, now + 2000)
    ui.store.decide_booking(bid, 99, room="4")
    aid = ui.store.advance(10, Decimal("20"), "USD", {"current": "251"})
    tid = ui.store.ticket(10, "Сохранить историю")
    await click(ui, "contact", 10)
    token = await confirm_access(ui, 10, "ban")
    assert ui.store.is_banned(10) and not ui.store.active_shifts()
    assert ui.store.user(10)["payroll_id"] is None
    assert ui.store.one("SELECT state FROM bookings WHERE id=?", (bid,))["state"] == "cancelled"
    assert ui.store.one("SELECT state FROM advances WHERE id=?", (aid,))["state"] == "pending"
    assert (
        ui.store.one("SELECT body FROM tickets WHERE id=?", (tid,))["body"] == "Сохранить историю"
    )
    assert ("model", 10) not in ui.pending
    await click(ui, f"accessconfirm:{token}", 99, "admin")
    assert ui.store.is_banned(10)
    await confirm_access(ui, 10, "unban")
    assert not ui.store.is_banned(10)
    assert ui.store.user(10)["payroll_id"] is None
    assert ui.store.user(10)["status"] == "approved"
    assert not ui.store.active_shifts()
    await click(ui, "salary:current", 10)
    assert "ещё не привязал" in texts(ui)
    assert (
        ui.store.one("SELECT count(*) n FROM audit WHERE action IN ('ban_person','unban_person')")[
            "n"
        ]
        == 2
    )


@pytest.mark.parametrize(
    "action", ["salary:current", "rooms", "week", "advance", "contact", "register", "rate", "home"]
)
async def test_banned_old_buttons_cannot_read_or_mutate(ui, action):
    ui.store.set_banned(10, True, 99)
    ui.payroll.get.reset_mock()
    await click(ui, action, 10)
    ui.payroll.get.assert_not_called()
    assert not ui.store.active_shifts()
    assert not ui.pending
    assert not texts(ui)


async def test_banned_account_cannot_reenter_admin_or_finish_input(ui):
    ui.settings.admin_ids = ()
    assert await ui.auth.login(10, "correct-password")
    assert 10 in ui.auth.recipients()
    ui.pending[("model", 10)] = ("name",)
    ui.store.set_banned(10, True, 99)
    assert not ui.auth.logged_in(10) and 10 not in ui.auth.recipients()
    assert not await ui.auth.login(10, "correct-password")
    for command in ("/start", "/admin", "/model", "/cancel", "Комментарий"):
        await message(ui, command, 10)
    await click(ui, "people:0", 10, "admin")
    assert ui.store.user(10)["name"] == "Аня"
    assert "заблокирован" in texts(ui)
    assert "Мария" not in texts(ui)


async def test_access_change_requires_admin_and_fresh_confirmation(ui):
    await click(ui, "accessask:20:ban", 10)
    await click(ui, "accessask:20:ban", 99, "admin")
    await click(ui, "accessconfirm:forged", 99, "admin")
    assert not ui.store.is_banned(20)
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "accessask:20:ban", 99, "admin")
    token = ui.pending[("admin", 99)][3]
    await click(ui, "person:20", 99, "admin")  # Cancel.
    await click(ui, f"accessconfirm:{token}", 99, "admin")
    assert not ui.store.is_banned(20)
    await click(ui, "accessask:20:ban", 99, "admin")
    token = ui.pending[("admin", 99)][3]
    now = ui.store.clock()
    ui.store.clock = lambda: now + 301
    await click(ui, f"accessconfirm:{token}", 99, "admin")
    assert not ui.store.is_banned(20)
    with pytest.raises(UserError):
        ui.store.set_banned(99, True, 99)


def test_migration_and_ban_survive_restart_with_released_binding(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO people VALUES (10,'Аня','','','approved','m10',1)")
    db.commit()
    db.close()
    store = Store(path)
    assert not store.is_banned(10)
    store.set_banned(10, True, 99)
    store.close()
    restored = Store(path)
    assert restored.is_banned(10)
    with pytest.raises(UserError):
        restored.approve_person(10, None, 99)
    with pytest.raises(UserError):
        restored.register(10, "Анна", "", "")
    with pytest.raises(UserError):
        restored.ticket(10, "Недоступно")
    restored.set_banned(10, False, 99)
    assert restored.approved(10)["payroll_id"] is None
    restored.register(30, "Новая модель", "", "")
    restored.approve_person(30, "m10", 99)
    assert restored.approved(30)["payroll_id"] == "m10"
    restored.close()


def test_upgrade_releases_aliases_of_already_banned_people(tmp_path):
    path = tmp_path / "previous.sqlite3"
    store = Store(path)
    store.register(10, "Аня", "", "")
    store.approve_person(10, "m10", 99)
    with store.db:
        store.db.execute("UPDATE people SET banned=1 WHERE user_id=10")
    epoch = store.callback_epoch
    store.close()
    restored = Store(path)
    assert restored.is_banned(10)
    assert restored.user(10)["payroll_id"] is None
    assert restored.callback_epoch == epoch
    restored.register(20, "Другая Аня", "", "")
    restored.approve_person(20, "m10", 99)
    restored.set_banned(10, False, 99)
    assert restored.user(10)["payroll_id"] is None
    restored.close()


async def test_ban_during_sheet_read_does_not_leak_salary_or_create_preview(ui):
    snapshot = ui.payroll.get.return_value

    async def read_then_ban(**kwargs):
        ui.store.set_banned(10, True, 99)
        return snapshot

    ui.payroll.get.side_effect = read_then_ban
    await click(ui, "salary:current", 10)
    assert "$125.50" not in texts(ui) and "$251" not in texts(ui) and "заблокирован" in texts(ui)
    ui.store.set_banned(10, False, 99)
    await click(ui, "advance", 10)
    assert ("model", 10) not in ui.pending


async def test_notifications_to_banned_users_are_cancelled_even_after_restart(people):
    people.set_banned(10, True, 99)
    with people.db:
        people.db.execute("DELETE FROM outbox")
        people.enqueue("model", 10, "Ответ")
        people.enqueue("admin", 10, "Новая заявка")
    bot = SimpleNamespace(send_message=AsyncMock())
    await deliver_once(people, {"model": bot, "admin": bot})
    bot.send_message.assert_not_called()
    assert all(row["state"] == "cancelled" for row in people.all("SELECT state FROM outbox"))


async def test_all_menus_and_advance_views_use_only_usd(ui):
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await message(ui, "/start", 30)
    await message(ui, "/start", 10)
    await click(ui, "home", 99, "admin")
    await click(ui, "advance", 10)
    await message(ui, "5000 RUB", 10)
    assert ui.pending[("model", 10)][0] == "advance_amount"
    await message(ui, "50 USD", 10)
    token = ui.pending[("model", 10)][1]
    await click(ui, f"advanceconfirm:{token}", 10)
    await click(ui, "myadvances", 10)
    await click(ui, "advanceitem:1", 99, "admin")
    for call in ui.bots["model"].session.calls:
        if isinstance(call, SendMessage):
            assert (
                "₽" not in call.text and "курс" not in call.text.lower() and "RUB" not in call.text
            )
            if call.reply_markup:
                assert all(
                    b.callback_data.split(":", 2)[2] != "rate"
                    for row in call.reply_markup.inline_keyboard
                    for b in row
                )
    with pytest.raises(UserError):
        ui.store.advance(20, Decimal(100), "RUB", {})
    assert ui.payroll.get.return_value.amount(
        "m20", (date(2026, 10, 1), date(2026, 10, 31)), gross=True
    ) == Decimal("1500")
