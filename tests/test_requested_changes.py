import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from aiogram.methods import SendDocument, SendMessage
from conftest import click, message, texts

from studio_bot.config import UserError
from studio_bot.payroll import Entry, Snapshot, report_csv
from studio_bot.ui import UI


async def test_admin_gross_and_model_share_usd_without_technical_details(ui):
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "salary:current", 99, "admin")
    report = texts(ui, "admin")
    assert "Полный оборот" in report
    assert "$251.00" in report and "$1 500.00" in report and "$1 751.00" in report
    assert "₽" not in report and "курс" not in report.lower()
    assert "$900.00" not in report
    await click(ui, "salary:current", 10)
    own = texts(ui)
    assert own == "Заработок: 01.10.2026 — 16.10.2026\n\nАня: $125.50"
    assert "$251.00" not in own and "Мария" not in own
    assert "Таблица" not in report and "процент" not in report
    await click(ui, "rate", 10)
    assert "Курс" not in texts(ui)


async def test_advance_share_usd_and_historical_snapshot_without_conversion(ui):
    await click(ui, "advance", 10)
    data = ui.pending[("model", 10)][1]
    assert data["current"] == "125.50" and data["previous"] == "200"
    assert not any("rate" in key for key in data)
    old = dict(data, current="125.50", rate="90", rate_day="2026-10-17")
    text = UI.snapshot_text(old)
    assert "$125.50" in text and "₽" not in text and "курс" not in text.lower()
    assert "Таблица" not in text and "процент" not in text and "UTC" not in text


async def test_model_individual_percentage_is_used(ui):
    await click(ui, "salary:current", 20)
    assert texts(ui) == "Заработок: 01.10.2026 — 16.10.2026\n\nМария: $900.00"


def test_gross_csv_and_missing_gross_fail_closed(ui):
    snapshot = ui.payroll.get.return_value
    raw = report_csv(
        snapshot,
        (date(2026, 10, 1), date(2026, 10, 31)),
        [],
        gross=True,
    )
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0]["оборот_USD"] == "251.00"
    assert "RUB" not in raw.decode("utf-8-sig") and "курс" not in raw.decode("utf-8-sig")
    assert "расчёт" not in rows[0] and "процент" not in raw.decode("utf-8-sig")
    unknown = Snapshot(
        (Entry("x", date(2026, 10, 1), "m", "Test", Decimal(50)),), datetime.now(timezone.utc)
    )
    with pytest.raises(UserError, match="нет полного оборота"):
        unknown.totals(date(2026, 10, 1), date(2026, 10, 31), gross=True)


async def test_large_admin_report_exports_gross_csv(ui):
    entries = tuple(
        Entry(
            str(i),
            date(2026, 10, 16),
            f"m{i}",
            f"Модель {i} " + "а" * 45,
            Decimal(50),
            Decimal(100),
        )
        for i in range(80)
    )
    ui.payroll.get.return_value = Snapshot(entries, datetime.now(timezone.utc))
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "salary:current", 99, "admin")
    documents = [x for x in ui.bots["admin"].session.calls if isinstance(x, SendDocument)]
    assert len(documents) == 1 and "Полный оборот" in documents[0].caption
    assert "Таблица" not in documents[0].caption
    rows = list(csv.DictReader(io.StringIO(documents[0].document.data.decode("utf-8-sig"))))
    assert rows and all(row["оборот_USD"] == "100.00" for row in rows)


async def test_rejected_model_can_contact_resubmit_and_get_approved(ui):
    ui.store.register(30, "Старая анкета", "", "Было")
    ui.store.reject_person(30, 99)
    await message(ui, "/start", 30)
    messages = [x for x in ui.bots["model"].session.calls if isinstance(x, SendMessage)]
    assert any(
        b.text == "🌸 Подать анкету заново"
        for m in messages
        if m.reply_markup
        for row in m.reply_markup.inline_keyboard
        for b in row
    )
    await click(ui, "contact", 30)
    await message(ui, "Как исправить анкету?", 30)
    assert ui.store.one("SELECT user_id FROM tickets")["user_id"] == 30
    await click(ui, "register", 30)
    await message(ui, "Новая анкета", 30)
    person = ui.store.user(30)
    assert person["status"] == "pending" and person["name"] == "Новая анкета"
    assert person["note"] == "" and ("model", 30) not in ui.pending
    assert ui.store.one("SELECT count(*) n FROM people WHERE user_id=30")["n"] == 1
    assert ui.store.one("SELECT count(*) n FROM tickets WHERE user_id=30")["n"] == 1
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "approveprofile:30", 99, "admin")
    await click(ui, "room:0", 30)
    assert ui.store.active_shifts()[0]["user_id"] == 30


async def test_admin_can_accept_rejected_without_resubmission(ui):
    ui.store.register(30, "Анкета", "", "")
    ui.store.reject_person(30, 99)
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "person:30", 99, "admin")
    buttons = [
        b.callback_data.split(":", 2)[0] + ":" + b.callback_data.split(":", 2)[2]
        for m in ui.bots["admin"].session.calls
        if isinstance(m, SendMessage) and m.reply_markup
        for row in m.reply_markup.inline_keyboard
        for b in row
    ]
    assert "a:approveprofile:30" in buttons and "a:bindstart:30" in buttons
    assert "a:rejectperson:30" not in buttons
    await click(ui, "approveprofile:30", 99, "admin")
    assert ui.store.user(30)["status"] == "approved"


async def test_resubmission_cannot_overwrite_intervening_approval(ui):
    ui.store.register(30, "Исходная", "", "")
    ui.store.reject_person(30, 99)
    await click(ui, "register", 30)
    ui.store.approve_person(30, "m30", 99)
    await message(ui, "Замена", 30)
    person = ui.store.user(30)
    assert person["status"] == "approved" and person["name"] == "Исходная"
    assert person["payroll_id"] == "m30"
    with pytest.raises(UserError):
        ui.store.register(10, "Другое имя", "", "")
