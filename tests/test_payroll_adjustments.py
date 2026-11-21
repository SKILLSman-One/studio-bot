from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from test_month_failures import month_data

from studio_bot.config import UserError
from studio_bot.payroll import Snapshot, report_csv
from studio_bot.studio_sheet import alias_id, parse_studio_matrix


def snapshot(data):
    entries, period = parse_studio_matrix(*data)
    return Snapshot(tuple(entries), datetime.now(timezone.utc), (period,)), period


def excluded_day():
    rows, shares, source = month_data("2026-11")
    model = rows[6]
    model[23] = "of "
    model[3] += 180
    model[4] += 180
    detail = [None, "Площадка", 100, 220, 320, None, 100, *([0] * 14), 40, *([0] * 14)]
    detail[23] = 180
    zero = [None, "", 0, 0, 0, None, *([0] * 30)]
    rows = rows[:5] + [detail] + [list(zero) for _ in range(9)] + [model, rows[7]]
    source["ignored_dates"] = ["2026-11-18"]
    return rows, shares, source


def override_day():
    rows, shares, source = month_data("2026-10")
    rows[6][30] = 355.45
    rows[6][3] = "388.30"
    rows[6][4] = "488.30"
    rows[2][30] = 355.45
    source["daily_overrides"] = [
        {
            "model": "Анна Test",
            "date": "2026-10-25",
            "expected_usd": "355.45",
            "amount_usd": "348.30",
        }
    ]
    return rows, shares, source


def test_whole_november_column_is_excluded_for_all_models():
    data = excluded_day()
    rows, shares, _ = data
    # A second model has real numeric revenue on the excluded day: it is
    # excluded as well, while her revenue on November 19 remains visible.
    detail = [None, "Площадка", 0, 45, 45, None, *([0] * 30)]
    detail[23], detail[24] = 20, 25
    zero = [None, "", 0, 0, 0, None, *([0] * 30)]
    model = ["Другая", None, 0, 45, 45, None, *([0] * 30)]
    model[23], model[24] = 20, 25
    rows.extend([detail] + [list(zero) for _ in range(9)] + [model])
    rows[2][23], rows[2][24] = 20, 25
    shares.append(["Другая", 0.5])
    snap, period = snapshot(data)
    ignored = (date(2026, 11, 18), date(2026, 11, 18))
    assert all(entry.gross == 0 for entry in snap.entries if entry.day == ignored[0])
    assert snap.amount(alias_id("Анна Test"), period) == 84
    assert snap.amount(alias_id("Другая"), period) == Decimal("12.5")
    assert snap.amount(alias_id("Другая"), ignored, gross=True) == 0
    assert snap.amount(alias_id("Анна Test"), period, gross=True) == 140
    assert "12.50" in report_csv(snap, period, []).decode("utf-8-sig")


@pytest.mark.parametrize(
    "problem", ["other_day_text", "wrong_totals", "wrong_summary", "missing_block"]
)
def test_ignored_column_does_not_disable_other_checks(problem):
    data = excluded_day()
    rows, _, _ = data
    if problem == "other_day_text":
        rows[15][24] = "of"
    elif problem == "wrong_totals":
        rows[15][3] += 1
    elif problem == "wrong_summary":
        rows[2][24] += 1
    else:
        del rows[5]
    with pytest.raises(UserError):
        snapshot(data)


def test_approved_october_correction_uses_exact_site_sum_and_model_percentage():
    snap, period = snapshot(override_day())
    mid = alias_id("Анна Test")
    day = (date(2026, 10, 25), date(2026, 10, 25))
    assert snap.amount(mid, day, gross=True) == Decimal("348.30")
    assert snap.amount(mid, day) == Decimal("208.980")
    assert snap.amount(mid, period, gross=True) == Decimal("488.30")


@pytest.mark.parametrize("change", ["name", "date", "value", "other_day", "summary"])
def test_correction_is_scoped_and_keeps_unrelated_validation(change):
    data = override_day()
    rows, _, source = data
    if change == "name":
        source["daily_overrides"][0]["model"] = "Другая"
    elif change == "date":
        source["daily_overrides"][0]["date"] = "2026-10-24"
    elif change == "value":
        rows[6][30] = 356
    elif change == "other_day":
        rows[6][31] += 1
    else:
        rows[2][31] += 1
    with pytest.raises(UserError):
        snapshot(data)


def test_corrected_source_and_row_moves_do_not_double_apply_adjustment():
    data = override_day()
    rows, _, _ = data
    rows[6][30] = "348.30"
    rows[2][30] = "348.30"
    rows.insert(6, [])
    snap, period = snapshot(data)
    assert snap.amount(alias_id("Анна Test"), period, gross=True) == Decimal("488.30")


@pytest.mark.parametrize("setting", ["ignored_dates", "daily_overrides"])
def test_adjustments_cannot_apply_to_another_month(setting):
    data = deepcopy(excluded_day() if setting == "ignored_dates" else override_day())
    _, _, source = data
    if setting == "ignored_dates":
        source[setting] = ["2026-12-18"]
    else:
        source[setting][0]["date"] = "2026-11-25"
    with pytest.raises(UserError, match="ignored_dates и daily_overrides"):
        snapshot(data)
