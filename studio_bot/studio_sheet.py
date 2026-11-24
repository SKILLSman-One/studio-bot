"""Reader for verified monthly studio workbooks; never modifies Google Sheets."""

import calendar
import hashlib
import unicodedata
from datetime import date, timedelta
from decimal import Decimal

from .config import UserError
from .payroll import Entry, decimal, sheet_date

SHARE_BASIS = "Выручка × процент модели, до удержаний и авансов."


def alias_key(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def alias_id(name):
    # This identity survives row moves and spacing changes, but not a renamed alias.
    return "alias_" + hashlib.sha256(alias_key(name).encode()).hexdigest()[:24]


def cell_decimal(value, row, column):
    try:
        return decimal(value)
    except UserError:
        letters = ""
        while column:
            column, digit = divmod(column - 1, 26)
            letters = chr(65 + digit) + letters
        raise UserError(
            f"Некорректная сумма в ячейке {letters}{row} листа «Чеки». Проверьте исходную таблицу."
        ) from None


def parse_shares(rows):
    if not rows or rows[0][:4] != ["Модель", "% модели", "Модель", "Индивидуальный % модели"]:
        raise UserError("Изменилась структура листа «Модели»: нужны столбцы G:J.")
    regular, overrides = {}, {}
    for row in rows[1:]:
        for col, target in ((0, regular), (2, overrides)):
            name = row[col] if len(row) > col else None
            value = row[col + 1] if len(row) > col + 1 else None
            if name in (None, "") and value in (None, ""):
                continue
            key = alias_key(name) if name is not None else ""
            if not key or key.startswith("#") or key in target:
                raise UserError(
                    "Проверьте пустые, повторяющиеся имена и ошибки в процентах моделей."
                )
            share = decimal(value)
            if not 0 < share <= 1:
                raise UserError("Процент модели должен быть числом больше 0 и не больше 100%.")
            target[key] = share
    regular.update(overrides)
    return regular


def monthly_adjustments(source, first, last):
    """Owner-approved exclusions and corrections, scoped to this source month."""
    overrides = {}
    try:
        ignored = {date.fromisoformat(value) for value in source.get("ignored_dates", [])}
        if any(not first <= day <= last for day in ignored):
            raise ValueError()
        for item in source.get("daily_overrides", []):
            day = date.fromisoformat(item["date"])
            key = alias_key(item["model"])
            expected, amount = decimal(item["expected_usd"]), decimal(item["amount_usd"])
            identity = (key, day)
            if (
                not first <= day <= last
                or day in ignored
                or not key
                or min(expected, amount) < 0
                or identity in overrides
            ):
                raise ValueError()
            overrides[identity] = (expected, amount)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise UserError("Проверьте ignored_dates и daily_overrides в studio.toml.") from None
    return ignored, overrides


def ignored_day_control(rows, number, column):
    # J:L sum the ten site subtotals. Reconstruct ONLY the excluded day's
    # contribution for reconciliation; it is never emitted as earned income.
    details = rows[number - 11 : number - 1]
    if len(details) != 10 or any(r and r[0] not in (None, "") for r in details):
        raise UserError(
            f"Не удалось проверить исключённый день в строке {number} листа «Чеки»: изменился блок площадок."
        )
    return sum(
        (
            decimal(r[column])
            for r in details
            if len(r) > column and type(r[column]) in (int, float) and r[column] > 0
        ),
        Decimal(0),
    )


def identity_aliases(source):
    """Preserve verified historical IDs when only the order of name words changed."""
    result = {}
    try:
        for current, canonical in source.get("identity_aliases", {}).items():
            key, target = alias_key(current), alias_key(canonical)
            if (
                not key
                or key in result
                or not isinstance(canonical, str)
                or len(canonical) > 60
                or any(ord(c) < 32 for c in canonical)
                or sorted(key.split()) != sorted(target.split())
            ):
                raise ValueError()
            result[key] = canonical
    except (AttributeError, TypeError, ValueError):
        raise UserError(
            "Проверьте identity_aliases в studio.toml: допустима только перестановка тех же слов."
        ) from None
    return result


def parse_studio_matrix(rows, share_rows, source):
    """H1:AR…: name in H, site in I, totals J:L, dates/daily gross N:AR."""
    try:
        first = date.fromisoformat(source["month"] + "-01")
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        width = last.day
        header = rows[0]
        if header[2:5] != ["1й период", "2й период", "выручка"]:
            raise ValueError()
        days = [sheet_date(x) for x in header[6 : 6 + width]]
        if days != [first + timedelta(days=i) for i in range(width)]:
            raise ValueError()
        # Some 30-day workbooks retain a 31st column in their J:L formulas.
        # Reconcile that column, but never emit it as income for this month.
        reconciliation_width = width
        if source.get("totals_include_next_month_day", False):
            if width != 30 or sheet_date(header[6 + width]) != last + timedelta(days=1):
                raise ValueError()
            reconciliation_width += 1
    except (ValueError, IndexError, KeyError, TypeError):
        raise UserError(
            "Изменились даты или заголовки «Чеки». Проверьте месяц и диапазон источника."
        ) from None
    shares = parse_shares(share_rows)
    aliases = identity_aliases(source)
    ignored, overrides = monthly_adjustments(source, first, last)
    entries, seen = [], set()
    gross_by_day = [Decimal(0)] * reconciliation_width
    summary_adjustments = [Decimal(0)] * reconciliation_width
    placeholders = {alias_key(x) for x in source.get("placeholder_names", [])}
    inactive = {alias_key(x) for x in source.get("inactive_names", [])}
    for number, row in enumerate(rows[5:], 6):
        name = str(row[0]).strip() if row and row[0] is not None else ""
        if not name:
            continue  # Rows for individual sites have no aggregate model name in H.
        key = alias_key(name)
        if len(name) > 60 or any(ord(c) < 32 for c in name) or key.startswith("#"):
            raise UserError(f"Некорректное имя модели в строке {number} листа «Чеки».")
        if len(row) < 6 + reconciliation_width or row[1] not in (None, ""):
            raise UserError(f"Изменилась итоговая строка {number} листа «Чеки».")
        values, reconciliation_values = [], []
        for index, raw in enumerate(row[6 : 6 + reconciliation_width]):
            day = first + timedelta(days=index)
            if day in ignored:
                values.append(Decimal(0))
                reconciliation_values.append(
                    Decimal(0)
                    if key in placeholders
                    else ignored_day_control(rows, number, 6 + index)
                )
            else:
                value = cell_decimal(raw, number, 14 + index)
                override = overrides.get((key, day))
                if override:
                    expected, amount = override
                    if abs(value - expected) <= Decimal("0.01"):
                        summary_adjustments[index] += amount - value
                        value = amount
                    elif abs(value - amount) > Decimal("0.01"):
                        raise UserError(
                            f"Изменилась согласованная сумма в строке {number} листа «Чеки». Проверьте daily_overrides."
                        )
                values.append(value)
                reconciliation_values.append(value)
        totals = [cell_decimal(x, number, 10 + index) for index, x in enumerate(row[2:5])]
        calculated = [
            sum(reconciliation_values[:15]),
            sum(reconciliation_values[15:]),
            sum(reconciliation_values),
        ]
        if any(abs(a - b) > Decimal("0.01") for a, b in zip(totals, calculated)):
            raise UserError(f"Дневные суммы не совпали с итогами в строке {number} листа «Чеки».")
        if key in placeholders:
            if any(values) or any(totals):
                raise UserError(
                    "У служебной строки есть выручка. Укажите модель в исходной таблице."
                )
            continue
        mid = alias_id(aliases.get(key, name))
        if mid in seen:
            raise UserError(
                "Модель повторяется в итоговых строках «Чеки». Двойной подсчёт остановлен."
            )
        seen.add(mid)
        gross_by_day = [a + b for a, b in zip(gross_by_day, values)]
        for day, gross in zip(days, values):
            entries.append(
                Entry(
                    f"studio:{mid}:{day}",
                    day,
                    name=" ".join(name.split()),
                    model_id=mid,
                    amount=gross * shares[key]
                    if key in shares
                    else Decimal(0)
                    if gross == 0
                    else None,
                    gross=gross,
                    inactive=key in inactive,
                    share_available=key in shares,
                )
            )
    if not seen:
        raise UserError("В листе «Чеки» не найдено итоговых строк моделей.")
    # Reconcile the complete workbook total to catch omitted/unnamed model rows.
    if len(rows) < 3 or len(rows[2]) < 6 + reconciliation_width or rows[2][1] != "Сумма за день":
        raise UserError("Не найдена контрольная строка «Сумма за день» в листе «Чеки».")
    summary = [
        Decimal(0)
        if first + timedelta(days=index) in ignored
        else cell_decimal(x, 3, 14 + index) + summary_adjustments[index]
        for index, x in enumerate(rows[2][6 : 6 + reconciliation_width])
    ]
    if any(abs(a - b) > Decimal("0.01") for a, b in zip(gross_by_day, summary)):
        raise UserError(
            "Сумма моделей не совпала с общей выручкой по дням. Проверьте пропущенные имена и итоговые строки «Чеки»."
        )
    return entries, (first, last)
