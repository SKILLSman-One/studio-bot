from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pytest

from studio_bot.config import Settings, UserError
from studio_bot.dates import next_week, parse_period, periods, slot_times
from studio_bot.payroll import (
    COLUMNS,
    Entry,
    SheetsPayroll,
    Snapshot,
    amount_input,
    parse_rows,
    report_csv,
    report_text,
)


def test_table_dates_money_mappings_and_decimal_precision():
    rows = [
        list(COLUMNS),
        ["a", "16.10.2026", "m1", "Анна", "1 234,56"],
        ["b", 46311, "m1", "Анна", -12.34],
    ]
    parsed = parse_rows(rows)
    assert parsed[0].amount == Decimal("1234.56")
    assert parsed[1].day == date(2026, 10, 16)
    assert parsed[1].amount == Decimal("-12.34")
    rows[0][4] = "Заработок модели"
    assert len(parse_rows(rows, {"amount_usd": "Заработок модели"})) == 2


@pytest.mark.parametrize(
    "row",
    [
        ["a", "не дата", "m1", "Анна", 12],
        ["a", "2026-10-16", "m1", "Анна", "#REF!"],
        ["a", "2026-10-16", "m1", "Анна", "NaN"],
        ["a", "2026-10-16", "", "Анна", 12],
        ["a", "2026-10-16"],
    ],
)
def test_malformed_rows_fail_closed(row):
    with pytest.raises(UserError):
        parse_rows([list(COLUMNS), row])


def test_period_edges_and_per_model_salary():
    current, previous = periods(date(2026, 1, 15))
    assert previous == (date(2025, 12, 1), date(2025, 12, 31))
    snap = Snapshot(
        (
            Entry("1", date(2026, 1, 1), "m1", "Аня", Decimal("10.01")),
            Entry("2", date(2026, 1, 15), "m1", "Аня", Decimal("0.02")),
            Entry("3", date(2026, 1, 16), "m1", "Аня", Decimal("900")),
            Entry("4", date(2026, 1, 1), "m2", "Мария", Decimal("500")),
        ),
        datetime.now(timezone.utc),
    )
    assert snap.amount("m1", current) == Decimal("10.03")
    text = report_text(snap, current, model_id="m1")
    assert "$10.03" in text and "Мария" not in text and "500.00" not in text


@pytest.mark.parametrize(
    "text", ["NaN USD", "0 USD", "-100 USD", "100.001 USD", "10 EUR", "100", "5000 RUB"]
)
def test_bad_advance_amounts(text):
    with pytest.raises(UserError):
        amount_input(text)


def test_valid_advance_amount():
    assert amount_input("1 000,50 usd") == (Decimal("1000.50"), "USD")


def test_next_week_monday_midnight_and_december_rollover():
    days = next_week(date(2026, 12, 31))
    assert days[0] == date(2027, 1, 4) and days[-1] == date(2027, 1, 10)
    settings = Settings(None)
    start, end = slot_times(days[0], ("18:00", "00:00"), settings.zone)
    assert end - start == 21600
    assert parse_period("2026-10-01 2026-10-16")[1] == date(2026, 10, 16)
    with pytest.raises(UserError):
        parse_period("2026-10-16 2026-10-01")


async def test_source_failure_never_returns_stale_cache(tmp_path):
    payroll = SheetsPayroll(Settings(tmp_path))
    snapshot = Snapshot((), datetime.now(timezone.utc))
    payroll._read_sync = Mock(return_value=snapshot)
    assert await payroll.get() is snapshot
    payroll._read_sync.side_effect = UserError("Нет доступа")
    with pytest.raises(UserError):
        await payroll.get(force=True)
    with pytest.raises(UserError):
        await payroll.get()


def test_csv_does_not_execute_spreadsheet_labels():
    snapshot = Snapshot(
        (Entry("1", date(2026, 10, 16), "=x", "=CMD()", Decimal("1")),), datetime.now(timezone.utc)
    )
    output = report_csv(snapshot, (date(2026, 10, 1), date(2026, 10, 31)), []).decode("utf-8-sig")
    assert "'=CMD()" in output and "'=x" in output


@pytest.mark.parametrize("bad_data", [None, "duplicate", "mixed_identity"])
def test_google_adapter_readonly_and_snapshot_validation(tmp_path, monkeypatch, bad_data):
    from google.auth.transport import requests as transport
    from google.oauth2 import service_account

    credentials = Mock(return_value=object())
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", credentials)
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    monkeypatch.setattr(transport, "AuthorizedSession", factory)
    rows = [list(COLUMNS), ["r1", "2026-10-16", "m1", "Анна", "10.01"]]
    if bad_data == "duplicate":
        rows.append(["r1", "2026-10-17", "m1", "Анна", "20"])
    elif bad_data == "mixed_identity":
        rows.append(["r2", "2026-10-17", "m1", "Мария", "20"])
    session.get.return_value.json.return_value = {"values": rows}
    settings = Settings(
        tmp_path, sources=[{"spreadsheet_id": "test-sheet-123", "range": "'Начисления'!A:E"}]
    )
    payroll = SheetsPayroll(settings)
    if bad_data:
        with pytest.raises(UserError):
            payroll._read_sync()
    else:
        assert payroll._read_sync().entries[0].amount == Decimal("10.01")
    assert credentials.call_args.kwargs["scopes"] == [
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
    assert session.get.call_args.kwargs["params"]["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert not session.post.called and not session.put.called
