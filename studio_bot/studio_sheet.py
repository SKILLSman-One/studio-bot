"""Reader for the verified October studio workbook; never modifies Google Sheets."""

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
    entries, seen = [], set()
    gross_by_day = [Decimal(0)] * reconciliation_width
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
        values = [decimal(x) for x in row[6 : 6 + reconciliation_width]]
        totals = [decimal(x) for x in row[2:5]]
        calculated = [sum(values[:15]), sum(values[15:]), sum(values)]
        if any(abs(a - b) > Decimal("0.01") for a, b in zip(totals, calculated)):
            raise UserError(f"Дневные суммы не совпали с итогами в строке {number} листа «Чеки».")
        if key in placeholders:
            if any(values) or any(totals):
                raise UserError(
                    "У служебной строки есть выручка. Укажите модель в исходной таблице."
                )
            continue
        if key in seen:
            raise UserError(
                "Модель повторяется в итоговых строках «Чеки». Двойной подсчёт остановлен."
            )
        seen.add(key)
        if key not in shares and key not in inactive:
            raise UserError(f"Для итоговой строки {number} не найден процент в листе «Модели».")
        gross_by_day = [a + b for a, b in zip(gross_by_day, values)]
        mid = alias_id(name)
        for day, gross in zip(days, values):
            entries.append(
                Entry(
                    f"studio:{mid}:{day}",
                    day,
                    name=" ".join(name.split()),
                    model_id=mid,
                    amount=gross * shares[key] if key in shares else None,
                    gross=gross,
                    inactive=key in inactive,
                )
            )
    if not seen:
        raise UserError("В листе «Чеки» не найдено итоговых строк моделей.")
    # Reconcile the complete workbook total to catch omitted/unnamed model rows.
    if len(rows) < 3 or len(rows[2]) < 6 + reconciliation_width or rows[2][1] != "Сумма за день":
        raise UserError("Не найдена контрольная строка «Сумма за день» в листе «Чеки».")
    summary = [decimal(x) for x in rows[2][6 : 6 + reconciliation_width]]
    if any(abs(a - b) > Decimal("0.01") for a, b in zip(gross_by_day, summary)):
        raise UserError(
            "Сумма моделей не совпала с общей выручкой по дням. Проверьте пропущенные имена и итоговые строки «Чеки»."
        )
    return entries, (first, last)
