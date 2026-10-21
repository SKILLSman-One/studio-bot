import re
from datetime import date, datetime, time, timedelta

from .config import UserError


def next_week(today: date):
    monday = today + timedelta(days=7 - today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def require_booking_window(now: datetime, day: date | None = None):
    if now.weekday() not in (3, 4):
        raise UserError(
            "🗓 Запись на следующую неделю открыта в четверг и пятницу по времени Санкт-Петербурга. Свободное посещение доступно через «Комнаты / моя смена»."
        )
    if day is not None and day not in next_week(now.date()):
        raise UserError("Эта неделя уже не следующая. Откройте запись заново.")


def visit_times(day: date, text: str, zone):
    match = re.fullmatch(r"\s*([0-2]?\d:[0-5]\d)\s*[-–—]\s*([0-2]?\d:[0-5]\d)\s*", text)
    if not match:
        raise UserError(
            "Введите начало и конец: 14:00–19:30. Если конец раньше начала, посещение закончится на следующий день."
        )
    try:
        start, end = (time.fromisoformat(t.zfill(5)) for t in match.groups())
    except ValueError:
        raise UserError("Часы — от 00 до 23. Пример: 14:00–19:30.") from None
    if start == end:
        raise UserError("Начало и конец должны отличаться.")
    return slot_times(day, (start.isoformat(), end.isoformat()), zone)


def payment_period(payday: date):
    if payday.day not in (5, 20):
        raise UserError("Дни выплаты — 5-е и 20-е число.")
    last = payday.replace(day=1) - timedelta(days=1)
    return (
        (last.replace(day=1), last.replace(day=15))
        if payday.day == 5
        else (last.replace(day=16), last)
    )


def payment_dates(today: date):
    """Next (including today) and preceding scheduled payment dates."""
    if today.day <= 5:
        return today.replace(day=5), (today.replace(day=1) - timedelta(days=1)).replace(day=20)
    if today.day <= 20:
        return today.replace(day=20), today.replace(day=5)
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month.replace(day=5), today.replace(day=20)


def periods(today: date, mode="monthly"):
    if mode == "semimonthly":
        if today.day >= 16:
            return (today.replace(day=16), today), (today.replace(day=1), today.replace(day=15))
        previous_end = today.replace(day=1) - timedelta(days=1)
        return (today.replace(day=1), today), (previous_end.replace(day=16), previous_end)
    current = today.replace(day=1)
    previous_end = current - timedelta(days=1)
    return (current, today), (previous_end.replace(day=1), previous_end)


def parse_period(text: str):
    try:
        a, b = [date.fromisoformat(x) for x in text.split()]
        if a > b or (b - a).days > 366:
            raise ValueError()
        return a, b
    except ValueError:
        raise UserError("Введите две даты: 2026-10-01 2026-10-15. Период — до 366 дней.") from None


def slot_times(day: date, slot: tuple[str, str], zone):
    start_t, end_t = map(time.fromisoformat, slot)
    start = datetime.combine(day, start_t, zone)
    end = datetime.combine(day + timedelta(days=end_t <= start_t), end_t, zone)
    # Reject nonexistent/ambiguous local times instead of silently moving a booking at DST.
    for value in (start, end):
        if (
            datetime.fromtimestamp(value.timestamp(), zone) != value
            or value.replace(fold=1).utcoffset() != value.utcoffset()
        ):
            raise UserError("Этот слот попадает на перевод часов. Измените часы слота.")
    return start.timestamp(), end.timestamp()


def duration(seconds: float):
    minutes = max(0, int(seconds // 60))
    return f"{minutes // 60} ч {minutes % 60:02} мин"
