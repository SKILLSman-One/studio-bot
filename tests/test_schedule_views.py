from datetime import datetime, timedelta

from aiogram.methods import SendMessage
from conftest import click


def add_booking(ui, uid, day, room="4", overnight=False, state="approved"):
    start = datetime(2026, 10, day, 18 if overnight else 14, tzinfo=ui.settings.zone)
    end = start + timedelta(hours=12 if overnight else 6)
    with ui.store.db:
        return ui.store.db.execute(
            "INSERT INTO bookings(user_id,day,slot,started,ended,state,room) VALUES (?,?,0,?,?,?,?)",
            (uid, start.date().isoformat(), start.timestamp(), end.timestamp(), state, room),
        ).lastrowid


def last_message(ui, uid):
    return next(
        call
        for call in reversed(ui.bots["model"].session.calls)
        if isinstance(call, SendMessage) and int(call.chat_id) == uid
    )


def buttons(message):
    return [button for row in message.reply_markup.inline_keyboard for button in row]


async def test_model_schedule_is_private_and_overnight_date_is_not_repeated(ui):
    bid = add_booking(ui, 10, 19, "10", overnight=True)
    other = add_booking(ui, 20, 20, "15")
    await click(ui, "mybookings:0")
    result = last_message(ui, 10)
    assert result.text.count("19.10") == 1
    assert "20.10" not in result.text
    assert "18:00–06:00 · на следующий день" in result.text
    assert "\n\n📅 Пн, 19.10\n" in result.text
    assert "10-я комната" in result.text
    assert all(value not in result.text for value in ("Аня", "Мария", "подтверждено", "#"))
    cancel = next(
        button for button in buttons(result) if button.callback_data.endswith(f"cancelbook:{bid}")
    )
    assert "19.10 18:00" in cancel.text and "#" not in cancel.text
    await click(ui, f"cancelbook:{bid}")
    assert ui.store.one("SELECT state FROM bookings WHERE id=?", (bid,))["state"] == "cancelled"
    assert ui.store.one("SELECT state FROM bookings WHERE id=?", (other,))["state"] == "approved"
    assert "отменено" in last_message(ui, 10).text


async def test_admin_selects_person_before_seeing_schedule_and_keeps_overview(ui):
    add_booking(ui, 10, 19, "10")
    add_booking(ui, 20, 20, "15")
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "calendar", 99, "admin")
    chooser = last_message(ui, 99)
    assert chooser.text == "🗓 Чьё расписание хотим посмотреть?"
    assert "14:00" not in chooser.text
    assert {button.text for button in buttons(chooser)} >= {"🗓 Аня", "🗓 Мария"}
    await click(ui, "calendarview:10:0", 99, "admin")
    selected = last_message(ui, 99)
    assert selected.text.count("Аня") == 1
    assert "10-я комната" in selected.text
    assert "Мария" not in selected.text and "15-я комната" not in selected.text
    await click(ui, "calendarview:0:0", 99, "admin")
    assert "Аня" in last_message(ui, 99).text and "Мария" in last_message(ui, 99).text


async def test_admin_calendar_pages_people_and_bookings_and_handles_empty_schedule(ui):
    for uid in range(30, 40):
        ui.store.register(uid, f"Модель {uid}", "", "")
        ui.store.approve_person(uid, f"m{uid}", 99)
    with ui.store.db:
        ui.store.db.execute("UPDATE people SET banned=1 WHERE user_id=39")
    ui.store.register(40, "Неподтверждённая", "", "")
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "calendar:0", 99, "admin")
    first = last_message(ui, 99)
    first_ids = {
        button.callback_data
        for button in buttons(first)
        if "calendarview:" in button.callback_data
        and not button.callback_data.endswith("calendarview:0:0")
    }
    assert len(first_ids) == 8
    await click(ui, "calendar:1", 99, "admin")
    second = last_message(ui, 99)
    second_ids = {
        button.callback_data
        for button in buttons(second)
        if "calendarview:" in button.callback_data
        and not button.callback_data.endswith("calendarview:0:0")
    }
    assert len(second_ids) == 3 and first_ids.isdisjoint(second_ids)
    assert all(":39:" not in value and ":40:" not in value for value in first_ids | second_ids)
    await click(ui, "calendarview:30:0", 99, "admin")
    assert "Предстоящих записей пока нет" in last_message(ui, 99).text
    for day in range(19, 28):
        add_booking(ui, 10, day)
    await click(ui, "calendarview:10:0", 99, "admin")
    assert last_message(ui, 99).text.count("📅") == 8
    assert any(
        button.callback_data.endswith("calendarview:10:1")
        for button in buttons(last_message(ui, 99))
    )
    await click(ui, "calendarview:10:1", 99, "admin")
    assert last_message(ui, 99).text.count("📅") == 1


async def test_calendar_callbacks_do_not_expose_another_model_schedule(ui):
    add_booking(ui, 20, 20, "15")
    await click(ui, "calendarview:20:0", 10, "model")
    await click(ui, "calendarview:20:0", 10, "admin")
    sent = [call for call in ui.bots["model"].session.calls if isinstance(call, SendMessage)]
    assert not any("15-я комната" in call.text or "Мария" in call.text for call in sent)
    ui.auth.sessions[99] = ui.store.clock() + 3600
    with ui.store.db:
        ui.store.db.execute("UPDATE people SET banned=1 WHERE user_id=20")
    await click(ui, "calendarview:20:0", 99, "admin")
    assert "Сотрудник недоступен" in last_message(ui, 99).text


async def test_unconfirmed_booking_is_not_presented_as_a_confirmed_reservation(ui):
    add_booking(ui, 10, 19, state="conflict")
    await click(ui, "mybookings:0")
    assert "брони пока нет" in last_message(ui, 10).text
