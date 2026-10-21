import json
from datetime import date

import pytest
from aiogram.methods import SendDocument, SendMessage
from conftest import click, message, texts

from studio_bot.auth import AdminAuth
from studio_bot.config import UserError
from studio_bot.dates import next_week


async def test_binding_exact_alias_requires_confirmation(ui):
    ui.store.register(30, "Новая анкета", "", "")
    with ui.store.db:
        ui.store.db.execute("UPDATE people SET payroll_id=NULL WHERE user_id=10")
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "bindstart:30", 99, "admin")
    state = ui.pending[("admin", 99)][1]
    await click(ui, f"bindpick:{state['token']}:0", 99, "admin")
    assert ui.pending[("admin", 99)][1]["selected"] == ("m10", "Аня")
    assert ui.store.user(30)["payroll_id"] is None


async def test_advance_with_missing_source_period_is_not_created(ui):
    from datetime import datetime, timezone

    from studio_bot.payroll import Snapshot

    ui.settings.payroll_period = "semimonthly"
    old = ui.payroll.get.return_value
    ui.payroll.get.return_value = Snapshot(
        old.entries, datetime.now(timezone.utc), ((date(2026, 10, 16), date(2026, 10, 31)),)
    )
    await click(ui, "advance")
    assert "Нет подключённых данных" in texts(ui)
    assert ("model", 10) not in ui.pending
    assert ui.store.one("SELECT count(*) n FROM advances")["n"] == 0


@pytest.mark.parametrize("uid,chat_type", [(10, "private"), (99, "group")])
async def test_admin_access_denied_before_password_and_export(ui, uid, chat_type):
    await message(ui, "/admin", uid, "admin", chat_type)
    for data in ("people:0", "salary:current", "advanceitem:1", "rooms", "tickets:0"):
        await click(ui, data, uid, "admin", chat_type)
    assert not any(
        isinstance(c, (SendMessage, SendDocument)) for c in ui.bots["admin"].session.calls
    )
    ui.payroll.get.assert_not_called()


async def test_allowlisted_admin_needs_password_and_session_expires(ui):
    await click(ui, "salary:current", 99, "admin")
    ui.payroll.get.assert_not_called()
    await message(ui, "correct-password", 99, "admin")
    assert ui.auth.logged_in(99)
    await click(ui, "salary:current", 99, "admin")
    assert "Мария" in texts(ui, "admin") and "251.00" in texts(ui, "admin")
    ui.auth.sessions[99] = ui.store.clock() - 1
    ui.payroll.get.reset_mock()
    await click(ui, "salary:current", 99, "admin")
    ui.payroll.get.assert_not_called()


async def test_password_lockout_survives_auth_restart(ui):
    for _ in range(5):
        assert not await ui.auth.login(99, "wrong")
    auth = AdminAuth(ui.store, ui.settings, clock=ui.store.clock)
    assert not await auth.login(99, "correct-password")
    base = ui.store.clock()
    auth.clock = lambda: base + 901
    assert await auth.login(99, "correct-password")
    assert not await auth.login(10, "correct-password")


async def test_model_salary_is_private_and_admin_callback_cannot_bypass(ui):
    await click(ui, "salary:current", 10)
    own = texts(ui)
    assert "125.50" in own and "Мария" not in own and "900.00" not in own
    await click(ui, "people:0", 10)
    await click(ui, "advanceitem:1", 10)
    assert "Мария" not in texts(ui)
    await click(ui, "rooms", 10)
    assert ui.store.active_shifts() == []


async def test_form_admin_binding_confirm_and_replay(ui):
    await click(ui, "register", 30)
    await message(ui, "Ольга", 30)
    assert ui.store.user(30)["status"] == "pending"
    assert ui.store.user(30)["note"] == ""
    assert ("model", 30) not in ui.pending
    assert "опыт" not in texts(ui).lower() and "комментарий" not in texts(ui).lower()
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "person:30", 99, "admin")
    await click(ui, "bindstart:30", 99, "admin")
    assert not ui.pending[("admin", 99)][1]["candidates"]
    await click(ui, "bind:30", 99, "admin")
    assert ui.store.user(30)["status"] == "pending"  # m10 already belongs to user 10.
    assert "Свободных однозначных записей нет" in texts(ui, "admin")
    await click(ui, "rejectperson:30", 99, "admin")
    assert ui.store.user(30)["status"] == "rejected"


async def test_full_shift_booking_advance_and_admin_reply(ui):
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "room:0")
    await click(ui, "rooms", 99, "admin")
    assert "Аня" in texts(ui, "admin")
    await click(ui, "end:1")
    token = ui.pending[("model", 10)][2]
    await click(ui, f"cleanconfirm:{token}")
    assert not ui.store.active_shifts()
    monday = next_week(date(2026, 10, 16))[0]
    await click(ui, "week")
    await click(ui, f"day:{monday}")
    await message(ui, "12:00–18:00")
    token = ui.pending[("model", 10)][1]["token"]
    await click(ui, f"bookroom:{token}:0")
    token = ui.pending[("model", 10)][1]["token"]
    await click(ui, f"bookconfirm:{token}")
    await click(ui, "calendar", 99, "admin")
    assert ui.store.one("SELECT room FROM bookings WHERE id=1")["room"] == "4"
    await click(ui, "advance")
    await message(ui, "50 USD")
    token = ui.pending[("model", 10)][1]
    await click(ui, f"advanceconfirm:{token}")
    await click(ui, f"advanceconfirm:{token}")
    assert ui.store.one("SELECT count(*) n FROM advances")["n"] == 1
    data = json.loads(ui.store.one("SELECT snapshot FROM advances")["snapshot"])
    assert data["current"] == "125.50" and data["previous"] == "200"
    await click(ui, "advanceitem:1", 99, "admin")
    await click(ui, "decideadvance:1:approve", 99, "admin")
    await click(ui, "askpaid:1", 99, "admin")
    await click(ui, "decideadvance:1:paid", 99, "admin")
    await click(ui, "myadvances")
    assert "выплачено" in texts(ui)
    await click(ui, "contact")
    await message(ui, "Уточните время съёмки")
    await click(ui, "ticketitem:1", 99, "admin")
    await click(ui, "reply:1", 99, "admin")
    await message(ui, "В 12:00", 99, "admin")
    assert ui.store.one("SELECT answer FROM tickets")["answer"] == "В 12:00"
    await click(ui, "usage", 99, "admin")
    await click(ui, "delivery", 99, "admin")


async def test_stale_week_buttons_cannot_create_old_booking(ui):
    await click(ui, "slot:2026-10-12:2")
    assert ui.store.one("SELECT count(*) n FROM bookings")["n"] == 0


async def test_unavailable_table_blocks_advance_not_other_features(ui):
    ui.payroll.get.side_effect = UserError("Источник недоступен")
    await click(ui, "advance")
    assert ("model", 10) not in ui.pending
    assert not ui.store.all("SELECT * FROM advances")
    await click(ui, "room:0")
    assert len(ui.store.active_shifts()) == 1


async def test_admin_logout_invalidates_old_buttons(ui):
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "logout", 99, "admin")
    await click(ui, "salary:current", 99, "admin")
    ui.payroll.get.assert_not_called()


async def test_profile_without_salary_binding_has_rooms_but_cannot_read_any_payroll(ui):
    ui.store.register(30, "Ольга", "", "")
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "approveprofile:30", 99, "admin")
    await click(ui, "room:0", 30)
    assert ui.store.active_shifts()[0]["user_id"] == 30
    await click(ui, "salary:current", 30)
    await click(ui, "advance", 30)
    ui.payroll.get.assert_not_called()
    assert "Мария" not in texts(ui)
    ui.store.approve_person(30, "m30", 99)
    assert ui.store.user(30)["payroll_id"] == "m30"
