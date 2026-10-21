from decimal import Decimal

import pytest

from studio_bot.config import UserError
from studio_bot.store import Store


def test_room_and_model_are_exclusive(people):
    s = people
    sid = s.start_shift(10, "4", ("4", "5"))
    with pytest.raises(UserError):
        s.start_shift(20, "4", ("4", "5"))
    with pytest.raises(UserError):
        s.start_shift(10, "5", ("4", "5"))
    with pytest.raises(UserError):
        s.end_shift(sid, 20)
    assert len(s.active_shifts()) == 1
    now = s.clock()
    s.clock = lambda: now + 3660
    assert s.end_shift(sid, 10) == 3660
    s.start_shift(20, "4", ("4", "5"))
    assert s.active_shifts()[0]["user_id"] == 20


def test_shift_survives_restart_and_usage_clips_boundaries(people, tmp_path):
    s = people
    start = s.clock()
    s.start_shift(10, "4", ("4",))
    another = Store(tmp_path / "studio.sqlite3", clock=lambda: start + 7200)
    try:
        assert len(another.active_shifts()) == 1
        assert another.room_usage(start + 1800, start + 3600) == {"4": 1800}
        another.end_shift(1, 99, admin=True)
        assert another.room_usage(start - 100, start + 10000) == {"4": 7200}
    finally:
        another.close()


def test_booking_overlap_and_ownership(people):
    s, start = people, people.clock() + 3600
    a = s.book(10, "2026-10-19", 0, start, start + 7200)
    b = s.book(20, "2026-10-19", 0, start + 3600, start + 10800)
    with pytest.raises(UserError):
        s.book(10, "2026-10-19", 1, start + 3600, start + 9000)
    s.decide_booking(a, 99, room="4")
    with pytest.raises(UserError):
        s.decide_booking(b, 99, room="4")
    s.decide_booking(b, 99, room="5")
    with pytest.raises(UserError):
        s.decide_booking(a, 20, reject=True, owner=True)
    s.decide_booking(a, 10, reject=True, owner=True)
    assert s.one("SELECT state FROM bookings WHERE id=?", (a,))["state"] == "cancelled"


def test_current_reservation_prevents_other_model_start(people):
    start = people.clock() + 60
    bid = people.book(10, "2026-10-19", 0, start, start + 3600)
    people.decide_booking(bid, 99, room="4")
    people.clock = lambda: start + 10
    with pytest.raises(UserError):
        people.start_shift(20, "4", ("4",))
    people.start_shift(10, "4", ("4",))


def test_payroll_binding_is_unique_and_pending_only(people):
    people.register(30, "Новенькая", "", "")
    with pytest.raises(UserError):
        people.approve_person(30, "m10", 99)
    assert people.user(30)["status"] == "pending"
    with pytest.raises(UserError):
        people.approve_person(10, "stolen", 99)


def test_advance_is_idempotent_and_requires_real_status_transitions(people):
    aid = people.advance(10, Decimal("50"), "USD", {"current": "100"})
    with pytest.raises(UserError):
        people.advance(10, Decimal("50"), "USD", {})
    with pytest.raises(UserError):
        people.decide_advance(aid, "paid", 99)
    people.decide_advance(aid, "approve", 99)
    with pytest.raises(UserError):
        people.decide_advance(aid, "approve", 99)
    with pytest.raises(UserError):
        people.advance(10, Decimal("50"), "USD", {})
    people.decide_advance(aid, "paid", 99)
    assert people.advance(10, Decimal("25"), "USD", {}) != aid


def test_reply_saved_once_and_outbox_routes_to_model(people):
    tid = people.ticket(10, "Можно уточнить смену?")
    people.answer(tid, 99, "Да, приходите к 12.")
    with pytest.raises(UserError):
        people.answer(tid, 99, "Повтор")
    row = people.one("SELECT * FROM outbox ORDER BY id DESC")
    assert row["user_id"] == 10 and row["role"] == "model"
    assert "12" in row["text"]


def test_unapproved_model_cannot_start_or_request_advance(store):
    store.register(10, "Анна", "", "")
    with pytest.raises(UserError):
        store.start_shift(10, "4", ("4",))
    with pytest.raises(UserError):
        store.advance(10, Decimal(10), "USD", {})
