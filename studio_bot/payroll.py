from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import quote

from .config import UserError

COLUMNS = ("record_id", "date", "model_id", "model_name", "amount_usd")
DEFAULT_BASIS = "Начислено за период; это не остаток к выплате. Авансы не вычитаются автоматически."


def decimal(value):
    try:
        result = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
        if not result.is_finite() or abs(result) > Decimal(1000000000):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise UserError(
            "В зарплатной таблице некорректная сумма. Начисления не показаны."
        ) from None


def money(value):
    return f"{Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}".replace(
        ",", " "
    )


def amount_input(text):
    match = re.fullmatch(r"\s*([\d., ]+)\s+(USD)\s*", text, re.IGNORECASE)
    if not match:
        raise UserError("Введите сумму в долларах, например 100 USD.")
    amount = decimal(match[1])
    if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise UserError("Сумма должна быть положительной, максимум два знака после запятой.")
    return amount, match[2].upper()


def sheet_date(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not 1 <= value <= 100000:
            raise ValueError()
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError()


@dataclass(frozen=True)
class Entry:
    record_id: str
    day: date
    model_id: str
    name: str
    amount: Decimal | None
    gross: Decimal | None = None
    inactive: bool = False


def parse_rows(rows, columns=None):
    mapping = {key: (columns or {}).get(key, key) for key in COLUMNS}
    if not rows:
        raise UserError("В зарплатной таблице нет строки заголовков.")
    headers = [str(x).strip() for x in rows[0]]
    if any(headers.count(v) != 1 for v in mapping.values()):
        raise UserError(
            "Проверьте названия столбцов зарплатной таблицы и их соответствие в studio.toml."
        )
    indices = {key: headers.index(value) for key, value in mapping.items()}
    entries = []
    for number, row in enumerate(rows[1:], 2):
        if not any(str(x).strip() for x in row):
            continue
        try:
            values = {key: row[index] for key, index in indices.items()}
            rid, mid, name = [
                str(values[key]).strip() for key in ("record_id", "model_id", "model_name")
            ]
            if (
                not rid
                or not mid
                or not name
                or len(mid) > 64
                or len(name) > 60
                or any(c.isspace() for c in mid)
            ):
                raise ValueError()
            if any(ord(c) < 32 for c in name):
                raise ValueError()
            entries.append(
                Entry(rid, sheet_date(values["date"]), mid, name, decimal(values["amount_usd"]))
            )
        except (KeyError, IndexError, ValueError):
            raise UserError(
                f"Ошибка зарплатной таблицы в строке {number}. Проверьте ID, дату, имя и сумму."
            ) from None
    return entries


@dataclass(frozen=True)
class Snapshot:
    entries: tuple[Entry, ...]
    fetched_at: datetime
    coverage: tuple[tuple[date, date], ...] | None = None
    basis: str = DEFAULT_BASIS

    def require_period(self, start, end):
        if self.coverage is None:
            return
        day = start
        while day <= end:
            if not any(a <= day <= b for a, b in self.coverage):
                raise UserError(
                    f"Нет подключённых данных за {day:%d.%m.%Y}. Добавьте таблицу за этот месяц; отсутствие данных не означает нулевую зарплату."
                )
            day += timedelta(days=1)

    def totals(self, start, end, *, gross=False, model_id=None):
        self.require_period(start, end)
        totals = {}
        for entry in self.entries:
            if start <= entry.day <= end and (model_id is None or entry.model_id == model_id):
                value = entry.gross if gross else entry.amount
                if value is None:
                    if not gross:
                        raise UserError(
                            "Для бывшего сотрудника нет исторического процента. Зарплата за этот период не определена."
                        )
                    raise UserError(
                        "В этом источнике нет полного оборота. Подключите вкладку «Чеки»; начисления модели не заменяют оборот."
                    )
                totals[entry.model_id] = totals.get(entry.model_id, Decimal(0)) + value
        return totals

    def names(self, *, active_only=False):
        entries = self.entries
        if active_only and entries:
            latest_month = max(e.day for e in entries).strftime("%Y-%m")
            entries = tuple(e for e in entries if e.day.strftime("%Y-%m") == latest_month)
            inactive = {e.model_id for e in entries if e.inactive}
            entries = tuple(e for e in entries if e.model_id not in inactive)
        return {entry.model_id: entry.name for entry in entries}

    def amount(self, mid, period, *, gross=False):
        if self.coverage is not None and mid not in self.names():
            raise UserError(
                "Псевдоним модели не найден в подключённых таблицах. Проверьте зарплатную привязку."
            )
        return self.totals(*period, gross=gross, model_id=mid).get(mid, Decimal(0))


class SheetsPayroll:
    """Private Sheets, read-only scope, whole-snapshot validation; no stale fallback."""

    def __init__(self, settings):
        self.settings = settings
        self.cache = None
        self.cached_at = 0
        self.lock = asyncio.Lock()
        self.last_attempt = None
        self.last_success = None
        self.last_error = None
        self.last_coverage = None
        self.last_months = ()

    def _read_sync(self):
        if not self.settings.sources:
            raise UserError(
                "Зарплатная таблица ещё не подключена. Администратору нужно заполнить payroll_sources в studio.toml."
            )
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        from .studio_sheet import SHARE_BASIS, alias_key, parse_studio_matrix

        matrix = [s.get("format", "rows") == "studio_daily_share" for s in self.settings.sources]
        if any(matrix) and not all(matrix):
            raise UserError(
                "Не смешивайте выручку × процент и готовые начисления в одном источнике зарплат."
            )
        for source in self.settings.sources:
            if source.get("format", "rows") not in ("rows", "studio_daily_share"):
                raise UserError("Неизвестный формат зарплатного источника.")
            if (
                source.get("format") == "studio_daily_share"
                and source.get("calculation_confirmed") is not True
            ):
                raise UserError(
                    "Нужно подтвердить правило заработка: выручка × процент модели до удержаний, либо подключить готовую ведомость."
                )

        try:
            creds = Credentials.from_service_account_file(
                str(self.settings.credentials_path),
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
            )
            entries = []
            coverage = []
            with AuthorizedSession(creds, refresh_timeout=15) as session:
                for source in self.settings.sources:
                    sid = source["spreadsheet_id"]
                    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", sid):
                        raise UserError("Проверьте spreadsheet_id в studio.toml.")
                    if source.get("format") == "studio_daily_share":
                        # One batch request keeps daily gross and shares in the same response.
                        response = session.get(
                            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchGet",
                            params={
                                "ranges": [source["range"], source["shares_range"]],
                                "valueRenderOption": "UNFORMATTED_VALUE",
                                "dateTimeRenderOption": "SERIAL_NUMBER",
                            },
                            timeout=20,
                        )
                        response.raise_for_status()
                        ranges = response.json()["valueRanges"]
                        if len(ranges) != 2:
                            raise UserError("Google вернул неполный набор зарплатных диапазонов.")
                        parsed, period = parse_studio_matrix(
                            ranges[0].get("values", []), ranges[1].get("values", []), source
                        )
                        entries.extend(parsed)
                        coverage.append(period)
                    else:
                        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{quote(source['range'], safe='')}"
                        response = session.get(
                            url,
                            params={
                                "valueRenderOption": "UNFORMATTED_VALUE",
                                "dateTimeRenderOption": "SERIAL_NUMBER",
                            },
                            timeout=20,
                        )
                        response.raise_for_status()
                        entries.extend(
                            parse_rows(response.json().get("values", []), source.get("columns"))
                        )
            identifiers = [entry.record_id for entry in entries]
            if len(set(identifiers)) != len(identifiers):
                raise UserError(
                    "Повторяются record_id начислений. Уберите пересечение источников или исправьте ID строк."
                )
            names = {}
            for entry in entries:
                if entry.model_id in names and alias_key(names[entry.model_id]) != alias_key(
                    entry.name
                ):
                    raise UserError("Один model_id связан с разными именами в зарплатной таблице.")
                names[entry.model_id] = entry.name
            if coverage:
                for index, (a, b) in enumerate(coverage):
                    if any(a <= y and x <= b for x, y in coverage[:index]):
                        raise UserError(
                            "Подключено несколько источников за один месяц. Уберите пересечение."
                        )
            return Snapshot(
                tuple(entries),
                datetime.now(timezone.utc),
                tuple(coverage) if all(matrix) else None,
                SHARE_BASIS if all(matrix) else DEFAULT_BASIS,
            )
        except UserError:
            raise
        except FileNotFoundError:
            raise UserError(
                "Ключ сервисного аккаунта Google не найден. Сохраните его в data/google-service-account.json на Windows или secrets/google-service-account.json для Docker. Вход Google Drive в чате не заменяет серверный ключ."
            ) from None
        except Exception:
            raise UserError(
                "Не удалось прочитать Google-таблицу. Проверьте сеть, ключ и доступ сервисного аккаунта. Старые суммы не подставлены."
            ) from None

    async def get(self, force=False):
        async with self.lock:
            if self.cache is None or force or time.monotonic() - self.cached_at >= 60:
                self.last_attempt = datetime.now(timezone.utc)
                try:
                    self.cache = await asyncio.to_thread(self._read_sync)
                except Exception as error:
                    self.cache = None
                    self.last_error = (
                        str(error)
                        if isinstance(error, UserError)
                        else "Не удалось прочитать Google-таблицу."
                    )
                    raise
                self.cached_at = time.monotonic()
                self.last_success = self.cache.fetched_at
                self.last_coverage = self.cache.coverage
                self.last_months = tuple(
                    sorted({entry.day.strftime("%Y-%m") for entry in self.cache.entries})
                )
                self.last_error = None
            return self.cache


def report_text(snapshot, period, people=None, model_id=None, *, gross=False, active_only=False):
    totals = snapshot.totals(*period, gross=gross, model_id=model_id)
    names = snapshot.names(active_only=active_only)
    if people:
        names.update(
            {
                p["payroll_id"]: p["name"]
                for p in people
                if p["payroll_id"] and (not active_only or p["payroll_id"] in names)
            }
        )
    if active_only:
        totals = {mid: value for mid, value in totals.items() if mid in names}
    ids = (
        [model_id]
        if model_id is not None
        else sorted(set(totals) | set(names), key=lambda x: names.get(x, x))
    )
    title = "Полный оборот" if gross else "Заработок"
    lines = [f"{title}: {period[0]:%d.%m.%Y} — {period[1]:%d.%m.%Y}", ""]
    for mid in ids:
        amount = totals.get(mid, Decimal(0))
        lines.append(f"{names.get(mid, mid)}: ${money(amount)}")
    total = sum((totals.get(mid, Decimal(0)) for mid in ids), Decimal(0))
    if model_id is None:
        lines += [
            "",
            f"Всего: ${money(total)}",
        ]
    if model_id is not None and model_id not in snapshot.names():
        lines += ["Строк для вашего зарплатного ID пока нет. Проверьте привязку у администратора."]
    return "\n".join(lines)


def report_csv(snapshot, period, people, *, gross=False, active_only=False):
    names = snapshot.names(active_only=active_only)
    names.update(
        {
            p["payroll_id"]: p["name"]
            for p in people
            if p["payroll_id"] and (not active_only or p["payroll_id"] in names)
        }
    )
    totals = snapshot.totals(*period, gross=gross)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "model_id",
            "модель",
            "оборот_USD" if gross else "начислено_USD",
            "период_с",
            "период_по",
        ]
    )
    for mid in sorted(names):
        amount = totals.get(mid, Decimal(0))

        # Excel must not evaluate values from spreadsheet labels as formulas.
        def safe(x):
            return "'" + x if x.lstrip().startswith(("=", "+", "-", "@")) else x

        writer.writerow(
            [
                safe(mid),
                safe(names[mid]),
                money(amount),
                *period,
            ]
        )
    return output.getvalue().encode("utf-8-sig")
