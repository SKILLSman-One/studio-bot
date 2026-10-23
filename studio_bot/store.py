from __future__ import annotations

import json
import secrets
import sqlite3
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import UserError
from .dates import payment_dates, require_booking_window
from .presentation import room_name

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS people(
 user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL, note TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', payroll_id TEXT UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS shifts(
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES people, room TEXT NOT NULL,
 started REAL NOT NULL, ended REAL, ended_by INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS occupied_room ON shifts(room) WHERE ended IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS one_shift ON shifts(user_id) WHERE ended IS NULL;
CREATE TABLE IF NOT EXISTS bookings(
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES people, day TEXT NOT NULL,
 slot INTEGER NOT NULL, started REAL NOT NULL, ended REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', room TEXT, decided_by INTEGER);
DROP INDEX IF EXISTS one_booking;
CREATE UNIQUE INDEX IF NOT EXISTS one_booking_time ON bookings(user_id,started,ended) WHERE state IN ('pending','approved','conflict');
CREATE TABLE IF NOT EXISTS advances(
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES people,
 amount TEXT NOT NULL, currency TEXT NOT NULL, snapshot TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, decided_by INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_advance ON advances(user_id) WHERE state IN ('pending','approved');
CREATE TABLE IF NOT EXISTS tickets(
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES people, body TEXT NOT NULL,
 created REAL NOT NULL, answer TEXT, answered_by INTEGER);
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY, role TEXT NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_attempts(user_id INTEGER PRIMARY KEY, failures INTEGER NOT NULL,
 last_attempt REAL NOT NULL, blocked_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS admin_recipients(user_id INTEGER PRIMARY KEY, password_tag TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor INTEGER, action TEXT NOT NULL,
 target INTEGER, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS announcements(
 id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, actor INTEGER NOT NULL,
 text TEXT NOT NULL, created REAL NOT NULL);
"""


class Store:
    def __init__(self, path: Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Additive migration keeps existing profiles, bindings and financial history.
        if "banned" not in {row["name"] for row in self.db.execute("PRAGMA table_info(people)")}:
            with self.db:
                self.db.execute("ALTER TABLE people ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        self.clock = clock
        with self.db:
            if "clean_confirmed_at" not in {
                r["name"] for r in self.db.execute("PRAGMA table_info(shifts)")
            }:
                self.db.execute("ALTER TABLE shifts ADD COLUMN clean_confirmed_at REAL")
            advance_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(advances)")}
            if "deduction_payday" not in advance_columns:
                self.db.execute("ALTER TABLE advances ADD COLUMN deduction_payday TEXT")
            if "approved_at" not in advance_columns:
                self.db.execute("ALTER TABLE advances ADD COLUMN approved_at REAL")
            self.db.execute(
                "INSERT OR IGNORE INTO runtime_state VALUES ('callback_epoch',?)",
                (secrets.token_hex(4),),
            )
            if "announcement_id" not in {
                r["name"] for r in self.db.execute("PRAGMA table_info(outbox)")
            }:
                self.db.execute(
                    "ALTER TABLE outbox ADD COLUMN announcement_id INTEGER REFERENCES announcements"
                )
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS announcement_recipient "
                "ON outbox(announcement_id,user_id) WHERE announcement_id IS NOT NULL"
            )
            # Existing blocked profiles must also release their payroll aliases.
            self.db.execute("UPDATE people SET payroll_id=NULL WHERE banned=1 AND payroll_id IS NOT NULL")

    @property
    def callback_epoch(self):
        return self.one("SELECT value FROM runtime_state WHERE key='callback_epoch'")["value"]

    def clear_test_history(self):
        """Maintenance only: call after taking a verified backup with the bot stopped."""
        with self.db:
            for table in (
                "outbox",
                "announcements",
                "audit",
                "tickets",
                "advances",
                "bookings",
                "shifts",
                "people",
                "admin_recipients",
                "login_attempts",
            ):
                self.db.execute(f"DELETE FROM {table}")
            self.db.execute(
                "UPDATE runtime_state SET value=? WHERE key='callback_epoch'",
                (secrets.token_hex(4),),
            )

    def one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def close(self):
        self.db.close()

    def user(self, uid):
        return self.one("SELECT * FROM people WHERE user_id=?", (uid,))

    def approved(self, uid):
        self.require_access(uid)
        row = self.user(uid)
        if not row or row["status"] != "approved":
            raise UserError("Дождитесь подтверждения анкеты администратором.")
        return row

    def is_banned(self, uid):
        row = self.user(uid)
        return bool(row and row["banned"])

    def require_access(self, uid):
        if self.is_banned(uid):
            raise UserError("Доступ к боту заблокирован администратором студии.")

    def set_banned(self, uid, banned, actor):
        if uid == actor:
            raise UserError("Нельзя заблокировать собственный аккаунт.")
        with self.db:
            row = self.user(uid)
            if not row or bool(row["banned"]) == banned:
                raise UserError("Статус уже изменён. Обновите карточку модели.")
            self.db.execute("UPDATE people SET banned=? WHERE user_id=?", (int(banned), uid))
            if banned:
                self.db.execute("UPDATE people SET payroll_id=NULL WHERE user_id=?", (uid,))
                self.db.execute(
                    "UPDATE shifts SET ended=max(started,?),ended_by=? WHERE user_id=? AND ended IS NULL",
                    (self.clock(), actor, uid),
                )
                self.db.execute(
                    "UPDATE bookings SET state='cancelled',decided_by=? "
                    "WHERE user_id=? AND state IN ('pending','approved','conflict') AND ended>?",
                    (actor, uid, self.clock()),
                )
                self.db.execute("DELETE FROM admin_recipients WHERE user_id=?", (uid,))
                self.db.execute(
                    "UPDATE outbox SET state='cancelled' WHERE user_id=? AND state='pending'",
                    (uid,),
                )
            self.audit(actor, "ban_person" if banned else "unban_person", uid)

    def audit(self, actor, action, target):
        self.db.execute(
            "INSERT INTO audit(actor,action,target,created) VALUES (?,?,?,?)",
            (actor, action, target, self.clock()),
        )

    def enqueue(self, role, uid, text):
        self.db.execute("INSERT INTO outbox(role,user_id,text) VALUES (?,?,?)", (role, uid, text))

    def announcement_recipients(self, actor):
        return self.all(
            "SELECT user_id,name FROM people "
            "WHERE status='approved' AND banned=0 AND user_id<>? ORDER BY name,user_id",
            (actor,),
        )

    def announce(self, actor, token, text, recipients):
        text = text.strip()
        if not text or len(text.encode("utf-16-le")) // 2 > 3500:
            raise UserError("Объявление: от 1 до 3500 символов, одним текстом.")
        with self.db:
            existing = self.one("SELECT * FROM announcements WHERE token=?", (token,))
            if existing:
                if existing["actor"] != actor or existing["text"] != text:
                    raise UserError("Подтверждение объявления устарело.")
                return existing["id"]
            selected = set(recipients)
            eligible = [
                r["user_id"] for r in self.announcement_recipients(actor)
                if r["user_id"] in selected
            ]
            if not eligible:
                raise UserError("Получателей нет: нужны одобренные сотрудники без блокировки.")
            aid = self.db.execute(
                "INSERT INTO announcements(token,actor,text,created) VALUES (?,?,?,?)",
                (token, actor, text, self.clock()),
            ).lastrowid
            self.db.executemany(
                "INSERT INTO outbox(role,user_id,text,announcement_id) VALUES ('model',?,?,?)",
                [(uid, text, aid) for uid in eligible],
            )
            self.audit(actor, "announcement", aid)
        return aid

    def alert_admins(self, ids, text):
        with self.db:
            for uid in ids:
                self.enqueue("admin", uid, text)

    def register(self, uid, name, username, note):
        self.require_access(uid)
        if not 2 <= len(name) <= 60 or len(note) > 500 or any(ord(c) < 32 for c in name):
            raise UserError("Имя: 2–60 символов без переносов, комментарий: до 500.")
        with self.db:
            existing = self.user(uid)
            if existing is not None:
                changed = self.db.execute(
                    "UPDATE people SET name=?,username=?,note=?,status='pending',created=? "
                    "WHERE user_id=? AND status='rejected'",
                    (name, username, note, self.clock(), uid),
                ).rowcount
                if not changed:
                    raise UserError("Анкета уже ожидает проверки или подтверждена. Обновите меню.")
                self.audit(uid, "resubmit_profile", uid)
            else:
                self.db.execute(
                    "INSERT INTO people(user_id,name,username,note,created) VALUES (?,?,?,?,?)",
                    (uid, name, username, note, self.clock()),
                )

    def approve_person(self, uid, payroll_id, actor):
        self.require_access(uid)
        if payroll_id is not None and (
            not payroll_id or len(payroll_id) > 64 or any(c.isspace() for c in payroll_id)
        ):
            raise UserError("ID модели из таблицы: до 64 символов без пробелов.")
        try:
            with self.db:
                changed = self.db.execute(
                    "UPDATE people SET status='approved',payroll_id=? WHERE user_id=? "
                    "AND (status IN ('pending','rejected') OR (status='approved' AND payroll_id IS NULL AND ? IS NOT NULL))",
                    (payroll_id, uid, payroll_id),
                ).rowcount
                if not changed:
                    raise UserError("Анкета уже обработана.")
                self.audit(actor, "approve_person", uid)
                self.enqueue(
                    "model",
                    uid,
                    "Анкета подтверждена. Откройте /start — доступны комнаты и расписание. Зарплата доступна после привязки к таблице.",
                )
        except sqlite3.IntegrityError:
            raise UserError("Эта запись таблицы уже связана с другим сотрудником.") from None

    def reject_person(self, uid, actor):
        self.require_access(uid)
        with self.db:
            changed = self.db.execute(
                "UPDATE people SET status='rejected' WHERE user_id=? AND status='pending'", (uid,)
            ).rowcount
            if not changed:
                raise UserError("Анкета уже обработана.")
            self.audit(actor, "reject_person", uid)
            self.enqueue(
                "model",
                uid,
                "Анкета отклонена. Откройте /start: можно подать анкету заново или связаться с администратором.",
            )

    def active_shifts(self):
        return self.all(
            "SELECT s.*,p.name FROM shifts s JOIN people p USING(user_id) WHERE ended IS NULL ORDER BY room"
        )

    def start_shift(self, uid, room, rooms):
        self.approved(uid)
        if room not in rooms:
            raise UserError("Комната не найдена. Обновите меню.")
        now = self.clock()
        try:
            with self.db:
                booked = self.one(
                    "SELECT 1 FROM bookings WHERE state='approved' AND room=? AND user_id<>? AND started<=? AND ended>?",
                    (room, uid, now, now),
                )
                if booked:
                    raise UserError("Сейчас комната забронирована для другой модели.")
                result = self.db.execute(
                    "INSERT INTO shifts(user_id,room,started) VALUES (?,?,?)", (uid, room, now)
                )
                self.audit(uid, "start_shift", result.lastrowid)
                self.enqueue(
                    "model",
                    uid,
                    f"🌸 Твоя смена началась!\n🚪 {room_name(room)}\nХорошей смены, UwU ♡",
                )
                return result.lastrowid
        except sqlite3.IntegrityError:
            raise UserError("Комната уже занята или у вас есть незавершённая смена.") from None

    def end_shift(self, sid, actor, admin=False, clean_confirmed=False):
        with self.db:
            shift = self.one("SELECT * FROM shifts WHERE id=?", (sid,))
            if not shift or shift["ended"] is not None or (not admin and shift["user_id"] != actor):
                raise UserError("Смена не найдена или уже завершена.")
            ended = max(self.clock(), shift["started"])
            self.db.execute("UPDATE shifts SET ended=?,ended_by=? WHERE id=?", (ended, actor, sid))
            if clean_confirmed and not admin:
                self.db.execute("UPDATE shifts SET clean_confirmed_at=? WHERE id=?", (ended, sid))
                self.audit(actor, "confirm_room_clean", sid)
            self.audit(actor, "end_shift", sid)
            if admin:
                self.enqueue("model", shift["user_id"], "Администратор завершил вашу смену.")
            return ended - shift["started"]

    def room_usage(self, start, end):
        rows = self.all(
            "SELECT room,started,ended FROM shifts WHERE started<? AND (ended IS NULL OR ended>?)",
            (end, start),
        )
        result = {}
        for row in rows:
            result[row["room"]] = result.get(row["room"], 0) + max(
                0,
                min(row["ended"] if row["ended"] is not None else self.clock(), end)
                - max(row["started"], start),
            )
        return result

    def book(self, uid, day, slot, started, ended):
        self.approved(uid)
        if started <= self.clock() or ended <= started:
            raise UserError("Этот слот уже начался.")
        try:
            with self.db:
                if self.one(
                    "SELECT 1 FROM bookings WHERE user_id=? AND state IN ('pending','approved','conflict') AND started<? AND ended>?",
                    (uid, ended, started),
                ):
                    raise UserError("У вас уже есть заявка на пересекающийся слот.")
                return self.db.execute(
                    "INSERT INTO bookings(user_id,day,slot,started,ended) VALUES (?,?,?,?,?)",
                    (uid, day, slot, started, ended),
                ).lastrowid
        except sqlite3.IntegrityError:
            raise UserError("Слот уже выбран.") from None

    def room_conflicts(self, room, started, ended):
        return self.all(
            "SELECT b.*,p.name FROM bookings b JOIN people p USING(user_id) "
            "WHERE state='approved' AND room=? AND started<? AND ended>? ORDER BY started",
            (room, ended, started),
        )

    def book_room(self, uid, day, room, started, ended, settings, admin_ids=()):
        # Lock before checking availability so concurrent confirmations cannot double book.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            person = self.approved(uid)
            now = datetime.fromtimestamp(self.clock(), settings.zone)
            require_booking_window(now, date.fromisoformat(day))
            if (
                room not in settings.rooms
                or started <= self.clock()
                or ended <= started
                or ended - started > 24 * 3600
                or datetime.fromtimestamp(started, settings.zone).date().isoformat() != day
            ):
                raise UserError("Проверьте комнату и время посещения.")
            if self.one(
                "SELECT 1 FROM bookings WHERE user_id=? AND state IN ('pending','approved','conflict') AND started<? AND ended>?",
                (uid, ended, started),
            ):
                raise UserError(
                    "У вас уже есть запись или заявка на это время. Проверьте «Мои записи»."
                )
            state = "conflict" if self.room_conflicts(room, started, ended) else "approved"
            bid = self.db.execute(
                "INSERT INTO bookings(user_id,day,slot,started,ended,state,room) VALUES (?,?,?,?,?,?,?)",
                (uid, day, int(started), started, ended, state, room),
            ).lastrowid
            self.audit(uid, "booking_" + state, bid)
            if state == "conflict":
                a = datetime.fromtimestamp(started, settings.zone)
                b = datetime.fromtimestamp(ended, settings.zone)
                for admin in admin_ids:
                    self.enqueue(
                        "admin",
                        admin,
                        f"⚠️ Пересечение записей: {person['name']} · {room_name(room)}\n"
                        f"{a:%d.%m %H:%M} — {b:%d.%m %H:%M}\n"
                        "Комната не забронирована. Откройте /admin → Записи на разбор.",
                    )
            return bid, state

    def decide_booking(self, bid, actor, room=None, reject=False, owner=False, times=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.one("SELECT * FROM bookings WHERE id=?", (bid,))
            if (
                not row
                or row["state"] not in {"pending", "approved", "conflict"}
                or row["started"] <= self.clock()
            ):
                raise UserError("Заявка уже обработана или посещение началось.")
            if owner and row["user_id"] != actor:
                raise UserError("Это не ваша заявка.")
            if owner and (not reject or times is not None):
                raise UserError("Это действие доступно только администратору.")
            started, ended = times if times is not None else (row["started"], row["ended"])
            if reject:
                state = "cancelled" if owner else "rejected"
            else:
                self.approved(row["user_id"])
                if row["state"] not in {"pending", "conflict"} or not room:
                    raise UserError("Заявка уже подтверждена.")
                if started <= self.clock() or ended <= started or ended - started > 24 * 3600:
                    raise UserError("Проверьте время посещения.")
                if self.one(
                    "SELECT 1 FROM bookings WHERE id<>? AND user_id=? AND state IN ('pending','approved','conflict') AND started<? AND ended>?",
                    (bid, row["user_id"], ended, started),
                ):
                    raise UserError("У сотрудника уже есть запись на это время.")
                if self.room_conflicts(room, started, ended):
                    raise UserError("В этой комнате уже есть пересекающаяся бронь.")
                state = "approved"
            self.db.execute(
                "UPDATE bookings SET state=?,room=?,decided_by=?,started=?,ended=? WHERE id=?",
                (state, room or row["room"], actor, started, ended, bid),
            )
            self.audit(actor, "booking_" + state, bid)
            if not owner:
                self.enqueue(
                    "model",
                    row["user_id"],
                    f"📅 Запись {row['day']}: {'подтверждена, ' + room_name(room) if state == 'approved' else 'отклонена'}. Актуальное время — в «Мои записи».",
                )

    def advance(self, uid, amount, currency, snapshot):
        self.approved(uid)
        if currency != "USD":
            raise UserError("Новые заявки принимаются только в USD.")
        try:
            with self.db:
                return self.db.execute(
                    "INSERT INTO advances(user_id,amount,currency,snapshot,created) VALUES (?,?,?,?,?)",
                    (
                        uid,
                        str(amount),
                        currency,
                        json.dumps(snapshot, ensure_ascii=False),
                        self.clock(),
                    ),
                ).lastrowid
        except sqlite3.IntegrityError:
            raise UserError(
                "У вас уже есть открытая заявка на аванс. Дождитесь её обработки или выплаты."
            ) from None

    def advance_deduction(self, uid, payday):
        return sum(
            (
                Decimal(r["amount"])
                for r in self.all(
                    "SELECT amount FROM advances WHERE user_id=? AND deduction_payday=? AND state IN ('approved','paid') AND currency='USD'",
                    (uid, str(payday)),
                )
            ),
            Decimal(0),
        )

    def decide_advance(self, aid, action, actor, zone=None):
        before, after = {
            "approve": ("pending", "approved"),
            "reject": ("pending", "rejected"),
            "paid": ("approved", "paid"),
        }[action]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.one("SELECT * FROM advances WHERE id=?", (aid,))
            if not row or row["state"] != before:
                raise UserError("Заявка уже обработана. Обновите список.")
            self.db.execute(
                "UPDATE advances SET state=?,decided_by=? WHERE id=?", (after, actor, aid)
            )
            payday = None
            if action == "approve":
                if row["currency"] != "USD":
                    raise UserError(
                        "Эта старая заявка не в USD. Её нельзя вычесть из долларовой зарплаты."
                    )
                payday = payment_dates(
                    datetime.fromtimestamp(self.clock(), zone or ZoneInfo("Europe/Moscow")).date()
                )[0]
                self.db.execute(
                    "UPDATE advances SET deduction_payday=?,approved_at=? WHERE id=?",
                    (str(payday), self.clock(), aid),
                )
            self.audit(actor, "advance_" + after, aid)
            wording = {
                "approved": "одобрена; выплата выполняется администратором отдельно",
                "rejected": "отклонена",
                "paid": "отмечена администратором как выплаченная",
            }[after]
            detail = (
                f"\n💵 ${Decimal(row['amount']):.2f} учтены в вычете из выплаты {payday:%d.%m.%Y}."
                if payday
                else ""
            )
            self.enqueue("model", row["user_id"], f"Заявка на аванс №{aid} {wording}." + detail)

    def ticket(self, uid, body):
        self.require_access(uid)
        if not self.user(uid):
            raise UserError("Сначала заполните анкету.")
        if not 1 <= len(body) <= 2000:
            raise UserError("Сообщение: 1–2000 символов.")
        with self.db:
            if (
                self.one(
                    "SELECT count(*) n FROM tickets WHERE user_id=? AND answer IS NULL", (uid,)
                )["n"]
                >= 3
            ):
                raise UserError("У вас уже 3 открытых обращения. Дождитесь ответа.")
            return self.db.execute(
                "INSERT INTO tickets(user_id,body,created) VALUES (?,?,?)",
                (uid, body, self.clock()),
            ).lastrowid

    def answer(self, tid, actor, text):
        if not 1 <= len(text) <= 2000:
            raise UserError("Ответ: 1–2000 символов.")
        with self.db:
            row = self.one("SELECT * FROM tickets WHERE id=? AND answer IS NULL", (tid,))
            if not row:
                raise UserError("Обращение уже обработано.")
            self.db.execute(
                "UPDATE tickets SET answer=?,answered_by=? WHERE id=?", (text, actor, tid)
            )
            self.audit(actor, "answer_ticket", tid)
            self.enqueue(
                "model", row["user_id"], f"Ответ администратора на обращение №{tid}:\n\n{text}"
            )
