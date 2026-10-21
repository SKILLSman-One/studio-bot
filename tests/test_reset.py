import sqlite3

import pytest
from aiogram.methods import AnswerCallbackQuery
from conftest import click, message

from studio_bot.config import UserError
from studio_bot.lock import InstanceLock
from studio_bot.maintenance import reset_test_history
from studio_bot.store import Store


async def test_old_buttons_cannot_touch_new_records_after_reset(ui):
    old = ui.store.callback_epoch
    ui.store.clear_test_history()
    assert old != ui.store.callback_epoch
    ui.store.register(10, "Новая Аня", "", "")
    ui.store.approve_person(10, "m10", 99)
    await click(ui, "room:0", 10, epoch=old)
    assert not ui.store.active_shifts()
    assert any(
        isinstance(call, AnswerCallbackQuery) and "устарела" in (call.text or "")
        for call in ui.bots["model"].session.calls
    )
    await message(ui, "/start", 10)
    await click(ui, "room:0", 10)
    assert ui.store.active_shifts()


def test_verified_backup_reset_preserves_config_and_clears_every_record(tmp_path):
    root = tmp_path
    store = Store(root / "data" / "studio.sqlite3")
    store.register(10, "Тест", "", "")
    store.approve_person(10, "m10", 99)
    store.ticket(10, "Тестовое обращение")
    with store.db:
        store.db.execute("INSERT INTO login_attempts VALUES (99,1,1,0)")
        store.db.execute("INSERT INTO admin_recipients VALUES (99,'test')")
    epoch = store.callback_epoch
    store.close()
    reopened = Store(root / "data" / "studio.sqlite3")
    assert reopened.callback_epoch == epoch
    reopened.close()
    files = (
        root / ".env",
        root / "studio.toml",
        root / "data/google-service-account.json",
        root / "data/admin-password.txt",
    )
    for path in files:
        path.write_text("synthetic-preserved-value", encoding="utf-8")
    backup = reset_test_history(root)
    assert backup.parent == root / "backups"
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("SELECT count(*) FROM people").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    clean = Store(root / "data" / "studio.sqlite3")
    for table in (
        "people",
        "shifts",
        "bookings",
        "advances",
        "tickets",
        "outbox",
        "audit",
        "admin_recipients",
        "login_attempts",
    ):
        assert clean.one(f"SELECT count(*) n FROM {table}")["n"] == 0
    assert clean.callback_epoch != epoch
    clean.register(10, "Сотрудник", "", "")
    assert clean.user(10)["status"] == "pending"
    clean.close()
    assert all(path.read_text(encoding="utf-8") == "synthetic-preserved-value" for path in files)


def test_reset_requires_stopped_instance_and_valid_backup(tmp_path):
    path = tmp_path / "data/studio.sqlite3"
    store = Store(path)
    store.register(10, "Тест", "", "")
    store.close()
    with InstanceLock(tmp_path / "data/app.lock"):
        with pytest.raises(UserError, match="уже запущен"):
            reset_test_history(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO shifts(user_id,room,started) VALUES (404,'4',1)")
    with pytest.raises(UserError, match="Связи резервной копии"):
        reset_test_history(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM people").fetchone()[0] == 1


def test_reset_is_atomic_on_delete_failure(people):
    people.ticket(10, "Сохранить")
    old = people.callback_epoch
    people.db.execute(
        "CREATE TRIGGER prevent_reset BEFORE DELETE ON people BEGIN SELECT RAISE(ABORT, 'test'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        people.clear_test_history()
    assert people.user(10) is not None
    assert people.one("SELECT count(*) n FROM tickets")["n"] == 1
    assert people.callback_epoch == old
