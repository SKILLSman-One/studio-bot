import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pytest
from conftest import admin_session, click, message, texts
from test_studio_sheet import fixture_rows

from studio_bot.config import Settings, UserError
from studio_bot.payroll import SheetsPayroll, Snapshot, SourceIssue, report_csv, report_text
from studio_bot.studio_sheet import alias_id, parse_studio_matrix


def month_data(month):
    rows, shares, source = fixture_rows()
    first = date.fromisoformat(month + "-01")
    width = calendar.monthrange(first.year, first.month)[1]
    source["month"] = month
    source["spreadsheet_id"] = "test-sheet-" + month
    rows[0] = rows[0][:6] + [(first + timedelta(days=i)).isoformat() for i in range(width)]
    for n in (2, 5, 6, 7):
        rows[n] = rows[n][: 6 + width]
    return rows, shares, source


def reader(tmp_path, monkeypatch, months, problems):
    from google.auth.transport import requests as transport
    from google.oauth2 import service_account

    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", Mock())
    factory = MagicMock()
    monkeypatch.setattr(transport, "AuthorizedSession", factory)
    sources, responses = [], []
    for month in months:
        rows, shares, source = month_data(month)
        problem = problems.get(month)
        response = Mock()
        if problem == "mismatch":
            rows[6][4] += 7.15
        elif problem == "text":
            rows[6][23] = "of "  # AE7; the same overwritten daily total as the live issue.
        elif problem == "network":
            response.raise_for_status.side_effect = RuntimeError("SECRET request details")
        response.json.return_value = {"valueRanges": [{"values": rows}, {"values": shares}]}
        responses.append(response)
        sources.append(source)
    factory.return_value.__enter__.return_value.get.side_effect = responses
    return SheetsPayroll(Settings(tmp_path, sources=sources))


def test_invalid_october_does_not_block_november_but_cannot_return_zero(tmp_path, monkeypatch):
    payroll = reader(tmp_path, monkeypatch, ["2026-10", "2026-11"], {"2026-10": "mismatch"})
    snap = payroll._read_sync()
    november = (date(2026, 11, 6), date(2026, 11, 21))
    mid = alias_id("Анна Test")
    assert snap.amount(mid, november) == Decimal("24.0")
    assert snap.amount(mid, november, gross=True) == Decimal(40)
    assert snap.names(active_only=True) == {mid: "Анна Test"}
    assert "$24.00" in report_text(snap, november, model_id=mid)
    for period in (
        (date(2026, 10, 1), date(2026, 10, 15)),
        (date(2026, 10, 31), date(2026, 11, 6)),
    ):
        with pytest.raises(UserError, match="2026-10.*строке 7"):
            snap.amount(mid, period)
        with pytest.raises(UserError, match="2026-10"):
            report_csv(snap, period, [], gross=True)


@pytest.mark.parametrize("problem", ["text", "network"])
def test_invalid_latest_month_does_not_restore_previous_roster(tmp_path, monkeypatch, problem):
    payroll = reader(tmp_path, monkeypatch, ["2026-10", "2026-11"], {"2026-11": problem})
    snap = payroll._read_sync()
    assert snap.amount(alias_id("Анна Test"), (date(2026, 10, 1), date(2026, 10, 31))) == 84
    with pytest.raises(UserError, match="2026-11"):
        snap.names(active_only=True)
    with pytest.raises(UserError, match="2026-11"):
        snap.amount("unknown", (date(2026, 11, 6), date(2026, 11, 21)))
    assert "SECRET" not in snap.issues[0].message
    if problem == "text":
        assert "AE7" in snap.issues[0].message


async def test_partial_refresh_discards_old_amounts_and_recovers(tmp_path, monkeypatch):
    payroll = reader(tmp_path, monkeypatch, ["2026-10", "2026-11"], {})
    assert (await payroll.get()).issues == ()
    failing = reader(tmp_path, monkeypatch, ["2026-10", "2026-11"], {"2026-10": "mismatch"})
    payroll._read_sync = failing._read_sync
    snap = await payroll.get(force=True)
    assert payroll.last_months == ("2026-11",)
    assert "2026-10" in payroll.last_error
    with pytest.raises(UserError, match="2026-10"):
        snap.totals(date(2026, 10, 1), date(2026, 10, 31))
    fixed = reader(tmp_path, monkeypatch, ["2026-10", "2026-11"], {})
    payroll._read_sync = fixed._read_sync
    assert (await payroll.get(force=True)).issues == ()
    assert payroll.last_error is None
    assert payroll.last_months == ("2026-10", "2026-11")


async def test_all_months_failed_clears_cache(tmp_path, monkeypatch):
    payroll = reader(tmp_path, monkeypatch, ["2026-10", "2026-11"], {})
    await payroll.get()
    failing = reader(
        tmp_path, monkeypatch, ["2026-10", "2026-11"], {"2026-10": "mismatch", "2026-11": "text"}
    )
    payroll._read_sync = failing._read_sync
    with pytest.raises(UserError, match="2026-10") as error:
        await payroll.get(force=True)
    assert "2026-11" in str(error.value) and "AE7" in str(error.value)
    assert payroll.cache is None


def test_duplicate_month_configuration_is_rejected_before_partial_reads(tmp_path, monkeypatch):
    payroll = reader(tmp_path, monkeypatch, ["2026-10", "2026-10"], {"2026-10": "mismatch"})
    with pytest.raises(UserError, match="несколько источников"):
        payroll._read_sync()


def test_overwritten_daily_total_is_not_treated_as_zero():
    rows, shares, source = month_data("2026-11")
    rows[6][23] = "of "
    with pytest.raises(UserError, match="ячейке AE7"):
        parse_studio_matrix(rows, shares, source)


async def test_diagnostics_displays_partial_failure_without_financial_values(ui):
    admin_session(ui)
    old = ui.payroll.get.return_value
    ui.payroll.get.return_value = Snapshot(
        old.entries,
        old.fetched_at,
        ((date(2026, 10, 1), date(2026, 10, 31)),),
        issues=(SourceIssue(date(2026, 9, 1), date(2026, 9, 30), "2026-09: ошибка строки 170"),),
    )
    await click(ui, "health", 99, "admin")
    text = texts(ui, "admin")
    assert "часть месяцев недоступна" in text and "2026-09: ошибка строки 170" in text
    assert "проверка успешна" not in text
    assert "$" not in text and "Аня" not in text


async def test_model_custom_period_works_with_unrelated_month_error(ui):
    today = datetime(2026, 11, 21, tzinfo=timezone.utc)
    old = ui.payroll.get.return_value
    entries = tuple(
        type(entry)(
            entry.record_id,
            date(2026, 11, 16),
            entry.model_id,
            entry.name,
            entry.amount,
            entry.gross,
        )
        for entry in old.entries[:2]
    )
    ui.payroll.get.return_value = Snapshot(
        entries,
        today,
        ((date(2026, 11, 1), date(2026, 11, 30)),),
        issues=(SourceIssue(date(2026, 10, 1), date(2026, 10, 31), "2026-10: ошибка строки 170"),),
    )
    await click(ui, "customperiod", 10)
    await message(ui, "2026-11-06 2026-11-21", 10)
    text = texts(ui)
    assert "Аня: $125.50" in text
    assert "Мария" not in text and "ошибка строки" not in text
