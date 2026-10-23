from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import click, message, texts

from studio_bot.config import UserError
from studio_bot.runtime import deliver_once
from studio_bot.store import Store


def admin(ui):
    ui.auth.sessions[99] = ui.store.clock() + 3600


async def preview(ui, text="🗓 Заполните расписание\n\nДо пятницы ♡"):
    await click(ui, "announcement", 99, "admin")
    await message(ui, text, 99, "admin")
    return ui.pending[("admin", 99)][1]


async def test_preview_requires_confirmation_and_targets_only_active_employees(ui):
    admin(ui)
    ui.store.register(30, "Ожидает", "", "")
    ui.store.register(99, "Администратор", "", "")
    ui.store.approve_person(99, None, 99)
    ui.store.set_banned(20, True, 99)
    before = ui.store.one("SELECT count(*) n FROM outbox")["n"]
    draft = await preview(ui)
    assert draft["recipients"] == (10,)
    assert ui.store.one("SELECT count(*) n FROM outbox")["n"] == before
    assert "Получателей: 1" in texts(ui, "admin")
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    rows = ui.store.all("SELECT * FROM outbox WHERE announcement_id IS NOT NULL")
    assert len(rows) == 1 and rows[0]["user_id"] == 10
    assert rows[0]["text"] == draft["text"] and rows[0]["state"] == "pending"
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    assert len(ui.store.all("SELECT * FROM announcements")) == 1
    assert "В очереди: 1" in texts(ui, "admin")


@pytest.mark.parametrize("role,uid", [("admin", 10), ("model", 10), ("admin", 99)])
async def test_announcement_routes_are_admin_only(ui, role, uid):
    for callback in ("announcement", "announcementconfirm:x", "announcementhistory:0", "announcementreport:1"):
        await click(ui, callback, uid, role)
    assert not ui.store.all("SELECT * FROM announcements")
    assert not any(state[0] == "announcement_text" for state in ui.pending.values())


async def test_confirmation_rechecks_recipients_and_does_not_add_unseen_newcomers(ui):
    admin(ui)
    draft = await preview(ui, "Точный текст <без HTML> https://example.com")
    ui.store.set_banned(20, True, 99)
    ui.store.register(30, "Новая", "", "")
    ui.store.approve_person(30, None, 99)
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    assert [r["user_id"] for r in ui.store.all("SELECT * FROM outbox WHERE announcement_id IS NOT NULL")] == [10]


async def test_cancel_edit_timeout_and_logout_invalidate_confirmation(ui):
    admin(ui)
    draft = await preview(ui, "Первый текст")
    await message(ui, "Исправленный текст", 99, "admin")
    edited = ui.pending[("admin", 99)][1]
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    assert not ui.store.all("SELECT * FROM announcements")
    await message(ui, "/cancel", 99, "admin")
    await click(ui, f"announcementconfirm:{edited['token']}", 99, "admin")
    assert not ui.store.all("SELECT * FROM announcements")
    draft = await preview(ui)
    now = ui.store.clock()
    ui.store.clock = lambda: now + 301
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    assert not ui.store.all("SELECT * FROM announcements")
    draft = await preview(ui)
    ui.auth.logout(99)
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    assert not ui.store.all("SELECT * FROM announcements")


async def test_no_recipients_and_telegram_length_limit(ui):
    admin(ui)
    await click(ui, "announcement", 99, "admin")
    await message(ui, "😀" * 1751, 99, "admin")
    assert ui.pending[("admin", 99)][0] == "announcement_text"
    assert "3500" in texts(ui, "admin")
    await message(ui, "😀" * 1750, 99, "admin")
    assert ui.pending[("admin", 99)][0] == "announcement_confirm"
    ui.store.set_banned(10, True, 99)
    ui.store.set_banned(20, True, 99)
    draft = ui.pending[("admin", 99)][1]
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    assert not ui.store.all("SELECT * FROM announcements")
    assert "Получателей нет" in texts(ui, "admin")


async def test_report_tracks_delivery_and_ban_skips_queued_recipient(ui):
    admin(ui)
    draft = await preview(ui)
    await click(ui, f"announcementconfirm:{draft['token']}", 99, "admin")
    aid = ui.store.one("SELECT id FROM announcements")["id"]
    with ui.store.db:
        ui.store.db.execute("DELETE FROM outbox WHERE announcement_id IS NULL")
    ui.store.set_banned(20, True, 99)
    bot = SimpleNamespace(send_message=AsyncMock())
    await deliver_once(ui.store, {"model": bot})
    bot.send_message.assert_awaited_once_with(10, draft["text"], parse_mode=None)
    await click(ui, f"announcementreport:{aid}", 99, "admin")
    assert "Отправлено: 1\nВ очереди: 0\nОшибки: 0\nПропущено: 1" in texts(ui, "admin")
    await click(ui, "announcementhistory:0", 99, "admin")
    assert "История объявлений" in texts(ui, "admin")


def test_queue_idempotency_and_reports_survive_restart_and_reset(people, tmp_path):
    aid = people.announce(99, "stable-token", "Текст", (10, 20))
    assert people.announce(99, "stable-token", "Текст", (10, 20)) == aid
    with pytest.raises(UserError):
        people.announce(98, "stable-token", "Текст", (10, 20))
    path = tmp_path / "restored.sqlite3"
    import sqlite3

    with sqlite3.connect(path) as copy:
        people.db.backup(copy)
    restored = Store(path)
    assert restored.announce(99, "stable-token", "Текст", (10, 20)) == aid
    assert len(restored.all("SELECT * FROM outbox WHERE announcement_id=?", (aid,))) == 2
    epoch = restored.callback_epoch
    restored.clear_test_history()
    assert not restored.all("SELECT * FROM announcements")
    assert not restored.all("SELECT * FROM outbox")
    assert restored.callback_epoch != epoch
    restored.close()
