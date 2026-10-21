from __future__ import annotations

import asyncio
import json
import secrets
from collections import Counter
from contextlib import suppress
from datetime import date, datetime, time, timedelta

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
)

from . import __version__
from .config import UserError
from .dates import (
    duration,
    next_week,
    parse_period,
    payment_dates,
    payment_period,
    periods,
    require_booking_window,
    visit_times,
)
from .payroll import amount_input, money, report_csv, report_text
from .presentation import button_label, room_name

PAGE = 8
LABELS = {
    "conflict": "⚠️ на разборе у администратора; брони пока нет",
    "pending": "ожидает",
    "approved": "подтверждено",
    "rejected": "отклонено",
    "cancelled": "отменено",
    "paid": "выплачено",
}


def keyboard(rows, role, epoch):
    prefix = ("a:" if role == "admin" else "m:") + epoch + ":"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=button_label(label, data), callback_data=prefix + data)
                for label, data in row
            ]
            for row in rows
        ]
    )


class Gate(BaseMiddleware):
    def __init__(self, ui):
        self.ui = ui
        self.locks = {}

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        message = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(message, "chat", None)
        if not user or not chat or chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ закрыт.", show_alert=True)
            return None
        lock = self.locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            if self.ui.store.is_banned(user.id):
                self.ui.auth.logout(user.id)
                self.ui.pending.pop(("admin", user.id), None)
                self.ui.pending.pop(("model", user.id), None)
                text = "Доступ к боту заблокирован администратором студии."
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                else:
                    await self.ui.send("model", user.id, text)
                return None
            if isinstance(event, CallbackQuery):
                payload = event.data or ""
                parts = payload.split(":", 2)
                if (
                    len(parts) != 3
                    or parts[0] not in {"a", "m"}
                    or parts[1] != self.ui.store.callback_epoch
                ):
                    await event.answer(
                        "Кнопка устарела. Откройте /start или /admin заново.", show_alert=True
                    )
                    return None
                role = "admin" if payload.startswith("a:") else "model"
                if role == "model":
                    self.ui.auth.logout(user.id)
                    self.ui.pending.pop(("admin", user.id), None)
            else:
                words = (event.text or "").split(maxsplit=1)
                command = words[0] if words else ""
                command = command.split("@")[0]
                role = (
                    "admin"
                    if (
                        command in {"/admin", "/login"}
                        or self.ui.auth.logged_in(user.id)
                        or ("admin", user.id) in self.ui.pending
                    )
                    else "model"
                )
            if role == "admin" and not self.ui.auth.allowed(user.id):
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ закрыт.", show_alert=True)
                return None
            data["role"] = role
            return await handler(event, data)


class UI:
    def __init__(self, settings, store, payroll, bots, auth):
        self.settings, self.store, self.payroll = settings, store, payroll
        self.bots, self.auth = bots, auth
        self.pending = {}
        self.started_at = store.clock()
        self.dispatcher = Dispatcher()
        router = Router()
        gate = Gate(self)
        router.message.outer_middleware(gate)
        router.callback_query.outer_middleware(gate)
        router.message.register(self.on_message)
        router.callback_query.register(self.on_callback)
        self.dispatcher.include_router(router)

    def now(self):
        return datetime.fromtimestamp(self.store.clock(), self.settings.zone)

    def when(self, stamp):
        return datetime.fromtimestamp(stamp, self.settings.zone).strftime("%d.%m %H:%M")

    async def send(self, role, uid, text, rows=None):
        if self.store.is_banned(uid):
            text, rows = "Доступ к боту заблокирован администратором студии.", None
        if len(text.encode("utf-16-le")) // 2 > 3900:
            await self.bots[role].send_document(
                uid,
                BufferedInputFile(text.encode("utf-8-sig"), filename="svodka.txt"),
                caption="Полная структурированная сводка в одном файле.",
                reply_markup=keyboard(rows, role, self.store.callback_epoch) if rows else None,
            )
        else:
            await self.bots[role].send_message(
                uid,
                text,
                parse_mode=None,
                reply_markup=keyboard(rows, role, self.store.callback_epoch) if rows else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    async def home(self, role, uid):
        self.pending.pop((role, uid), None)
        if role == "admin":
            await self.send(
                role,
                uid,
                "🪄 СТУДИЯ · АДМИНИСТРАТОР\n\nВсё под рукой ♡\nСанкт-Петербург · выберите раздел",
                [
                    [("Сейчас в студии", "rooms"), ("Использование комнат", "usage")],
                    [("Анкеты и модели", "people:0")],
                    [
                        ("Заблокировать", "accesslist:ban:0"),
                        ("Разблокировать", "accesslist:unban:0"),
                    ],
                    [("Записи на разбор", "bookings:0"), ("Расписание", "calendar")],
                    [("Полный оборот моделей", "salary")],
                    [("Авансы", "advances:0"), ("Обращения", "tickets:0")],
                    [("Состояние бота", "health"), ("Уведомления", "delivery")],
                    [("Выйти", "logout")],
                ],
            )
            return
        user = self.store.user(uid)
        if not user:
            await self.send(
                role,
                uid,
                "🌷 СТУДИЯ · КАБИНЕТ СОТРУДНИКА\n\nДобро пожаловать, UwU ♡\nЗдесь твои комнаты, расписание и заработок. Укажи рабочее имя, чтобы администратор открыл доступ.\n\nВход администратора: /admin",
                [[("Указать рабочее имя", "register")]],
            )
        elif user["status"] != "approved":
            await self.send(
                role,
                uid,
                f"Анкета: {LABELS[user['status']]}\nДоступ к сменам и начислениям открывается после проверки администратором.",
                ([[("Подать анкету заново", "register")]] if user["status"] == "rejected" else [])
                + [[("Связаться с админом", "contact")], [("Обновить", "home")]],
            )
        else:
            await self.send(
                role,
                uid,
                f"🌷 СТУДИЯ · ЛИЧНЫЙ КАБИНЕТ\n\nПривет, {user['name']} ♡\nРады видеть тебя, UwU\n\nСанкт-Петербург · {self.now():%d.%m.%Y %H:%M}",
                [
                    [("Комнаты / моя смена", "rooms")],
                    [("Записаться · чт–пт", "week"), ("Мои записи", "mybookings:0")],
                    [("Мой доход · USD", "salary")],
                    [("Заявка на аванс", "advance"), ("Мои авансы", "myadvances")],
                    [("Связаться с админом", "contact")],
                ],
            )

    async def login_prompt(self, uid):
        self.pending.pop(("model", uid), None)
        self.pending[("admin", uid)] = ("login",)
        await self.send(
            "admin",
            uid,
            "Введите пароль администратора отдельным сообщением. Сообщение будет удалено по возможности; вход действует 1 час. /cancel — отмена.",
        )

    async def on_message(self, message, role):
        uid, text = message.from_user.id, message.text or ""
        key = role, uid
        try:
            command = text.split(maxsplit=1)[0].split("@")[0] if text.startswith("/") else ""
            if command in {"/model", "/logout"} or (
                command == "/cancel" and self.pending.get(("admin", uid), (None,))[0] == "login"
            ):
                self.auth.logout(uid)
                self.pending.pop(("admin", uid), None)
                await self.home("model", uid)
                return
            if command in {"/admin", "/login"}:
                self.pending.pop(("model", uid), None)
                if self.auth.logged_in(uid):
                    await self.home("admin", uid)
                else:
                    await self.login_prompt(uid)
                return
            if role == "admin" and not self.auth.logged_in(uid):
                if self.pending.get(key, (None,))[0] != "login" or text.startswith("/"):
                    if text == "/cancel":
                        self.pending.pop(key, None)
                        return
                    await self.login_prompt(uid)
                    return
                with suppress(Exception):
                    await message.delete()
                if await self.auth.login(uid, text):
                    await self.home(role, uid)
                else:
                    await self.send(
                        role,
                        uid,
                        "Пароль не принят или вход временно заблокирован. После 5 ошибок — пауза 15 минут. /cancel — отмена.",
                    )
                return
            if command in {"/start", "/menu", "/cancel"}:
                await self.home(role, uid)
                return
            state = self.pending.get(key, (None,))
            if not text:
                raise UserError(
                    "Отправьте текст. Файлы и вложения в этой версии не обрабатываются."
                )
            if state[0] == "name" and role == "model":
                if not 2 <= len(text.strip()) <= 60 or any(ord(c) < 32 for c in text):
                    raise UserError("Имя: 2–60 символов, одной строкой.")
                self.store.register(uid, text.strip(), message.from_user.username or "", "")
                self.pending.pop(key, None)
                self.store.alert_admins(
                    self.auth.recipients(),
                    "Сотрудник запросил доступ к кабинету. Откройте /admin → Анкеты и модели.",
                )
                await self.home(role, uid)
            elif state[0] == "visit_time" and role == "model":
                self.store.approved(uid)
                day = state[1]
                require_booking_window(self.now(), day)
                start, end = visit_times(day, text, self.settings.zone)
                data = dict(
                    day=str(day),
                    start=start,
                    end=end,
                    token=secrets.token_hex(4),
                    issued=self.store.clock(),
                )
                self.pending[key] = ("visit_room", data)
                await self.visit_rooms(uid, data)
            elif state[0] == "booking_time" and role == "admin":
                row = self.store.one("SELECT * FROM bookings WHERE id=?", (state[1],))
                if not row or row["state"] not in {"pending", "conflict"}:
                    raise UserError("Заявка уже обработана. Откройте список заново.")
                start, end = visit_times(date.fromisoformat(row["day"]), text, self.settings.zone)
                data = dict(
                    bid=row["id"],
                    start=start,
                    end=end,
                    token=secrets.token_hex(4),
                    issued=self.store.clock(),
                )
                self.pending[key] = ("booking_edit", data)
                await self.send(
                    "admin",
                    uid,
                    f"📅 Новое время: {self.when(start)} — {self.when(end)}\nВыберите комнату, чтобы подтвердить запись:",
                    [
                        [(room_name(room), f"bookedit:{data['token']}:{i}")]
                        for i, room in enumerate(self.settings.rooms)
                    ]
                    + [[("Отмена", "bookings:0")]],
                )
            elif state[0] == "period":
                await self.salary(role, uid, parse_period(text))
                self.pending.pop(key, None)
            elif state[0] == "usage_period" and role == "admin":
                await self.usage(uid, parse_period(text))
                self.pending.pop(key, None)
            elif state[0] == "contact" and role == "model":
                tid = self.store.ticket(uid, text)
                self.pending.pop(key, None)
                self.store.alert_admins(
                    self.auth.recipients(), "Новое обращение модели. Откройте /admin → Обращения."
                )
                await self.send(
                    role,
                    uid,
                    f"Обращение №{tid} сохранено. Ответ придёт сюда.",
                    [[("Меню", "home")]],
                )
            elif state[0] == "answer" and role == "admin":
                self.store.answer(state[1], uid, text)
                self.pending.pop(key, None)
                await self.send(
                    role,
                    uid,
                    "Ответ сохранён и поставлен в очередь доставки.",
                    [[("Обращения", "tickets:0")]],
                )
            elif state[0] == "advance_amount" and role == "model":
                amount, currency = amount_input(text)
                token = secrets.token_hex(4)
                self.pending[key] = ("advance_confirm", token, amount, currency, state[1])
                await self.send(
                    role,
                    uid,
                    f"Заявка на {money(amount)} {currency}\n\n{self.snapshot_text(state[1])}\n\nЭто запрос администратору, а не автоматическая выплата.",
                    [[("Отправить заявку", f"advanceconfirm:{token}")], [("Отмена", "home")]],
                )
            else:
                await self.home(role, uid)
        except UserError as error:
            await self.send(role, uid, str(error), [[("Меню", "home")]])

    async def on_callback(self, query, role):
        uid = query.from_user.id
        with suppress(Exception):
            await query.answer()
        if role == "admin" and not self.auth.logged_in(uid):
            await self.login_prompt(uid)
            return
        try:
            await self.action(role, uid, (query.data or "").split(":", 2)[2].split(":"))
        except UserError as error:
            await self.send(role, uid, str(error), [[("Меню", "home")]])
        except (ValueError, IndexError, KeyError):
            await self.send(
                role, uid, "Кнопка устарела. Откройте меню заново.", [[("Меню", "home")]]
            )

    async def action(self, role, uid, p):
        action, key = p[0], (role, uid)
        if action == "home":
            await self.home(role, uid)
            return
        if action == "rate":
            await self.home(role, uid)
            return
        if role == "model":
            if action == "register":
                existing = self.store.user(uid)
                if existing and existing["status"] != "rejected":
                    raise UserError("Анкета уже сохранена.")
                self.pending[key] = ("name",)
                await self.send(
                    role,
                    uid,
                    "Укажите рабочее имя или псевдоним, как в таблице студии (2–60 символов).",
                )
                return
            if action == "contact":
                if not self.store.user(uid):
                    raise UserError("Сначала заполните анкету.")
                self.pending[key] = ("contact",)
                await self.send(
                    role,
                    uid,
                    "Напишите сообщение администратору одним текстом (до 2000 символов). /cancel — отмена.",
                )
                return
            self.store.approved(uid)
        if action == "rooms":
            await self.rooms(role, uid)
        elif action == "salary":
            if len(p) == 1:
                upcoming, previous = payment_dates(self.now().date())
                await self.send(
                    role,
                    uid,
                    "Выберите период оборота:"
                    if role == "admin"
                    else "💵 Мой доход · USD\n\nВыплаты 5-го — за 1–15 прошлого месяца, 20-го — за 16–конец прошлого месяца.\nВыберите дату выплаты или период заработка:",
                    [
                        [(f"Ближайшая выплата · {upcoming:%d.%m.%Y}", f"payday:{upcoming}")],
                        [(f"Предыдущая дата · {previous:%d.%m.%Y}", f"payday:{previous}")],
                        [
                            ("Текущий период", "salary:current"),
                            ("Прошлый период", "salary:previous"),
                        ],
                        [("Другие даты", "customperiod")],
                        [("Меню", "home")],
                    ],
                )
            else:
                period = periods(self.now().date(), self.settings.payroll_period)[
                    0 if p[1] == "current" else 1
                ]
                await self.salary(role, uid, period)
        elif action == "payday":
            payday = date.fromisoformat(p[1])
            await self.salary(role, uid, payment_period(payday), payday=payday)
        elif action == "salarydates":
            await self.salary(role, uid, parse_period(f"{p[1]} {p[2]}"))
        elif action == "customperiod":
            self.pending[key] = ("period",)
            await self.send(role, uid, "Введите начало и конец периода: 2026-10-01 2026-10-15.")
        elif role == "model":
            await self.model_action(uid, p)
        else:
            await self.admin_action(uid, p)

    async def rooms(self, role, uid):
        active = {row["room"]: row for row in self.store.active_shifts()}
        lines = [f"🚪 Комнаты студии · {self.now():%d.%m %H:%M}", ""]
        rows = []
        mine = next((s for s in active.values() if s["user_id"] == uid), None)
        for index, room in enumerate(self.settings.rooms):
            row = active.get(room)
            if row:
                who = row["name"] if role == "admin" or row["user_id"] == uid else "занята"
                lines.append(
                    f"🔴 {room_name(room)}: {who} • {duration(self.store.clock() - row['started'])} • с {self.when(row['started'])}"
                )
                if role == "admin":
                    rows.append([(f"Завершить смену: {room_name(room)}", f"askend:{row['id']}")])
            else:
                reservation = self.store.one(
                    "SELECT user_id FROM bookings WHERE state='approved' AND room=? AND started<=? AND ended>?",
                    (room, self.store.clock(), self.store.clock()),
                )
                lines.append(
                    f"{'🟡' if reservation else '🟢'} {room_name(room)}: "
                    + ("есть запись на это время" if reservation else "свободна")
                )
                if (
                    role == "model"
                    and not mine
                    and (not reservation or reservation["user_id"] == uid)
                ):
                    rows.append([(f"Начать смену — {room_name(room)}", f"room:{index}")])
        if mine and role == "model":
            rows.append([("Завершить мою смену", f"end:{mine['id']}")])
        lines += [
            "",
            "Занятость отмечается началом/концом смены в боте. Бронь сама смену не запускает.",
        ]
        rows += [[("Обновить", "rooms"), ("Меню", "home")]]
        await self.send(role, uid, "\n".join(lines), rows)

    async def salary(self, role, uid, period, payday=None):
        person = self.store.approved(uid) if role == "model" else None
        if person is not None and not person["payroll_id"]:
            raise UserError("Администратор ещё не привязал вашу анкету к зарплатной таблице.")
        snapshot = await self.payroll.get(force=True)
        self.store.require_access(uid)
        people = (
            self.store.all("SELECT * FROM people WHERE status='approved'")
            if role == "admin"
            else None
        )
        text = report_text(
            snapshot,
            period,
            people,
            person["payroll_id"] if person else None,
            gross=role == "admin",
            active_only=role == "admin",
        )
        if payday is not None:
            text = f"💵 Дата выплаты: {payday:%d.%m.%Y}\n\n" + text
            if role == "model":
                earned = snapshot.amount(person["payroll_id"], period)
                deduction = self.store.advance_deduction(uid, payday)
                text += (
                    f"\n\nАвансы: ${money(deduction)}\nК получению: ${money(earned - deduction)}"
                )
                if deduction > earned:
                    text += "\nАвансы превышают начисление. Уточните расчёт у администратора."
        rows = [
            [
                (
                    "Обновить",
                    f"payday:{payday}" if payday else f"salarydates:{period[0]}:{period[1]}",
                ),
                ("Период", "salary"),
            ],
            [("Меню", "home")],
        ]
        if role == "admin" and len(text.encode("utf-16-le")) // 2 > 3900:
            await self.bots[role].send_document(
                uid,
                BufferedInputFile(
                    report_csv(snapshot, period, people, gross=True, active_only=True),
                    filename="oborot.csv",
                ),
                caption=f"Полный оборот моделей за {period[0]:%d.%m.%Y} — {period[1]:%d.%m.%Y}.",
                reply_markup=keyboard(rows, role, self.store.callback_epoch),
            )
        else:
            await self.send(role, uid, text, rows)

    async def model_action(self, uid, p):
        action, key = p[0], ("model", uid)
        if action == "room":
            self.store.start_shift(uid, self.settings.rooms[int(p[1])], self.settings.rooms)
            await self.rooms("model", uid)
        elif action == "end":
            await self.clean_room_prompt(uid, int(p[1]))
        elif action == "cleanconfirm":
            state = self.pending.get(key, (None,))
            if (
                state[0] != "clean_confirm"
                or state[2] != p[1]
                or self.store.clock() - state[3] > 1800
            ):
                raise UserError(
                    "Подтверждение устарело. Откройте «Комнаты / моя смена» и нажмите завершение ещё раз."
                )
            length = self.store.end_shift(state[1], uid, clean_confirmed=True)
            self.pending.pop(key, None)
            await self.send(
                "model",
                uid,
                f"🌙 Смена завершена. Продолжительность: {duration(length)}.\nСпасибо за чистоту, UwU ♡",
                [[("Меню", "home")]],
            )
        elif action == "week":
            require_booking_window(self.now())
            days = next_week(self.now().date())
            weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
            await self.send(
                "model",
                uid,
                "🗓 Запись на следующую неделю ♡\nВыбери день, напиши время начала и конца, затем выбери комнату. Свободная комната бронируется сразу.\nВремя Санкт-Петербурга. Запись открыта только в четверг и пятницу.",
                [[(f"{d:%d.%m} · {weekdays[d.weekday()]}", f"day:{d}")] for d in days]
                + [[("Мои записи", "mybookings:0"), ("Меню", "home")]],
            )
        elif action == "day":
            day = date.fromisoformat(p[1])
            require_booking_window(self.now(), day)
            self.pending[key] = ("visit_time", day)
            await self.send(
                "model",
                uid,
                f"📅 {day:%d.%m.%Y} · Санкт-Петербург\nНапиши начало и конец: 14:00–19:30.\nЕсли конец раньше начала, посещение закончится на следующий день. /cancel — отмена.",
            )
        elif action == "slot":
            raise UserError("Теперь выбираем комнату и своё время ♡ Откройте запись заново.")
        elif action == "bookroom":
            data = self.visit_state(uid, "visit_room", p[1])
            index = int(p[2])
            if not 0 <= index < len(self.settings.rooms):
                raise UserError("Комната не найдена.")
            data = dict(data, room=self.settings.rooms[index], token=secrets.token_hex(4))
            self.pending[key] = ("visit_confirm", data)
            conflict = bool(self.store.room_conflicts(data["room"], data["start"], data["end"]))
            detail = (
                "⚠️ На это время есть бронь. Отправить запрос администратору? До его решения комната не закрепляется."
                if conflict
                else "🟢 Время свободно. Подтвердить запись?"
            )
            await self.send(
                "model",
                uid,
                f"🚪 {room_name(data['room'])}\n{self.when(data['start'])} — {self.when(data['end'])}\n\n{detail}\nЕсли время займут до подтверждения, запрос отправится администратору.",
                [
                    [
                        (
                            "Отправить на разбор" if conflict else "Забронировать",
                            f"bookconfirm:{data['token']}",
                        )
                    ],
                    [("Отмена", "home")],
                ],
            )
        elif action == "bookconfirm":
            data = self.visit_state(uid, "visit_confirm", p[1])
            _, state = self.store.book_room(
                uid,
                data["day"],
                data["room"],
                data["start"],
                data["end"],
                self.settings,
                self.auth.recipients(),
            )
            self.pending.pop(key, None)
            await self.send(
                "model",
                uid,
                (
                    "⚠️ Время пересекается с другой записью. Администратору отправлено уведомление; комната пока не забронирована."
                    if state == "conflict"
                    else "✅ Комната за тобой, UwU ♡"
                )
                + f"\n{room_name(data['room'])} · {self.when(data['start'])} — {self.when(data['end'])}",
                [[("Мои записи", "mybookings:0"), ("Добавить ещё", "week")]],
            )
        elif action == "mybookings":
            await self.list_bookings("model", uid, int(p[1]))
        elif action == "cancelbook":
            self.store.decide_booking(int(p[1]), uid, reject=True, owner=True)
            await self.list_bookings("model", uid, 0)
        elif action == "advance":
            person = self.store.approved(uid)
            if not person["payroll_id"]:
                raise UserError(
                    "Для заявки на аванс администратору нужно привязать вашу зарплатную строку."
                )
            snapshot = await self.payroll.get(force=True)
            self.store.require_access(uid)
            current, previous = periods(self.now().date(), self.settings.payroll_period)
            payload = {
                "current": str(snapshot.amount(person["payroll_id"], current)),
                "previous": str(snapshot.amount(person["payroll_id"], previous)),
                "current_period": f"{current[0]} — {current[1]}",
                "previous_period": f"{previous[0]} — {previous[1]}",
                "fetched": snapshot.fetched_at.isoformat(),
                "basis": snapshot.basis,
                "preview_at": self.store.clock(),
            }
            self.pending[key] = ("advance_amount", payload)
            await self.send(
                "model",
                uid,
                self.snapshot_text(payload)
                + "\n\nВведите желаемую сумму: 100 USD. /cancel — отмена.",
            )
        elif action == "advanceconfirm":
            state = self.pending.get(key, (None,))
            if state[0] != "advance_confirm" or state[1] != p[1]:
                raise UserError("Подтверждение устарело. Создайте заявку заново.")
            if self.store.clock() - state[4]["preview_at"] > 300:
                raise UserError(
                    "Предпросмотр старше 5 минут. Нажмите «Заявка на аванс» заново для свежих сумм."
                )
            aid = self.store.advance(uid, state[2], state[3], state[4])
            self.pending.pop(key, None)
            self.store.alert_admins(
                self.auth.recipients(), "Поступила заявка на аванс. Откройте /admin → Авансы."
            )
            await self.send(
                "model",
                uid,
                f"Заявка №{aid} сохранена. Администратор увидит сумму и начисления за оба периода.",
                [[("Мои авансы", "myadvances")]],
            )
        elif action == "myadvances":
            rows = self.store.all(
                "SELECT * FROM advances WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,)
            )
            await self.send(
                "model",
                uid,
                "Последние заявки:\n"
                + (
                    "\n".join(
                        f"#{r['id']} • {r['amount']} {r['currency']} • {LABELS[r['state']]}"
                        for r in rows
                    )
                    or "Заявок пока нет."
                ),
                [[("Меню", "home")]],
            )
        else:
            raise UserError("Эта команда недоступна в кабинете модели.")

    async def clean_room_prompt(self, uid, sid):
        self.pending.pop(("model", uid), None)
        shift = self.store.one(
            "SELECT * FROM shifts WHERE id=? AND user_id=? AND ended IS NULL", (sid, uid)
        )
        if not shift:
            raise UserError("Смена не найдена или уже завершена.")
        paths = self.settings.photos(shift["room"])
        if paths:
            try:
                if any(not p.is_file() or not 0 < p.stat().st_size < 10_000_000 for p in paths):
                    raise OSError()
                await self.bots["model"].send_media_group(
                    uid,
                    media=[
                        InputMediaPhoto(
                            media=FSInputFile(path),
                            caption=f"✨ {room_name(shift['room'])} · вот так должно быть чисто ♡"
                            if i == 0
                            else None,
                        )
                        for i, path in enumerate(paths)
                    ],
                )
            except (OSError, TelegramAPIError):
                raise UserError(
                    "Не удалось показать фотографии. Смена ещё идёт. Повторите завершение или свяжитесь с администратором."
                ) from None
        self.store.approved(uid)
        if not self.store.one(
            "SELECT 1 FROM shifts WHERE id=? AND user_id=? AND ended IS NULL", (sid, uid)
        ):
            raise UserError("Эта смена уже завершена.")
        token = secrets.token_hex(4)
        self.pending[("model", uid)] = ("clean_confirm", sid, token, self.store.clock())
        await self.send(
            "model",
            uid,
            f"🧹 {room_name(shift['room'])}\n\n"
            + (
                "У тебя так же чисто, как на фотографиях? ✨"
                if paths
                else "В комнате всё чисто и убрано? ✨"
            )
            + "\nНажми после уборки — и смена завершится ♡",
            [[("Да, всё чисто", f"cleanconfirm:{token}")]],
        )

    def visit_state(self, uid, expected, token):
        state = self.pending.get(("model", uid), (None,))
        if (
            state[0] != expected
            or state[1]["token"] != token
            or self.store.clock() - state[1]["issued"] > 300
        ):
            raise UserError("Выбор устарел. Начните запись заново.")
        require_booking_window(self.now(), date.fromisoformat(state[1]["day"]))
        return state[1]

    async def visit_rooms(self, uid, data):
        rows = []
        for i, room in enumerate(self.settings.rooms):
            busy = bool(self.store.room_conflicts(room, data["start"], data["end"]))
            rows.append(
                [
                    (
                        f"{'🟡' if busy else '🟢'} {room_name(room)}"
                        + (" · на разбор" if busy else ""),
                        f"bookroom:{data['token']}:{i}",
                    )
                ]
            )
        await self.send(
            "model",
            uid,
            f"🚪 Выбери комнату ♡\n{self.when(data['start'])} — {self.when(data['end'])}\n\n🟢 Свободна на весь интервал\n🟡 Есть пересечение — можно отправить запрос администратору",
            rows + [[("Другой день", "week"), ("Отмена", "home")]],
        )

    @staticmethod
    def snapshot_text(data):
        # Keep the original USD amounts in historical snapshots; hide retired conversions.
        return (
            f"Текущий период ({data['current_period']}):\n${money(data['current'])}\n\n"
            f"Прошлый период ({data['previous_period']}):\n${money(data['previous'])}"
        )

    def pages(self, prefix, page, total):
        row = []
        if page > 0:
            row.append(("←", f"{prefix}:{page - 1}"))
        if (page + 1) * PAGE < total:
            row.append(("→", f"{prefix}:{page + 1}"))
        return ([row] if row else []) + [[("Меню", "home")]]

    def binding_person(self, target):
        self.store.require_access(target)
        person = self.store.user(target)
        if (
            not person
            or person["payroll_id"]
            or person["status"] not in {"pending", "rejected", "approved"}
        ):
            raise UserError("Сотрудник уже привязан или недоступен. Обновите карточку.")
        return person

    async def start_binding(self, uid, target):
        self.pending.pop(("admin", uid), None)
        self.binding_person(target)
        snapshot = await self.payroll.get(force=True)
        if not self.auth.logged_in(uid):
            raise UserError("Войдите в админку заново через /admin.")
        self.binding_person(target)
        from .studio_sheet import alias_key

        names = snapshot.names(active_only=True)
        counts = Counter(alias_key(name) for name in names.values())
        used = {
            row["payroll_id"]
            for row in self.store.all("SELECT payroll_id FROM people WHERE payroll_id IS NOT NULL")
        }
        candidates = sorted(
            (
                (mid, name)
                for mid, name in names.items()
                if mid not in used and counts[alias_key(name)] == 1
            ),
            key=lambda item: (alias_key(item[1]), item[0]),
        )
        state = {
            "target": target,
            "token": secrets.token_hex(4),
            "issued": self.store.clock(),
            "candidates": candidates,
            "duplicates": any(count > 1 for count in counts.values()),
        }
        self.pending[("admin", uid)] = ("bind_select", state)
        await self.binding_page(uid, state, 0)

    async def binding_page(self, uid, state, page):
        person = self.binding_person(state["target"])
        candidates, token = state["candidates"], state["token"]
        page = min(max(0, page), max(0, (len(candidates) - 1) // PAGE))
        text = f"Сотрудник: {person['name']}\n\nВыберите имя из таблицы:"
        if not candidates:
            text += "\nСвободных однозначных записей нет."
        if state["duplicates"]:
            text += "\nСовпадающие псевдонимы скрыты. Уточните имена в таблице и обновите список."
        rows = [
            [(name, f"bindpick:{token}:{i}")]
            for i, (_, name) in enumerate(candidates[page * PAGE : (page + 1) * PAGE], page * PAGE)
        ]
        rows += self.pages(f"bindpage:{token}", page, len(candidates))[:-1]
        rows += [
            [("Обновить список", f"bindstart:{state['target']}")],
            [("Отмена", f"person:{state['target']}")],
        ]
        await self.send("admin", uid, text, rows)

    async def binding_action(self, uid, p):
        key = ("admin", uid)
        pending = self.pending.get(key, (None,))
        if pending[0] not in {"bind_select", "bind_confirm"}:
            raise UserError("Выбор устарел. Откройте «Связать с таблицей» заново.")
        state = pending[1]
        if state["token"] != p[1] or self.store.clock() - state["issued"] > 300:
            self.pending.pop(key, None)
            raise UserError("Выбор устарел. Обновите список в карточке сотрудника.")
        person = self.binding_person(state["target"])
        if p[0] == "bindpage" and pending[0] == "bind_select":
            await self.binding_page(uid, state, int(p[2]))
        elif p[0] == "bindpick" and pending[0] == "bind_select":
            index = int(p[2])
            if not 0 <= index < len(state["candidates"]):
                raise UserError("Имя не найдено. Обновите список.")
            mid, name = state["candidates"][index]
            state = dict(state, selected=(mid, name))
            self.pending[key] = ("bind_confirm", state)
            await self.send(
                "admin",
                uid,
                f"Проверьте привязку:\n\nСотрудник: {person['name']}\nВ таблице: {name}",
                [
                    [("Подтвердить", f"bindconfirm:{state['token']}")],
                    [("Отмена", f"person:{state['target']}")],
                ],
            )
        elif p[0] == "bindconfirm" and pending[0] == "bind_confirm":
            snapshot = await self.payroll.get(force=True)
            if not self.auth.logged_in(uid):
                raise UserError("Войдите в админку заново через /admin.")
            self.binding_person(state["target"])
            if self.store.clock() - state["issued"] > 300:
                raise UserError("Подтверждение устарело. Обновите список.")
            from .studio_sheet import alias_key

            mid, name = state["selected"]
            names = snapshot.names(active_only=True)
            if (
                names.get(mid) != name
                or sum(alias_key(n) == alias_key(name) for n in names.values()) != 1
            ):
                self.pending.pop(key, None)
                raise UserError(
                    "Запись в таблице изменилась. Обновите список и выберите имя заново."
                )
            self.store.approve_person(state["target"], mid, uid)
            self.pending.pop(key, None)
            await self.send(
                "admin",
                uid,
                "Сотрудник подтверждён и связан с таблицей.",
                [[("Модели", "people:0")]],
            )
        else:
            raise UserError("Кнопка устарела. Откройте карточку заново.")

    async def access_list(self, uid, choice, page):
        self.pending.pop(("admin", uid), None)
        if choice not in {"ban", "unban"}:
            raise UserError("Действие недоступно.")
        records = self.store.all(
            "SELECT user_id,name FROM people WHERE banned=? AND user_id<>? ORDER BY name,user_id",
            (int(choice == "unban"), uid),
        )
        page = min(max(0, page), max(0, (len(records) - 1) // PAGE))
        title = "Кого заблокировать?" if choice == "ban" else "Кого разблокировать?"
        if not records:
            title += "\nПодходящих сотрудников нет."
        rows = [
            [(row["name"], f"accessask:{row['user_id']}:{choice}")]
            for row in records[page * PAGE : (page + 1) * PAGE]
        ]
        await self.send(
            "admin", uid, title, rows + self.pages(f"accesslist:{choice}", page, len(records))
        )

    async def health(self, uid):
        self.pending.pop(("admin", uid), None)
        try:
            self.store.one("SELECT 1")
            database = "доступна"
        except Exception:
            database = "ошибка чтения"
        snapshot = None
        try:
            snapshot = await self.payroll.get(force=True)
            google = "подключена, проверка успешна"
        except UserError as error:
            google = "ошибка: " + str(error)
        except Exception:
            google = "ошибка чтения; проверьте подключение и доступ"
        if not self.auth.logged_in(uid):
            raise UserError("Войдите в админку заново через /admin.")
        success = getattr(self.payroll, "last_success", None)
        months = getattr(self.payroll, "last_months", ())
        if snapshot is not None:
            success = snapshot.fetched_at
            months = tuple(sorted({entry.day.strftime("%Y-%m") for entry in snapshot.entries}))
        last = (
            success.astimezone(self.settings.zone).strftime("%d.%m.%Y %H:%M:%S")
            if success
            else "ещё не было"
        )
        text = (
            f"СОСТОЯНИЕ БОТА\n\nВерсия: {__version__}\nЗапущен: {self.when(self.started_at)}"
            f"\nВремя: Санкт-Петербург\nБаза SQLite: {database}\n\nGoogle-таблица: {google}"
            f"\nПоследнее успешное чтение: {last}\nМесяцы последнего успешного чтения: {', '.join(months) or 'нет данных'}"
        )
        if snapshot is not None and self.now().strftime("%Y-%m") not in months:
            text += "\nДанных за текущий месяц нет."
        if snapshot is None:
            text += "\nАктуальность данных за текущий месяц проверить не удалось."
        await self.send("admin", uid, text, [[("Проверить снова", "health")], [("Меню", "home")]])

    async def list_bookings(self, role, uid, page):
        if role == "model":
            records = self.store.all(
                "SELECT b.*,p.name FROM bookings b JOIN people p USING(user_id) WHERE b.user_id=? AND b.ended>? ORDER BY started",
                (uid, self.store.clock()),
            )
        else:
            records = self.store.all(
                "SELECT b.*,p.name FROM bookings b JOIN people p USING(user_id) WHERE state IN ('pending','conflict') AND started>? ORDER BY started",
                (self.store.clock(),),
            )
        page = min(max(0, page), max(0, (len(records) - 1) // PAGE))
        lines, rows = ["📋 Записи:"], []
        for row in records[page * PAGE : (page + 1) * PAGE]:
            lines.append(
                f"#{row['id']} • {row['name']} • {self.when(row['started'])}–{self.when(row['ended'])} • {LABELS[row['state']]} • {room_name(row['room']) if row['room'] else 'комната не назначена'}"
            )
            if role == "admin":
                rows.append([(f"Рассмотреть #{row['id']}", f"booking:{row['id']}")])
            elif (
                row["state"] in {"pending", "approved", "conflict"}
                and row["started"] > self.store.clock()
            ):
                rows.append([(f"Отменить #{row['id']}", f"cancelbook:{row['id']}")])
        if not records:
            lines.append("Нет актуальных заявок.")
        await self.send(
            role,
            uid,
            "\n".join(lines),
            rows + self.pages("mybookings" if role == "model" else "bookings", page, len(records)),
        )

    async def usage(self, uid, period):
        start = datetime.combine(period[0], time.min, self.settings.zone).timestamp()
        end = datetime.combine(
            period[1] + timedelta(days=1), time.min, self.settings.zone
        ).timestamp()
        totals = self.store.room_usage(start, end)
        lines = [f"Использование комнат • {period[0]} — {period[1]}"]
        lines += [f"{room}: {duration(totals.get(room, 0))}" for room in self.settings.rooms]
        recent = self.store.all(
            "SELECT s.*,p.name FROM shifts s JOIN people p USING(user_id) WHERE started<? AND (ended IS NULL OR ended>?) ORDER BY started DESC LIMIT 20",
            (end, start),
        )
        lines += ["", "Последние 20 смен периода:"]
        for row in recent:
            lines.append(
                f"{row['name']} • {row['room']} • {self.when(row['started'])} → {self.when(row['ended']) if row['ended'] is not None else 'идёт'}"
            )
        await self.send(
            "admin", uid, "\n".join(lines), [[("Другой период", "usageperiod"), ("Меню", "home")]]
        )

    async def admin_action(self, uid, p):
        action, key = p[0], ("admin", uid)
        if action == "logout":
            self.auth.logout(uid)
            self.pending.pop(key, None)
            await self.send("model", uid, "Вы вышли из админки. /admin — войти снова.")
            await self.home("model", uid)
        elif action == "askend":
            await self.send(
                "admin",
                uid,
                "Завершить эту смену и освободить комнату?",
                [[("Да, завершить", f"adminend:{int(p[1])}"), ("Назад", "rooms")]],
            )
        elif action == "adminend":
            self.store.end_shift(int(p[1]), uid, admin=True)
            await self.rooms("admin", uid)
        elif action == "usage":
            await self.usage(uid, (self.now().date(), self.now().date()))
        elif action == "usageperiod":
            self.pending[key] = ("usage_period",)
            await self.send("admin", uid, "Введите начало и конец периода: 2026-10-01 2026-10-15.")
        elif action == "people":
            records = self.store.all(
                "SELECT * FROM people ORDER BY status='pending' DESC,created DESC"
            )
            page = min(max(0, int(p[1])), max(0, (len(records) - 1) // PAGE))
            rows = [
                [
                    (
                        f"{r['name']} • {'заблокирована' if r['banned'] else LABELS[r['status']]}",
                        f"person:{r['user_id']}",
                    )
                ]
                for r in records[page * PAGE : (page + 1) * PAGE]
            ]
            await self.send(
                "admin",
                uid,
                "Анкеты и модели:" if records else "Анкет пока нет.",
                rows + self.pages("people", page, len(records)),
            )
        elif action == "person":
            self.pending.pop(key, None)
            row = self.store.user(int(p[1]))
            if not row:
                raise UserError("Анкета не найдена.")
            rows = (
                [
                    [
                        (
                            "Одобрить анкету без зарплатной привязки",
                            f"approveprofile:{row['user_id']}",
                        )
                    ],
                    [("Связать с таблицей", f"bindstart:{row['user_id']}")],
                ]
                + (
                    [[("Отклонить", f"rejectperson:{row['user_id']}")]]
                    if row["status"] == "pending"
                    else []
                )
                if row["status"] in {"pending", "rejected"}
                else [[("Связать с таблицей", f"bindstart:{row['user_id']}")]]
                if row["status"] == "approved" and not row["payroll_id"]
                else []
            )
            if row["banned"]:
                rows = [[("Разблокировать", f"accessask:{row['user_id']}:unban")]]
            elif row["user_id"] != uid:
                rows += [[("Заблокировать", f"accessask:{row['user_id']}:ban")]]
            await self.send(
                "admin",
                uid,
                f"МОДЕЛЬ · {row['name']}\n\nДоступ: {'заблокирован' if row['banned'] else 'открыт'}\nАнкета: {LABELS[row['status']]}\n\nTelegram ID: {row['user_id']}\nUsername: {row['username'] or 'нет'}\nТаблица: {'привязана' if row['payroll_id'] else 'не привязана'}",
                rows + [[("Назад", "people:0")]],
            )
        elif action == "accessask":
            target, choice = int(p[1]), p[2]
            row = self.store.user(target)
            if not row or choice not in {"ban", "unban"} or target == uid:
                raise UserError("Действие недоступно.")
            if bool(row["banned"]) != (choice == "unban"):
                raise UserError("Статус уже изменён. Обновите карточку.")
            token = secrets.token_hex(4)
            self.pending[key] = ("access_confirm", target, choice, token, self.store.clock())
            detail = (
                "Доступ к боту закроется. Текущая смена завершится, оставшиеся записи отменятся. "
                "История и заявки на аванс сохранятся."
                if choice == "ban"
                else "Доступ вернётся с прежним статусом анкеты и привязкой к таблице. "
                "Завершённые смены и отменённые записи не восстановятся."
            )
            await self.send(
                "admin",
                uid,
                f"{row['name']}\n\n{detail}",
                [
                    [("Подтвердить", f"accessconfirm:{token}")],
                    [("Отмена", f"person:{target}")],
                ],
            )
        elif action == "accessconfirm":
            state = self.pending.get(key, (None,))
            if (
                state[0] != "access_confirm"
                or state[3] != p[1]
                or self.store.clock() - state[4] > 300
            ):
                raise UserError("Подтверждение устарело. Откройте карточку заново.")
            target, banned = state[1], state[2] == "ban"
            self.store.set_banned(target, banned, uid)
            self.pending.pop(key, None)
            self.pending.pop(("model", target), None)
            self.pending.pop(("admin", target), None)
            self.auth.logout(target)
            await self.admin_action(uid, ["person", str(target)])
        elif action == "approveprofile":
            self.store.approve_person(int(p[1]), None, uid)
            await self.send(
                "admin",
                uid,
                "Анкета подтверждена. Зарплатную привязку можно добавить в карточке позже.",
                [[("Модели", "people:0")]],
            )
        elif action == "bindstart":
            await self.start_binding(uid, int(p[1]))
        elif action in {"bindpage", "bindpick", "bindconfirm"}:
            await self.binding_action(uid, p)
        elif action == "accesslist":
            await self.access_list(uid, p[1], int(p[2]))
        elif action == "health":
            await self.health(uid)
        elif action == "rejectperson":
            self.store.reject_person(int(p[1]), uid)
            await self.send("admin", uid, "Анкета отклонена.", [[("Модели", "people:0")]])
        elif action == "bookings":
            self.pending.pop(key, None)
            await self.list_bookings("admin", uid, int(p[1]))
        elif action == "booking":
            row = self.store.one(
                "SELECT b.*,p.name FROM bookings b JOIN people p USING(user_id) WHERE b.id=?",
                (int(p[1]),),
            )
            if (
                not row
                or row["state"] not in {"pending", "conflict"}
                or row["started"] <= self.store.clock()
            ):
                raise UserError("Заявка уже обработана или посещение началось.")
            conflicts = (
                self.store.room_conflicts(row["room"], row["started"], row["ended"])
                if row["room"]
                else []
            )
            detail = "\n".join(
                f"• {c['name']} · {self.when(c['started'])} — {self.when(c['ended'])}"
                for c in conflicts
            )
            await self.send(
                "admin",
                uid,
                f"📅 {row['name']}\n{self.when(row['started'])} — {self.when(row['ended'])}\nЗапрошена: {room_name(row['room']) if row['room'] else 'не выбрана'}\n"
                + (f"\nПересечения в этой комнате:\n{detail}\n" if detail else "")
                + "\nВыберите свободную комнату для подтверждения или измените время:",
                [
                    [(room_name(room), f"approvebook:{row['id']}:{i}")]
                    for i, room in enumerate(self.settings.rooms)
                ]
                + [
                    [("Изменить время", f"booktime:{row['id']}")],
                    [("Отклонить", f"rejectbook:{row['id']}"), ("Назад", "bookings:0")],
                ],
            )
        elif action == "booktime":
            row = self.store.one("SELECT * FROM bookings WHERE id=?", (int(p[1]),))
            if (
                not row
                or row["state"] not in {"pending", "conflict"}
                or row["started"] <= self.store.clock()
            ):
                raise UserError("Заявка уже обработана или посещение началось.")
            self.pending[key] = ("booking_time", row["id"])
            await self.send(
                "admin",
                uid,
                f"Новое время на {row['day']}: например 14:00–19:30. Затем выберите комнату для подтверждения. /cancel — отмена.",
            )
        elif action == "bookedit":
            state = self.pending.get(key, (None,))
            if (
                state[0] != "booking_edit"
                or state[1]["token"] != p[1]
                or self.store.clock() - state[1]["issued"] > 300
            ):
                raise UserError("Выбор устарел. Откройте заявку заново.")
            data = state[1]
            index = int(p[2])
            if not 0 <= index < len(self.settings.rooms):
                raise UserError("Комната не найдена.")
            self.store.decide_booking(
                data["bid"],
                uid,
                room=self.settings.rooms[index],
                times=(data["start"], data["end"]),
            )
            self.pending.pop(key, None)
            await self.list_bookings("admin", uid, 0)
        elif action in {"approvebook", "rejectbook"}:
            if action == "approvebook" and not 0 <= int(p[2]) < len(self.settings.rooms):
                raise UserError("Комната не найдена.")
            room = self.settings.rooms[int(p[2])] if action == "approvebook" else None
            self.store.decide_booking(int(p[1]), uid, room=room, reject=action == "rejectbook")
            await self.list_bookings("admin", uid, 0)
        elif action == "calendar":
            rows = self.store.all(
                "SELECT b.*,p.name FROM bookings b JOIN people p USING(user_id) WHERE state='approved' AND ended>? ORDER BY started,room",
                (self.store.clock(),),
            )
            await self.send(
                "admin",
                uid,
                "Подтверждённое расписание:\n\n"
                + (
                    "\n".join(
                        f"{self.when(r['started'])}–{self.when(r['ended'])} • {room_name(r['room'])} • {r['name']}"
                        for r in rows
                    )
                    or "Броней пока нет."
                ),
                [[("Обновить", "calendar"), ("Меню", "home")]],
            )
        elif action in {"advances", "tickets"}:
            table, cond, prefix = (
                ("advances", "a.state IN ('pending','approved')", "advanceitem")
                if action == "advances"
                else ("tickets", "a.answer IS NULL", "ticketitem")
            )
            records = self.store.all(
                f"SELECT a.*,p.name FROM {table} a JOIN people p USING(user_id) WHERE {cond} ORDER BY a.id"
            )
            page = min(max(0, int(p[1])), max(0, (len(records) - 1) // PAGE))
            rows = [
                [(f"#{r['id']} • {r['name']}", f"{prefix}:{r['id']}")]
                for r in records[page * PAGE : (page + 1) * PAGE]
            ]
            await self.send(
                "admin",
                uid,
                "Открытые заявки:" if records else "Открытых заявок нет.",
                rows + self.pages(action, page, len(records)),
            )
        elif action == "advanceitem":
            row = self.store.one(
                "SELECT a.*,p.name FROM advances a JOIN people p USING(user_id) WHERE a.id=?",
                (int(p[1]),),
            )
            if not row:
                raise UserError("Заявка не найдена.")
            rows = (
                [
                    [
                        ("Одобрить", f"decideadvance:{row['id']}:approve"),
                        ("Отклонить", f"decideadvance:{row['id']}:reject"),
                    ]
                ]
                if row["state"] == "pending"
                else [[("Отметить выплату", f"askpaid:{row['id']}")]]
                if row["state"] == "approved"
                else []
            )
            await self.send(
                "admin",
                uid,
                f"Аванс #{row['id']} • {row['name']}\nЗапрошено: {row['amount']} {row['currency']}\nСтатус: {LABELS[row['state']]}\n"
                + (
                    f"Вычет из выплаты: {date.fromisoformat(row['deduction_payday']):%d.%m.%Y}\n"
                    if row["deduction_payday"]
                    else f"При одобрении вычтем из ближайшей выплаты: {payment_dates(self.now().date())[0]:%d.%m.%Y}\n"
                    if row["state"] == "pending"
                    else ""
                )
                + f"\nСнимок на момент заявки:\n{self.snapshot_text(json.loads(row['snapshot']))}",
                rows + [[("Назад", "advances:0")]],
            )
        elif action == "askpaid":
            await self.send(
                "admin",
                uid,
                "Подтвердите, что аванс уже выплачен вне бота. Это отметка в журнале, а не перевод денег или изменение Google-таблицы.",
                [
                    [
                        ("Да, уже выплачен", f"decideadvance:{int(p[1])}:paid"),
                        ("Назад", "advances:0"),
                    ]
                ],
            )
        elif action == "decideadvance":
            self.store.decide_advance(int(p[1]), p[2], uid, zone=self.settings.zone)
            await self.send(
                "admin",
                uid,
                "Статус сохранён. Модель получит уведомление."
                + (
                    f" Аванс вычтен из выплаты {payment_dates(self.now().date())[0]:%d.%m.%Y}."
                    if p[2] == "approve"
                    else ""
                ),
                [[("Авансы", "advances:0")]],
            )
        elif action == "ticketitem":
            row = self.store.one(
                "SELECT t.*,p.name FROM tickets t JOIN people p USING(user_id) WHERE t.id=?",
                (int(p[1]),),
            )
            if not row:
                raise UserError("Обращение не найдено.")
            await self.send(
                "admin",
                uid,
                f"Обращение #{row['id']} • {row['name']}\n\n{row['body']}\n\nОтвет: {row['answer'] or 'пока нет'}",
                [[("Ответить", f"reply:{row['id']}")], [("Назад", "tickets:0")]],
            )
        elif action == "reply":
            self.pending[key] = ("answer", int(p[1]))
            await self.send(
                "admin", uid, "Введите ответ модели одним сообщением (до 2000 символов)."
            )
        elif action == "delivery":
            rows = self.store.all("SELECT state,count(*) n FROM outbox GROUP BY state")
            counts = {r["state"]: r["n"] for r in rows}
            await self.send(
                "admin",
                uid,
                f"Уведомления: ожидают {counts.get('pending', 0)}, доставлены {counts.get('sent', 0)}, ошибки {counts.get('failed', 0)}, отменены {counts.get('cancelled', 0)}.\nЕсли получатель заблокировал бота, доставка невозможна. Сами заявки остаются в базе.",
                [[("Обновить", "delivery"), ("Меню", "home")]],
            )
        else:
            raise UserError("Команда не найдена. Откройте /menu.")
