from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pytest

from studio_bot.config import Settings, UserError
from studio_bot.dates import periods
from studio_bot.payroll import SheetsPayroll, Snapshot, report_text
from studio_bot.studio_sheet import SHARE_BASIS, alias_id, parse_studio_matrix


def fixture_rows():
    days = [(date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(31)]
    rows = [[None, None, "1й период", "2й период", "выручка", None, *days]]
    rows += [[] for _ in range(4)]
    rows[2] = [None, "Сумма за день", 100, 40, 140, None, 100, *([0] * 14), 40, *([0] * 15)]
    # Site details must not be counted a second time.
    rows += [[None, "Площадка", 100, 40, 140, None, 100, *([0] * 30)]]
    rows += [["Анна Test", None, 100, 40, 140, None, 100, *([0] * 14), 40, *([0] * 15)]]
    rows += [["Работающие модели", None, 0, 0, 0, None, *([0] * 31)]]
    shares = [
        ["Модель", "% модели", "Модель", "Индивидуальный % модели"],
        ["Анна Test", 0.5, "Анна Test", 0.6],
    ]
    source = {
        "spreadsheet_id": "test-sheet-123",
        "format": "studio_daily_share",
        "calculation_confirmed": True,
        "month": "2026-10",
        "range": "'Чеки'!H1:AR770",
        "shares_range": "'Модели'!G1:J635",
        "placeholder_names": ["Работающие модели"],
    }
    return rows, shares, source


def test_real_layout_and_override_only_counts_model_totals():
    rows, shares, source = fixture_rows()
    entries, coverage = parse_studio_matrix(rows, shares, source)
    snap = Snapshot(tuple(entries), datetime.now(timezone.utc), (coverage,), SHARE_BASIS)
    mid = alias_id("  АННА  Test ")
    assert len(entries) == 31 and len(snap.names()) == 1
    assert snap.amount(mid, coverage) == Decimal("84.0")
    assert snap.totals(*coverage, gross=True)[mid] == Decimal("140")
    current, previous = periods(date(2026, 10, 16), "semimonthly")
    assert snap.amount(mid, current) == Decimal("24.0")
    assert snap.amount(mid, previous) == Decimal("60.0")
    assert (
        report_text(snap, previous, model_id=mid)
        == "Заработок: 01.10.2026 — 15.10.2026\n\nАнна Test: $60.00"
    )
    with pytest.raises(UserError, match="Нет подключённых данных"):
        snap.totals(date(2026, 9, 30), date(2026, 10, 1))
    with pytest.raises(UserError, match="Псевдоним"):
        snap.amount("unknown", current)
    # Row positions and insignificant whitespace don't change IDs.
    rows[6][0] = " Анна   Test "
    rows.insert(6, [])
    changed, _ = parse_studio_matrix(rows, shares, source)
    assert [e.record_id for e in entries] == [e.record_id for e in changed]


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate",
        "placeholder_money",
        "formula_error",
        "missing_percent",
        "invalid_percent",
        "moved_month",
        "mismatch",
        "short_row",
        "duplicate_percent",
    ],
)
def test_matrix_problems_stop_the_entire_snapshot(problem):
    rows, shares, source = fixture_rows()
    if problem == "duplicate":
        rows.append(list(rows[6]))
    elif problem == "placeholder_money":
        rows[7] = ["Работающие модели", *rows[6][1:]]
    elif problem == "formula_error":
        rows[6][6] = "#REF!"
    elif problem == "missing_percent":
        shares = shares[:1]
    elif problem == "invalid_percent":
        shares[1][3] = 60
    elif problem == "moved_month":
        source["month"] = "2026-11"
    elif problem == "mismatch":
        rows[6][4] = 900
    elif problem == "short_row":
        rows[6] = rows[6][:9]
    elif problem == "duplicate_percent":
        shares.append(list(shares[1]))
    with pytest.raises(UserError):
        parse_studio_matrix(rows, shares, source)


def test_half_month_boundaries_and_leap_year():
    assert periods(date(2026, 1, 1), "semimonthly")[1] == (date(2025, 12, 16), date(2025, 12, 31))
    assert periods(date(2024, 3, 15), "semimonthly")[1] == (date(2024, 2, 16), date(2024, 2, 29))
    assert periods(date(2026, 10, 16), "semimonthly")[0] == (date(2026, 10, 16), date(2026, 10, 16))


def test_server_batch_reader_and_overlap(tmp_path, monkeypatch):
    from google.auth.transport import requests as transport
    from google.oauth2 import service_account

    rows, shares, source = fixture_rows()
    credentials = Mock(return_value=object())
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", credentials)
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    monkeypatch.setattr(transport, "AuthorizedSession", factory)
    session.get.return_value.json.return_value = {
        "valueRanges": [{"values": rows}, {"values": shares}]
    }
    settings = Settings(tmp_path, sources=[source])
    snap = SheetsPayroll(settings)._read_sync()
    assert snap.amount(alias_id("Анна Test"), (date(2026, 10, 1), date(2026, 10, 31))) == Decimal(
        "84.0"
    )
    assert session.get.call_args.args[0].endswith("/values:batchGet")
    assert session.get.call_args.kwargs["params"]["ranges"] == [
        source["range"],
        source["shares_range"],
    ]
    assert credentials.call_args.kwargs["scopes"] == [
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
    assert not session.post.called
    settings.sources.append(dict(source))
    with pytest.raises(UserError, match="record_id"):
        SheetsPayroll(settings)._read_sync()


def test_unconfirmed_calculation_is_explicit_error(tmp_path):
    _, _, source = fixture_rows()
    source["calculation_confirmed"] = False
    with pytest.raises(UserError, match="подтвердить"):
        SheetsPayroll(Settings(tmp_path, sources=[source]))._read_sync()


def test_missing_model_name_cannot_silently_drop_earnings():
    rows, shares, source = fixture_rows()
    rows[6][0] = None
    # Another zero model keeps the snapshot nonempty, but cannot hide the missing gross.
    rows.append(["Другая", None, 0, 0, 0, None, *([0] * 31)])
    shares.append(["Другая", 0.5])
    with pytest.raises(UserError, match="общей выручкой"):
        parse_studio_matrix(rows, shares, source)


def test_september_totals_reconcile_october_tail_without_counting_it():
    rows, shares, source = fixture_rows()
    source["month"] = "2026-09"
    rows[0][6:] = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(31)]
    for index in (2, 6):
        rows[index][-1] = 15
        rows[index][3] = 55
        rows[index][4] = 155
    with pytest.raises(UserError, match="Дневные суммы"):
        parse_studio_matrix(rows, shares, source)
    source["totals_include_next_month_day"] = True
    entries, coverage = parse_studio_matrix(rows, shares, source)
    assert len(entries) == 30 and max(e.day for e in entries) == date(2026, 9, 30)
    snap = Snapshot(tuple(entries), datetime.now(timezone.utc), (coverage,))
    assert snap.amount(alias_id("Анна Test"), coverage, gross=True) == Decimal(140)
    assert snap.amount(alias_id("Анна Test"), coverage) == Decimal(84)
    rows[0][-1] = "2026-10-02"
    with pytest.raises(UserError, match="даты"):
        parse_studio_matrix(rows, shares, source)


def test_declared_former_model_keeps_gross_without_inventing_percentage():
    rows, shares, source = fixture_rows()
    shares = shares[:1]
    with pytest.raises(UserError, match="процент"):
        parse_studio_matrix(rows, shares, source)
    source["inactive_names"] = ["Анна Test"]
    entries, coverage = parse_studio_matrix(rows, shares, source)
    snap = Snapshot(tuple(entries), datetime.now(timezone.utc), (coverage,))
    assert not snap.names(active_only=True)
    assert snap.amount(alias_id("Анна Test"), coverage, gross=True) == Decimal(140)
    with pytest.raises(UserError, match="бывшего"):
        snap.amount(alias_id("Анна Test"), coverage)
    rows[6][4] = 900
    with pytest.raises(UserError, match="Дневные суммы"):
        parse_studio_matrix(rows, shares, source)
