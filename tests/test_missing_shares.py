from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from conftest import admin_session, click, texts
from test_studio_sheet import fixture_rows

from studio_bot.config import UserError
from studio_bot.payroll import Entry, Snapshot, report_csv, report_text
from studio_bot.studio_sheet import alias_id, parse_studio_matrix


def two_models(*, zero=False):
    rows, shares, source = fixture_rows()
    other = [
        "Без процента",
        None,
        0 if zero else 100,
        0,
        0 if zero else 100,
        None,
        0 if zero else 100,
        *([0] * 30),
    ]
    rows.append(other)
    rows[2][6] += other[6]
    return rows, shares, source


def snapshot(data):
    entries, period = parse_studio_matrix(*data)
    return Snapshot(tuple(entries), datetime.now(timezone.utc), (period,)), period


def test_missing_share_does_not_block_other_models_or_admin_turnover():
    snap, period = snapshot(two_models())
    assert snap.amount(alias_id("Анна Test"), period) == 84
    assert snap.amount(alias_id("Без процента"), period, gross=True) == 100
    with pytest.raises(UserError, match="не указан процент"):
        snap.amount(alias_id("Без процента"), period)
    assert sum(snap.totals(*period, gross=True).values()) == 240
    assert "$84.00" in report_text(snap, period, model_id=alias_id("Анна Test"))
    assert "$100.00" in report_text(snap, period, gross=True)
    assert "100.00" in report_csv(snap, period, [], gross=True).decode("utf-8-sig")
    assert alias_id("Без процента") in snap.names(active_only=True)
    assert alias_id("Без процента") not in snap.names(active_only=True, require_share=True)


def test_zero_earnings_need_no_invented_percentage_but_cannot_be_newly_bound():
    snap, period = snapshot(two_models(zero=True))
    assert snap.amount(alias_id("Без процента"), period) == 0
    assert all(
        e.amount == 0 and not e.share_available
        for e in snap.entries
        if e.model_id == alias_id("Без процента")
    )
    assert alias_id("Без процента") not in snap.names(active_only=True, require_share=True)


def test_only_nonzero_days_without_share_are_unavailable():
    snap, _ = snapshot(two_models())
    mid = alias_id("Без процента")
    assert snap.amount(mid, (date(2026, 10, 2), date(2026, 10, 15))) == 0
    with pytest.raises(UserError, match="процент"):
        snap.amount(mid, (date(2026, 10, 1), date(2026, 10, 2)))


def test_restoring_percentage_restores_salary_and_binding_on_next_read():
    data = two_models()
    snap, period = snapshot(data)
    assert alias_id("Без процента") not in snap.names(active_only=True, require_share=True)
    data[1].append(["Без процента", 0.4])
    snap, period = snapshot(data)
    assert snap.amount(alias_id("Без процента"), period) == 40
    assert alias_id("Без процента") in snap.names(active_only=True, require_share=True)


def test_missing_share_does_not_hide_other_reconciliation_errors():
    data = two_models()
    data[0][-1][4] += 1
    with pytest.raises(UserError, match="Дневные суммы"):
        snapshot(data)


def reordered():
    rows, shares, source = fixture_rows()
    rows[6][0] = "Test Анна"
    shares[1][0] = shares[1][2] = "Test Анна"
    source["identity_aliases"] = {"Test Анна": "Анна Test"}
    return rows, shares, source


def test_verified_word_reordering_preserves_existing_id_and_percentage():
    snap, period = snapshot(reordered())
    assert snap.amount(alias_id("Анна Test"), period) == 84
    assert snap.names(active_only=True) == {alias_id("Анна Test"): "Test Анна"}
    assert {e.record_id for e in snap.entries} == {
        f"studio:{alias_id('Анна Test')}:{e.day}" for e in snap.entries
    }


@pytest.mark.parametrize(
    "aliases",
    [
        {"Mia Даша": "Mia Виктория"},
        {"Test Анна": "Анна Test\n"},
        {"Test Анна": "Анна Test", " Test  Анна ": "Анна Test"},
    ],
)
def test_aliases_cannot_merge_distinct_people_or_ambiguous_names(aliases):
    data = reordered()
    data[2]["identity_aliases"] = aliases
    with pytest.raises(UserError, match="identity_aliases"):
        snapshot(data)


def test_duplicate_canonical_identity_in_one_month_is_rejected():
    data = reordered()
    another = list(data[0][6])
    another[0] = "Анна Test"
    data[0].append(another)
    with pytest.raises(UserError, match="Двойной подсчёт"):
        snapshot(data)


async def test_missing_share_is_private_and_does_not_block_other_users(ui):
    old = ui.payroll.get.return_value
    missing = Entry(
        "missing",
        date(2026, 10, 16),
        "m30",
        "Без процента",
        None,
        Decimal(100),
        share_available=False,
    )
    ui.payroll.get.return_value = Snapshot(old.entries + (missing,), old.fetched_at)
    await click(ui, "salary:current", 10)
    assert "Аня: $125.50" in texts(ui) and "Без процента" not in texts(ui)
    ui.store.register(30, "Без процента", "", "")
    ui.store.approve_person(30, "m30", 99)
    ui.bots["model"].session.calls.clear()
    await click(ui, "salary:current", 30)
    assert "не указан процент" in texts(ui) and "$0.00" not in texts(ui)
    assert "Аня" not in texts(ui) and "125.50" not in texts(ui)
    admin_session(ui)
    await click(ui, "health", 99, "admin")
    status = texts(ui, "admin")
    assert "Без процента в последнем месяце: 1" in status
    assert "$" not in status and "Без процента\n" not in status


async def test_percentage_is_required_again_at_binding_confirmation(ui):
    admin_session(ui)
    with ui.store.db:
        ui.store.db.execute("UPDATE people SET payroll_id=NULL WHERE user_id=10")
    ui.store.register(30, "Новая", "", "")
    await click(ui, "bindstart:30", 99, "admin")
    state = ui.pending[("admin", 99)][1]
    await click(ui, f"bindpick:{state['token']}:0", 99, "admin")
    old = ui.payroll.get.return_value
    entries = tuple(
        Entry(e.record_id, e.day, e.model_id, e.name, None, e.gross, share_available=False)
        if e.model_id == "m10"
        else e
        for e in old.entries
    )
    ui.payroll.get = AsyncMock(return_value=Snapshot(entries, old.fetched_at))
    await click(ui, f"bindconfirm:{state['token']}", 99, "admin")
    assert ui.store.user(30)["payroll_id"] is None
    assert "Запись в таблице изменилась" in texts(ui, "admin")
