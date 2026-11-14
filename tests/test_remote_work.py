import sqlite3
from datetime import date

import pytest
from aiogram.methods import SendMediaGroup, SendMessage
from conftest import admin_session, click, message, texts

from studio_bot.config import Settings, UserError
from studio_bot.dates import visit_times
from studio_bot.presentation import REMOTE_ROOM
from studio_bot.store import SCHEMA, Store


async def test_remote_shifts_are_separate_and_finish_without_cleaning(ui):
    await click(ui, "rooms", 10)
    assert any(
        button.text == "💻 Начать смену — дистанционно"
        for call in ui.bots["model"].session.calls
        if isinstance(call, SendMessage) and call.reply_markup
        for row in call.reply_markup.inline_keyboard
        for button in row
    )
    await click(ui, "remote", 10)
    await click(ui, "remote", 20)
    await click(ui, "room:0", 10)
    shifts = ui.store.active_shifts()
    assert {(s["user_id"], s["room"]) for s in shifts} == {(10, REMOTE_ROOM), (20, REMOTE_ROOM)}
    admin_session(ui)
    await click(ui, "rooms", 99, "admin")
    admin_text = texts(ui, "admin")
    assert all(s in admin_text for s in ("Дистанционные смены", "Аня", "Мария", "4-я комната: свободна"))
    sid = next(s["id"] for s in shifts if s["user_id"] == 10)
    other_sid = next(s["id"] for s in shifts if s["user_id"] == 20)
    await click(ui, f"end:{sid}", 20)
    assert len(ui.store.active_shifts()) == 2
    await click(ui, f"end:{sid}", 10)
    await click(ui, f"adminend:{other_sid}", 99, "admin")
    assert not ui.store.active_shifts()
    assert not any(isinstance(c, SendMediaGroup) for c in ui.bots["model"].session.calls)
    assert "Дистанционная смена завершена" in texts(ui)
    assert ui.store.one("SELECT clean_confirmed_at FROM shifts WHERE id=?", (sid,))[0] is None
    assert not ui.store.room_usage(ui.store.clock() - 3600, ui.store.clock() + 3600)


async def test_remote_booking_appears_in_model_and_admin_schedule(ui):
    await click(ui, "day:2026-10-19", 10)
    await message(ui, "14:00–20:00", 10)
    draft = ui.pending[("model", 10)][1]
    await click(ui, f"bookremote:{draft['token']}", 10)
    confirm = ui.pending[("model", 10)][1]
    assert not ui.store.all("SELECT * FROM bookings")
    await click(ui, f"bookconfirm:{confirm['token']}", 10)
    await click(ui, "mybookings:0", 10)
    admin_session(ui)
    await click(ui, "calendarview:10:0", 99, "admin")
    assert "💻 Дистанционно" in texts(ui, "admin")
    assert "💻 Дистанционно" in texts(ui)
    row = ui.store.one("SELECT * FROM bookings")
    assert row["state"] == "approved" and row["room"] == REMOTE_ROOM
    assert not ui.store.room_conflicts("4", row["started"], row["ended"])
    _, state = ui.store.book_room(
        20, row["day"], REMOTE_ROOM, row["started"], row["ended"], ui.settings
    )
    assert state == "approved"
    with pytest.raises(UserError, match="пересекающийся|на это время"):
        ui.store.book_room(10, row["day"], "4", row["started"], row["ended"], ui.settings)


async def test_admin_can_resolve_a_room_conflict_as_remote(ui):
    day = date(2026, 10, 19)
    start, end = visit_times(day, "14:00–20:00", ui.settings.zone)
    original, _ = ui.store.book_room(10, str(day), "4", start, end, ui.settings)
    conflict, state = ui.store.book_room(20, str(day), "4", start, end, ui.settings)
    assert state == "conflict"
    admin_session(ui)
    await click(ui, f"approvebook:{conflict}:remote", 99, "admin")
    bookings = {r["id"]: (r["room"], r["state"]) for r in ui.store.all("SELECT * FROM bookings")}
    assert bookings == {original: ("4", "approved"), conflict: (REMOTE_ROOM, "approved")}


def test_old_room_index_migrates_without_changing_existing_shift(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("DROP INDEX occupied_studio_room")
        db.execute("CREATE UNIQUE INDEX occupied_room ON shifts(room) WHERE ended IS NULL")
        db.executemany(
            "INSERT INTO people VALUES (?,?,?,?,?,?,?)",
            [(uid, name, "", "", "approved", None, 1) for uid, name in ((10, "Аня"), (20, "Маша"), (30, "Оля"))],
        )
        db.execute("INSERT INTO shifts VALUES (1,10,'4',123,NULL,NULL)")
    store = Store(path)
    try:
        assert store.one("SELECT started FROM shifts WHERE id=1")[0] == 123
        for uid in (20, 30):
            with pytest.raises(UserError, match="Комната уже занята"):
                store.start_shift(uid, "4", ("4",))
            store.start_shift(uid, REMOTE_ROOM, ("4",))
        assert len(store.active_shifts()) == 3
        assert not store.all("PRAGMA foreign_key_check")
    finally:
        store.close()


def test_pending_or_banned_person_cannot_start_or_book_remotely(people, tmp_path):
    settings = Settings(tmp_path)
    people.register(30, "Новая", "", "")
    people.set_banned(20, True, 99)
    day = date(2026, 10, 19)
    start, end = visit_times(day, "14:00–20:00", settings.zone)
    for uid in (20, 30):
        with pytest.raises(UserError):
            people.start_shift(uid, REMOTE_ROOM, settings.rooms)
        with pytest.raises(UserError):
            people.book_room(uid, str(day), REMOTE_ROOM, start, end, settings)
