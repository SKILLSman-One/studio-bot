from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import SendMessage
from conftest import click, message, texts

from studio_bot.config import Settings, UserError
from studio_bot.dates import payment_dates, payment_period, require_booking_window, visit_times
from studio_bot.payroll import Entry, Snapshot
from studio_bot.store import Store

ZONE = ZoneInfo("Europe/Moscow")
DAY = date(2026, 10, 19)


@pytest.mark.parametrize(
    "stamp,allowed",
    [
        ("2026-10-14T23:59:59", False),
        ("2026-10-15T00:00:00", True),
        ("2026-10-16T23:59:59", True),
        ("2026-10-17T00:00:00", False),
        ("2026-10-18T12:00:00", False),
        ("2026-10-19T12:00:00", False),
    ],
)
def test_booking_window_boundaries(stamp, allowed):
    now = datetime.fromisoformat(stamp).replace(tzinfo=ZONE)
    if allowed:
        require_booking_window(now, DAY)
    else:
        with pytest.raises(UserError):
            require_booking_window(now, DAY)


@pytest.mark.parametrize(
    "text", ["14:00", "24:00-25:00", "14:60-16:00", "12:00-12:00", "abc", "14:00-16:00 extra"]
)
def test_invalid_manual_time(text):
    with pytest.raises(UserError):
        visit_times(DAY, text, ZONE)


def test_night_and_adjacent_times():
    a, b = visit_times(DAY, "23:00–2:30", ZONE)
    assert b - a == 3.5 * 3600
    assert datetime.fromtimestamp(b, ZONE).date() == DAY + timedelta(days=1)
    assert visit_times(DAY, "9:00 — 19:30", ZONE)[1] > a - 86400


@pytest.mark.parametrize(
    "payday,start,end",
    [
        ("2026-10-05", "2026-09-01", "2026-09-15"),
        ("2026-10-20", "2026-09-16", "2026-09-30"),
        ("2026-11-20", "2026-10-16", "2026-10-31"),
        ("2024-03-20", "2024-02-16", "2024-02-29"),
        ("2026-01-05", "2025-12-01", "2025-12-15"),
    ],
)
def test_payment_periods(payday, start, end):
    assert payment_period(date.fromisoformat(payday)) == (
        date.fromisoformat(start),
        date.fromisoformat(end),
    )


@pytest.mark.parametrize(
    "today,next_day,previous",
    [
        ("2026-10-05", "2026-10-05", "2026-09-20"),
        ("2026-10-06", "2026-10-20", "2026-10-05"),
        ("2026-10-20", "2026-10-20", "2026-10-05"),
        ("2026-12-21", "2027-01-05", "2026-12-20"),
    ],
)
def test_next_payment(today, next_day, previous):
    assert payment_dates(date.fromisoformat(today)) == (
        date.fromisoformat(next_day),
        date.fromisoformat(previous),
    )


def book(store, uid=10, room="4", hours="14:00-19:30", admins=(99,)):
    start, end = visit_times(DAY, hours, ZONE)
    return store.book_room(uid, str(DAY), room, start, end, Settings(root=None), admins)


def test_free_booking_conflict_resolution_and_adjacent(people):
    a, state = book(people)
    assert state == "approved"
    b, state = book(people, 20, hours="15:00-18:00")
    assert state == "conflict"
    assert people.one("SELECT count(*) n FROM outbox WHERE role='admin'")["n"] == 1
    with pytest.raises(UserError):
        people.decide_booking(b, 99, room="4")
    people.decide_booking(b, 99, room="5")
    assert people.one("SELECT state FROM bookings WHERE id=?", (b,))["state"] == "approved"
    assert book(people, 20, hours="19:30-20:00")[1] == "approved"
    with pytest.raises(UserError):
        book(people, 10, room="6", hours="14:00-15:00")
    with pytest.raises(UserError):
        people.decide_booking(a, 20, reject=True, owner=True)


def test_conflict_cancel_ban_and_reschedule(people):
    book(people)
    b, _ = book(people, 20)
    people.decide_booking(b, 20, reject=True, owner=True)
    b, _ = book(people, 20)
    people.decide_booking(b, 99, room="4", times=visit_times(DAY, "20:00-21:00", ZONE))
    assert book(people, 20, room="5")[1] == "approved"  # Old start no longer blocks new time.
    people.set_banned(20, True, 99)
    assert {r["state"] for r in people.all("SELECT state FROM bookings WHERE user_id=20")} == {
        "cancelled"
    }
    people.set_banned(20, False, 99)
    c, _ = book(people, 20)
    people.set_banned(20, True, 99)
    assert people.one("SELECT state FROM bookings WHERE id=?", (c,))["state"] == "cancelled"


def test_two_concurrent_confirmations_never_double_book(people, tmp_path):
    stamp = people.clock()
    barrier = Barrier(2)

    def worker(uid):
        s = Store(tmp_path / "studio.sqlite3", clock=lambda: stamp)
        try:
            barrier.wait(timeout=10)
            return book(s, uid)[1]
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [10, 20]))
    assert sorted(results) == ["approved", "conflict"]


async def prepare(ui, uid=10):
    await click(ui, f"day:{DAY}", uid)
    await message(ui, "14:00–19:30", uid)
    token = ui.pending[("model", uid)][1]["token"]
    await click(ui, f"bookroom:{token}:0", uid)
    return ui.pending[("model", uid)][1]["token"]


async def test_booking_ui_race_and_admin_time_resolution(ui):
    assert await ui.auth.login(99, "correct-password")
    token = await prepare(ui)
    book(ui.store, 20)
    await click(ui, f"bookconfirm:{token}")
    await click(ui, f"bookconfirm:{token}")
    assert ui.store.one("SELECT count(*) n FROM bookings")["n"] == 2
    assert ui.store.one("SELECT state FROM bookings WHERE user_id=10")["state"] == "conflict"
    assert "пока не забронирована" in texts(ui)
    await click(ui, "bookings:0", 99, "admin")
    await click(ui, "booking:2", 99, "admin")
    assert "Пересечения" in texts(ui, "admin") and "Мария" in texts(ui, "admin")
    await click(ui, "booktime:2", 99, "admin")
    await message(ui, "20:00-22:00", 99, "admin")
    token = ui.pending[("admin", 99)][1]["token"]
    await click(ui, f"bookedit:{token}:0", 99, "admin")
    assert ui.store.one("SELECT state FROM bookings WHERE id=2")["state"] == "approved"
    await click(ui, f"bookedit:{token}:0", 99, "admin")
    assert "устарел" in texts(ui, "admin")


async def test_friday_to_saturday_confirmation_and_free_attendance(ui):
    stamp = datetime(2026, 10, 16, 23, 59, 50, tzinfo=ZONE).timestamp()
    ui.store.clock = lambda: stamp
    token = await prepare(ui)
    ui.store.clock = lambda: stamp + 11
    await click(ui, f"bookconfirm:{token}")
    assert not ui.store.all("SELECT * FROM bookings")
    assert "четверг и пятницу" in texts(ui)
    await click(ui, "room:0")
    assert ui.store.active_shifts()[0]["room"] == "4"
    notes = ui.store.all("SELECT text FROM outbox WHERE text LIKE '%Твоя смена началась%'")
    assert len(notes) == 1 and "4-я комната" in notes[0]["text"]
    await click(ui, "room:0")
    assert len(ui.store.all("SELECT text FROM outbox WHERE text LIKE '%Твоя смена началась%'")) == 1


async def test_booking_expiry_and_admin_access(ui):
    token = await prepare(ui)
    stamp = ui.store.clock()
    ui.store.clock = lambda: stamp + 301
    await click(ui, f"bookconfirm:{token}")
    assert not ui.store.all("SELECT * FROM bookings")
    await click(ui, "booktime:1", 20, "admin")
    assert ("admin", 20) not in ui.pending
    await click(ui, "rooms")
    buttons = [
        b.text
        for c in ui.bots["model"].session.calls
        if isinstance(c, SendMessage) and c.reply_markup
        for row in c.reply_markup.inline_keyboard
        for b in row
    ]
    assert "✨ Начать смену — 4-я комната" in buttons


def test_advance_deduction_persists_once_and_stays_on_payday(people, tmp_path):
    aid = people.advance(10, Decimal("50.25"), "USD", {})
    assert people.advance_deduction(10, date(2026, 10, 20)) == 0
    people.decide_advance(aid, "approve", 99)
    assert people.advance_deduction(10, date(2026, 10, 20)) == Decimal("50.25")
    with pytest.raises(UserError):
        people.decide_advance(aid, "approve", 99)
    people.clock = lambda: datetime(2026, 10, 21, tzinfo=ZONE).timestamp()
    people.decide_advance(aid, "paid", 99)
    assert people.advance_deduction(10, date(2026, 10, 20)) == Decimal("50.25")
    assert people.advance_deduction(10, date(2026, 11, 5)) == 0
    reopened = Store(tmp_path / "studio.sqlite3")
    try:
        assert reopened.advance_deduction(10, date(2026, 10, 20)) == Decimal("50.25")
    finally:
        reopened.close()
    aid = people.advance(10, Decimal("20"), "USD", {})
    people.decide_advance(aid, "reject", 99)
    assert people.advance_deduction(10, date(2026, 11, 5)) == 0


async def test_salary_payday_deducts_only_own_advance_and_preserves_admin_gross(ui):
    aid = ui.store.advance(10, Decimal("50"), "USD", {})
    ui.store.decide_advance(aid, "approve", 99)
    await click(ui, "payday:2026-10-20")
    assert "16.09.2026 — 30.09.2026" in texts(ui)
    assert "К получению: $150.00" in texts(ui)
    assert "Мария" not in texts(ui)
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "payday:2026-10-20", 99, "admin")
    assert "400.00" in texts(ui, "admin") and "К получению" not in texts(ui, "admin")
    ui.store.decide_advance(aid, "paid", 99)
    await click(ui, "payday:2026-10-20")
    assert "К получению: $150.00" in texts(ui)


async def test_missing_payment_month_is_not_zero(ui):
    old = ui.payroll.get.return_value
    ui.payroll.get.return_value = Snapshot(
        old.entries, old.fetched_at, ((date(2026, 10, 1), date(2026, 10, 31)),)
    )
    await click(ui, "payday:2026-10-20")
    assert "Нет подключённых данных" in texts(ui)
    assert "К получению" not in texts(ui)


async def test_inactive_aliases_do_not_block_other_models_or_offer_binding(ui):
    old = ui.payroll.get.return_value
    ui.payroll.get.return_value = Snapshot(
        old.entries + (Entry("x", date(2026, 9, 16), "old", "Бывшая", None, Decimal("70"), True),),
        old.fetched_at,
    )
    await click(ui, "payday:2026-10-20")
    assert "К получению: $200.00" in texts(ui)
    ui.store.register(30, "Новая", "", "")
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "bindstart:30", 99, "admin")
    assert not ui.pending[("admin", 99)][1]["candidates"]
    await click(ui, "payday:2026-10-20", 99, "admin")
    assert "Бывшая" not in texts(ui, "admin")
    assert ui.payroll.get.return_value.amount(
        "old", (date(2026, 9, 1), date(2026, 9, 30)), gross=True
    ) == Decimal("70")
    with pytest.raises(UserError, match="бывшего"):
        ui.payroll.get.return_value.amount("old", (date(2026, 9, 1), date(2026, 9, 30)))
