import asyncio
import time

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter


async def deliver_once(store, bots):
    rows = store.all(
        "SELECT * FROM outbox WHERE state='pending' AND retry_at<=? ORDER BY id LIMIT 20",
        (store.clock(),),
    )
    for row in rows:
        if store.is_banned(row["user_id"]):
            with store.db:
                store.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
            continue
        if store.one("SELECT state FROM outbox WHERE id=?", (row["id"],))["state"] != "pending":
            continue
        with store.db:
            store.db.execute(
                "UPDATE outbox SET state='inflight',attempts=attempts+1 WHERE id=?", (row["id"],)
            )
        try:
            await bots[row["role"]].send_message(row["user_id"], row["text"], parse_mode=None)
            with store.db:
                store.db.execute("UPDATE outbox SET state='sent' WHERE id=?", (row["id"],))
        except TelegramRetryAfter as error:
            with store.db:
                store.db.execute(
                    "UPDATE outbox SET state='pending',retry_at=? WHERE id=?",
                    (store.clock() + error.retry_after + 1, row["id"]),
                )
        except (TelegramForbiddenError, TelegramBadRequest):
            with store.db:
                store.db.execute("UPDATE outbox SET state='failed' WHERE id=?", (row["id"],))
        except Exception:
            with store.db:
                store.db.execute(
                    "UPDATE outbox SET state=?,retry_at=? WHERE id=?",
                    (
                        "failed" if row["attempts"] >= 9 else "pending",
                        store.clock() + min(3600, 5 * 2 ** min(row["attempts"], 10)),
                        row["id"],
                    ),
                )


async def delivery_loop(store, bots):
    with store.db:
        # Notifications are at-least-once; financial operations never run in this worker.
        store.db.execute("UPDATE outbox SET state='pending' WHERE state='inflight'")
    while True:
        await deliver_once(store, bots)
        await asyncio.sleep(2)


async def payroll_loop(payroll):
    last_status = None
    while True:
        try:
            snapshot = await payroll.get(force=True)
            months = tuple(sorted({e.day.strftime("%Y-%m") for e in snapshot.entries}))
            latest = months[-1] if months else ""
            current = [e for e in snapshot.entries if e.day.strftime("%Y-%m") == latest]
            models = len({e.model_id for e in current})
            missing = len({e.model_id for e in current if not e.share_available and not e.inactive})
            unavailable = len(snapshot.issues)
            status = ("read", months, models, missing, unavailable)
            message = (
                "Google Sheets: чтение "
                + ("частичное" if unavailable else "успешно")
                + f"; месяцы: {', '.join(months) or 'нет данных'}"
                + f"; моделей в последнем прочитанном месяце: {models}"
                + f"; без процента: {missing}; недоступных источников: {unavailable}."
            )
        except Exception as error:
            # Exception text may contain credentials or HTTP URLs. Types only.
            status = ("error", type(error).__name__)
            message = f"Google Sheets: чтение не удалось ({type(error).__name__}). Проверьте «Состояние бота»."
        if status != last_status:
            print(message, flush=True)
            last_status = status
        await asyncio.sleep(60)


async def heartbeat(data):
    while True:
        (data / "heartbeat").write_text(str(time.time()), encoding="ascii")
        await asyncio.sleep(30)
