import argparse
import asyncio
import getpass
import logging
import os
import re
import sys
import time

import requests
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from dotenv import dotenv_values, set_key

from . import __version__
from .auth import AdminAuth, hash_password
from .config import ROOT, Settings, UserError
from .lock import InstanceLock
from .payroll import SheetsPayroll
from .preflight import validate_local
from .runtime import delivery_loop, heartbeat, payroll_loop
from .store import Store
from .ui import UI


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return f"{record.levelname}: {record.name} (подробности скрыты)"


def network_failure_types(error):
    """Expose exception types only: request URLs may contain the bot token."""
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 6:
        seen.add(id(error))
        result.append(type(error).__name__)
        error = error.__cause__ or error.__context__
    return " → ".join(result)


def setup():
    path = ROOT / ".env"
    existing = dotenv_values(path)
    if existing.get("ADMIN_PASSWORD_HASH"):
        raise UserError("Настройка уже выполнена. Для смены пароля используйте ChangePassword.cmd.")
    token = (
        existing.get("BOT_TOKEN") or getpass.getpass("Токен единого бота (ввод скрыт): ").strip()
    )
    ids = input("Telegram ID для ограничения админки (Enter — вход только по паролю): ").strip()
    if not re.fullmatch(r"\d+:[\w-]{30,}", token, flags=re.ASCII):
        raise UserError("Нужен корректный токен Telegram-бота.")
    if ids and any(not x.strip().isdigit() or int(x.strip()) <= 0 for x in ids.split(",")):
        raise UserError("Укажите числовые ID администраторов, не username.")
    password = getpass.getpass("Придумайте пароль админки (от 12 символов): ")
    if password != getpass.getpass("Повторите пароль: "):
        raise UserError("Пароли не совпали.")
    encoded = hash_password(password)
    for name, value in {
        "BOT_TOKEN": token,
        "ADMIN_IDS": ids,
        "ADMIN_PASSWORD_HASH": encoded,
    }.items():
        set_key(str(path), name, value, quote_mode="always")
    if os.name != "nt":
        path.chmod(0o600)
    print("Настройки сохранены. Запускайте бота; вход администратора — /admin.")


async def run(settings):
    store = Store(settings.data / "studio.sqlite3")
    bot = Bot(settings.bot_token)
    bots = {"model": bot, "admin": bot}
    payroll = SheetsPayroll(settings)
    auth = AdminAuth(store, settings)
    ui = UI(settings, store, payroll, bots, auth)
    tasks = []
    try:
        await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=False)
        tasks = [
            asyncio.create_task(delivery_loop(store, bots)),
            asyncio.create_task(payroll_loop(payroll)),
            asyncio.create_task(heartbeat(settings.data)),
        ]
        print(
            f"Бот {__version__} запущен. Вход администратора: /admin. Google-таблица обновляется раз в минуту и по кнопке."
        )
        poll = asyncio.create_task(
            ui.dispatcher.start_polling(
                bot, close_bot_session=False, handle_signals=sys.platform != "win32"
            )
        )
        tasks.append(poll)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # A failed persistence worker must stop the process, not vanish silently.
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ui.dispatcher.storage.close()
        await bot.session.close()
        store.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.basicConfig(level=logging.ERROR, handlers=[handler], force=True)
    parser = argparse.ArgumentParser(description="Studio Bot")
    parser.add_argument(
        "command", choices=["setup", "password", "check", "check-google", "run", "health"]
    )
    args = parser.parse_args()
    try:
        if args.command == "health":
            return 0 if time.time() - (ROOT / "data" / "heartbeat").stat().st_mtime < 120 else 1
        if args.command == "setup":
            setup()
        elif args.command == "password":
            password = getpass.getpass("Новый пароль (от 12 символов): ")
            if password != getpass.getpass("Повторите пароль: "):
                raise UserError("Пароли не совпали.")
            with InstanceLock(ROOT / "data" / "app.lock"):
                set_key(
                    str(ROOT / ".env"),
                    "ADMIN_PASSWORD_HASH",
                    hash_password(password),
                    quote_mode="always",
                )
                (ROOT / "data" / "admin-password.txt").unlink(missing_ok=True)
            print("Пароль изменён. На сервере пересоздайте контейнер, чтобы перечитать .env.")
        else:
            settings = Settings.load(require_tokens=args.command != "check-google")
            if args.command == "check-google":
                snapshot = SheetsPayroll(settings)._read_sync()
                print(f"Google Sheets: чтение успешно. Моделей: {len(snapshot.names())}.")
                print(snapshot.basis)
                for start, end in snapshot.coverage or ():
                    print(f"Доступный период: {start} — {end}")
            elif args.command == "check":
                validate_local(settings)
                print(
                    f"Настройки корректны. Комнат: {len(settings.rooms)}, часовой пояс: {settings.timezone}."
                )
                print(
                    f"Источников зарплат: {len(settings.sources)}. Ключ Google, фотографии и запись в data проверены. "
                    "Сетевой доступ проверяется отдельно: CheckGoogle.cmd."
                )
            else:
                validate_local(settings)
                with InstanceLock(settings.data / "app.lock"):
                    asyncio.run(run(settings))
        return 0
    except KeyboardInterrupt:
        print("Бот остановлен. Начатые смены сохранены и продолжат отображаться после запуска.")
        return 0
    except TelegramNetworkError as error:
        print(
            "Нет соединения с Telegram API: " + network_failure_types(error) + ". "
            "Проверьте исходящий HTTPS-доступ хостинга к api.telegram.org:443. "
            "Адреса запросов и токен не выводятся."
        )
        try:
            response = requests.get("https://api.telegram.org", timeout=10)
            print(f"Проверка HTTPS к Telegram без токена: HTTP {response.status_code}.")
        except requests.RequestException as probe_error:
            print("Проверка HTTPS к Telegram без токена: " + network_failure_types(probe_error))
        return 1
    except (UserError, ValueError) as error:
        print(
            str(error)
            if isinstance(error, UserError)
            else "Проверьте формат настроек и длину пароля."
        )
        return 1
    except Exception as error:
        print(
            f"Запуск остановлен ({type(error).__name__}). Проверьте подключение и настройки. Секреты не выводятся."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
