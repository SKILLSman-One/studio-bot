"""Owner-requested private recovery ZIP, including credentials and a fresh SQLite snapshot."""

import hashlib
import json
import platform
import re
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from package_release import FILES

from . import __version__
from .backup import backup_before_start
from .config import UserError

PREFIX = "studio-bot/"
REQUIRED = {
    ".env",
    "main.py",
    "studio.toml",
    "requirements.txt",
    "pyproject.toml",
    "studio_bot/__main__.py",
    "studio_bot/config.py",
    "data/studio.sqlite3",
    "data/google-service-account.json",
    "secrets/google-service-account.json",
    "RESTORE-FULL.txt",
    "FULL-BACKUP.json",
}
RESTORE = """ПОЛНЫЙ БЭКАП STUDIO BOT — СОДЕРЖИТ РАБОЧИЕ СЕКРЕТЫ

В комплекте: исходники, фотографии, настройки, токен бота, хеш пароля
администратора, ключ Google и согласованная SQLite со всеми таблицами.
База сохранена через SQLite backup API с учётом WAL. Исходные WAL/SHM,
блокировки и heartbeat не нужны для восстановления и не включены.

1. Распакуйте весь ZIP. Перед запуском восстановленной копии остановите старый
   экземпляр бота, чтобы два процесса не опрашивали один Telegram-аккаунт.
2. На Windows запустите Verify.cmd, затем Install.cmd и Start.cmd.
   Если .cmd отсутствуют: Python 3.11+; python -m venv .venv;
   установите requirements.txt в .venv и запустите python main.py.
3. На Linux: создайте venv, установите requirements.txt, запустите main.py.
   Для Docker предусмотрены compose.yaml, data/ и secrets/ с ключом Google.
4. Рабочий .env уже заполнен. Путь ключа в нём относительный. Пароль админки
   сохраняется прежним: в комплект включён его хеш, не восстановленный текст.
5. Контрольные суммы файлов находятся в MANIFEST.sha256. FULL-BACKUP.json
   содержит время создания и версию. Сам архив — приватный файл с ключами.

В папке sheets/ сохранены значения и формулы настроенных зарплатных диапазонов.
Полные документы Google со всеми вкладками и оформлением остаются в Google:
здесь сохранены настройки подключения и ключ для доступа к ним.
Необязательные разовые задания резервирования/экспорта в .env очищены;
исходные метки сохранены в FULL-BACKUP.json. История SQLite и привязки сохранены.
"""


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_full_archive(path):
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not {PREFIX + n for n in REQUIRED}.issubset(names):
            raise UserError("Полный бэкап неполон или содержит повторяющиеся файлы.")
        lines = archive.read(PREFIX + "MANIFEST.sha256").decode("utf-8").splitlines()
        expected = set()
        for line in lines:
            wanted, name = line.split("  ", 1)
            member = PREFIX + name
            if member in expected or not re.fullmatch(r"[0-9a-f]{64}", wanted):
                raise UserError("Некорректный манифест полного бэкапа.")
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise UserError("Небезопасный путь в полном бэкапе.")
            expected.add(member)
            digest = hashlib.sha256()
            with archive.open(member) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != wanted:
                raise UserError("Не совпали контрольные суммы полного бэкапа.")
        if expected != set(names) - {PREFIX + "MANIFEST.sha256"}:
            raise UserError("Манифест полного бэкапа не охватывает все файлы.")


def _portable_env(settings):
    if not settings.bot_token or not settings.password_hash:
        raise UserError("Нет рабочего токена или хеша пароля. Неполный архив не создан.")
    values = {
        "BOT_TOKEN": settings.bot_token,
        "ADMIN_IDS": ",".join(str(uid) for uid in settings.admin_ids),
        "ADMIN_PASSWORD_HASH": settings.password_hash,
        "GOOGLE_APPLICATION_CREDENTIALS": "data/google-service-account.json",
        "STUDIO_BACKUP_REQUEST": "",
        "STUDIO_EXPORT_REQUEST": "",
    }
    lines = ["# Private recovery configuration; contains working credentials."]
    for key, value in values.items():
        if any(ord(char) < 32 for char in value):
            raise UserError("Недопустимые символы в рабочих настройках. Экспорт остановлен.")
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _source_files(root):
    paths = [root / name for name in FILES if (root / name).is_file()]
    paths += sorted((root / "studio_bot").glob("*.py"))
    paths += sorted((root / "tests").glob("*.py"))
    paths += sorted(p for p in (root / "assets").rglob("*") if p.is_file())
    result = {}
    for path in paths:
        if not path.resolve().is_relative_to(root.resolve()):
            raise UserError("Файл исходников находится за пределами проекта. Экспорт остановлен.")
        result[path.relative_to(root).as_posix()] = path
    return result


def _sheet_snapshots(settings):
    if not settings.sources:
        return {}
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(
        str(settings.credentials_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    result = {}
    with AuthorizedSession(credentials, refresh_timeout=15) as session:
        for index, source in enumerate(settings.sources, 1):
            sid = source["spreadsheet_id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", sid):
                raise UserError("Некорректный ID зарплатного источника. Полный экспорт остановлен.")
            ranges = [source["range"]]
            if source.get("format") == "studio_daily_share":
                ranges.append(source["shares_range"])
            for rendering in ("UNFORMATTED_VALUE", "FORMULA"):
                response = session.get(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchGet",
                    params={
                        "ranges": ranges,
                        "valueRenderOption": rendering,
                        "dateTimeRenderOption": "SERIAL_NUMBER",
                    },
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                if len(payload.get("valueRanges", [])) != len(ranges):
                    raise UserError("Неполный снимок зарплатных диапазонов. Экспорт остановлен.")
                result[f"sheets/source-{index:02}-{rendering.lower()}.json"] = json.dumps(
                    {
                        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                        "spreadsheet_id": sid,
                        "month": source.get("month"),
                        "rendering": rendering,
                        "response": payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
    return result


def export_before_start(settings):
    label = settings.export_request
    if not label:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", label):
        raise UserError("STUDIO_EXPORT_REQUEST: до 48 латинских букв, цифр, _ или -.")
    folder = settings.data / "exports"
    target = folder / f"Studio-Bot-FULL-{label}.zip"
    temporary = None
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        if not folder.resolve().is_relative_to(settings.data.resolve()) or target.is_symlink():
            raise UserError("Полный бэкап должен находиться внутри data/exports.")
        if target.exists():
            verify_full_archive(target)
        else:
            root = settings.root
            files = _source_files(root)
            working_env = _portable_env(settings)
            credentials = settings.credentials_path.read_bytes()
            key = json.loads(credentials)
            if (
                key.get("type") != "service_account"
                or not key.get("private_key")
                or not key.get("client_email")
            ):
                raise UserError("Нет полного рабочего ключа Google. Экспорт остановлен.")
            snapshot = backup_before_start(replace(settings, backup_request="export-" + label))
            for path in sorted(settings.data.rglob("*")):
                relative = path.relative_to(settings.data)
                if not path.is_file() or relative.parts[0] == "exports":
                    continue
                if path.name in {
                    "studio.sqlite3",
                    "app.lock",
                    "heartbeat",
                    "google-service-account.json",
                }:
                    continue
                if path.name.endswith(("-wal", "-shm", ".tmp")):
                    continue
                if not path.resolve().is_relative_to(settings.data.resolve()):
                    raise UserError("Файл данных находится за пределами data. Экспорт остановлен.")
                files["data/" + relative.as_posix()] = path
            files["data/studio.sqlite3"] = snapshot
            generated = {
                ".env": working_env,
                "data/google-service-account.json": credentials,
                "secrets/google-service-account.json": credentials,
                "RESTORE-FULL.txt": RESTORE.encode("utf-8"),
                "FULL-BACKUP.json": json.dumps(
                    {
                        "version": __version__,
                        "created_at_utc": datetime.now(timezone.utc).isoformat(),
                        "python": platform.python_version(),
                        "database_integrity": "ok",
                        "contains_working_credentials": True,
                        "entry_point": "main.py",
                        "original_backup_request": settings.backup_request,
                        "original_export_request": label,
                        "database_snapshot_at_utc": datetime.fromtimestamp(
                            snapshot.stat().st_mtime, timezone.utc
                        ).isoformat(),
                        "configured_google_ranges_included": bool(settings.sources),
                        "full_external_google_documents_included": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            }
            try:
                generated.update(_sheet_snapshots(settings))
            except UserError:
                raise
            except Exception as error:
                raise UserError(
                    f"Не удалось включить зарплатные диапазоны ({type(error).__name__}). Полный экспорт не завершён."
                ) from None
            if not REQUIRED.issubset(set(files) | set(generated)):
                raise UserError(
                    "Не хватает исходников для полного восстановления. Экспорт остановлен."
                )
            with tempfile.NamedTemporaryFile(
                dir=folder, prefix="full-", suffix=".tmp", delete=False
            ) as tmp:
                temporary = Path(tmp.name)
            manifest = []
            with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
                for name, path in sorted(files.items()):
                    manifest.append(f"{digest_file(path)}  {name}\n")
                    archive.write(path, PREFIX + name)
                for name, payload in sorted(generated.items()):
                    manifest.append(f"{hashlib.sha256(payload).hexdigest()}  {name}\n")
                    archive.writestr(PREFIX + name, payload)
                archive.writestr(PREFIX + "MANIFEST.sha256", "".join(manifest).encode("utf-8"))
            verify_full_archive(temporary)
            temporary.replace(target)
            temporary = None
        print(
            f"Full backup: OK; file=data/exports/{target.name}; bytes={target.stat().st_size}; "
            f"sha256={digest_file(target)}; credentials=included",
            flush=True,
        )
        return target
    except (OSError, ValueError, BadZipFile, KeyError) as error:
        if isinstance(error, UserError):
            raise
        raise UserError(
            f"Не удалось собрать полный бэкап ({type(error).__name__}). Секреты не выводятся."
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
