from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest
from aiogram.methods import SendMessage
from conftest import click, texts

from studio_bot import __version__
from studio_bot.config import UserError
from studio_bot.payroll import Entry, SheetsPayroll, Snapshot


def buttons(ui, uid=99):
    calls = [
        call
        for call in ui.bots["admin"].session.calls
        if isinstance(call, SendMessage) and int(call.chat_id) == uid
    ]
    return [
        (b.text, b.callback_data.split(":", 2)[2])
        for row in calls[-1].reply_markup.inline_keyboard
        for b in row
    ]


def admin(ui, uid=99):
    ui.auth.sessions[uid] = ui.store.clock() + 3600


def free_anna(ui):
    with ui.store.db:
        ui.store.db.execute("UPDATE people SET payroll_id=NULL WHERE user_id=10")


async def select_first(ui, target=30, uid=99):
    await click(ui, f"bindstart:{target}", uid, "admin")
    token = ui.pending[("admin", uid)][1]["token"]
    await click(ui, f"bindpick:{token}:0", uid, "admin")
    return token


async def test_main_menu_access_lists_pagination_and_self_exclusion(ui):
    admin(ui)
    await click(ui, "home", 99, "admin")
    assert ("🔒 Заблокировать", "accesslist:ban:0") in buttons(ui)
    assert ("🔓 Разблокировать", "accesslist:unban:0") in buttons(ui)
    assert ("🩺 Состояние бота", "health") in buttons(ui)
    ui.store.register(99, "Администратор", "", "")
    for i in range(30, 39):
        ui.store.register(i, f"Модель {i}", "", "")
    ui.store.set_banned(20, True, 99)
    await click(ui, "accesslist:ban:0", 99, "admin")
    options = buttons(ui)
    assert len([cb for _, cb in options if cb.startswith("accessask:")]) == 8
    assert all(cb not in {"accessask:99:ban", "accessask:20:ban"} for _, cb in options)
    await click(ui, "accesslist:ban:1", 99, "admin")
    assert len([cb for _, cb in buttons(ui) if cb.startswith("accessask:")]) == 2
    await click(ui, "accesslist:unban:0", 99, "admin")
    assert ("Мария", "accessask:20:unban") in buttons(ui)
    await click(ui, "accessask:20:unban", 99, "admin")
    token = ui.pending[("admin", 99)][3]
    await click(ui, f"accessconfirm:{token}", 99, "admin")
    await click(ui, "accesslist:unban:0", 99, "admin")
    assert "Подходящих сотрудников нет" in texts(ui, "admin")


async def test_binding_buttons_confirm_without_exposing_ids(ui):
    admin(ui)
    free_anna(ui)
    ui.store.register(30, "Ольга", "", "")
    token = await select_first(ui)
    assert ui.store.user(30)["payroll_id"] is None
    assert "Сотрудник: Ольга\nВ таблице: Аня" in texts(ui, "admin")
    assert "m10" not in texts(ui, "admin") and "model_id" not in texts(ui, "admin")
    ui.payroll.get.reset_mock()
    await click(ui, f"bindconfirm:{token}", 99, "admin")
    ui.payroll.get.assert_awaited_once_with(force=True)
    assert ui.store.approved(30)["payroll_id"] == "m10"
    await click(ui, f"bindconfirm:{token}", 99, "admin")
    assert ui.store.approved(30)["payroll_id"] == "m10"


async def test_binding_hides_duplicates_and_supports_pages(ui):
    admin(ui)
    ui.store.register(30, "Ольга", "", "")
    entries = [
        Entry(str(i), date(2026, 10, 1), f"x{i}", f"Имя {i:02}", Decimal(0)) for i in range(10)
    ]
    entries += [
        Entry("a", date(2026, 10, 1), "a", "Дубль", Decimal(0)),
        Entry("b", date(2026, 10, 1), "b", " дубль ", Decimal(0)),
    ]
    ui.payroll.get.return_value = Snapshot(tuple(entries), datetime.now(timezone.utc))
    await click(ui, "bindstart:30", 99, "admin")
    state = ui.pending[("admin", 99)][1]
    assert len(state["candidates"]) == 10
    assert "Совпадающие псевдонимы скрыты" in texts(ui, "admin")
    assert len([cb for _, cb in buttons(ui) if cb.startswith("bindpick:")]) == 8
    await click(ui, f"bindpage:{state['token']}:1", 99, "admin")
    assert len([cb for _, cb in buttons(ui) if cb.startswith("bindpick:")]) == 2
    await click(ui, "person:30", 99, "admin")
    await click(ui, f"bindpick:{state['token']}:0", 99, "admin")
    assert ui.store.user(30)["payroll_id"] is None


@pytest.mark.parametrize(
    "change", ["removed", "renamed", "duplicate", "banned", "expired", "network"]
)
async def test_binding_revalidates_at_confirmation(ui, change):
    admin(ui)
    free_anna(ui)
    ui.store.register(30, "Ольга", "", "")
    token = await select_first(ui)
    original = ui.payroll.get.return_value
    if change in {"removed", "renamed", "duplicate"}:
        entries = [e for e in original.entries if e.model_id != "m10"]
        if change != "removed":
            entries.append(
                Entry(
                    "x",
                    date(2026, 10, 1),
                    "m10",
                    "Новое имя" if change == "renamed" else "Аня",
                    Decimal(0),
                )
            )
        if change == "duplicate":
            entries.append(Entry("y", date(2026, 10, 1), "other", "Аня", Decimal(0)))
        ui.payroll.get.return_value = Snapshot(tuple(entries), original.fetched_at)
    elif change == "banned":
        ui.store.set_banned(30, True, 99)
    elif change == "expired":
        now = ui.store.clock()
        ui.store.clock = lambda: now + 301
    else:
        ui.payroll.get.side_effect = UserError("Таблица недоступна")
    await click(ui, f"bindconfirm:{token}", 99, "admin")
    assert ui.store.user(30)["payroll_id"] is None


async def test_two_admins_cannot_bind_one_row_to_two_people(ui):
    ui.settings.admin_ids = ()
    admin(ui, 99)
    admin(ui, 98)
    free_anna(ui)
    for uid in (30, 40):
        ui.store.register(uid, f"Сотрудник {uid}", "", "")
    first = await select_first(ui, 30, 99)
    second = await select_first(ui, 40, 98)
    await click(ui, f"bindconfirm:{first}", 99, "admin")
    await click(ui, f"bindconfirm:{second}", 98, "admin")
    assert ui.store.user(30)["payroll_id"] == "m10"
    assert ui.store.user(40)["payroll_id"] is None


async def test_new_admin_tools_are_private(ui):
    for action in ("health", "accesslist:ban:0", "accesslist:unban:0", "bindstart:20"):
        await click(ui, action, 10)
        await click(ui, action, 99, "admin")
    ui.payroll.get.assert_not_called()
    assert not ui.store.is_banned(10)
    assert "Мария" not in texts(ui)


async def test_health_refreshes_preserves_last_success_and_hides_salary(ui):
    admin(ui)
    snapshot = ui.payroll.get.return_value
    ui.payroll = SheetsPayroll(ui.settings)
    ui.payroll._read_sync = Mock(return_value=snapshot)
    await click(ui, "health", 99, "admin")
    assert f"Версия: {__version__}" in texts(ui, "admin")
    assert "2026-10" in texts(ui, "admin")
    assert "$" not in texts(ui, "admin") and "Аня" not in texts(ui, "admin")
    success = ui.payroll.last_success
    ui.payroll._read_sync.side_effect = RuntimeError("SECRET must not be displayed")
    ui.bots["admin"].session.calls.clear()
    await click(ui, "health", 99, "admin")
    assert ui.payroll.last_success == success and ui.payroll.cache is None
    assert "ошибка" in texts(ui, "admin") and "SECRET" not in texts(ui, "admin")
    assert "2026-10" in texts(ui, "admin") and "проверить не удалось" in texts(ui, "admin")
    ui.payroll._read_sync.side_effect = None
    ui.payroll._read_sync.return_value = Snapshot(
        (Entry("old", date(2026, 9, 1), "x", "Test", Decimal(0)),), snapshot.fetched_at
    )
    await click(ui, "health", 99, "admin")
    assert "Данных за текущий месяц нет" in texts(ui, "admin")
    assert ui.payroll.last_error is None and ui.payroll._read_sync.call_count == 3
