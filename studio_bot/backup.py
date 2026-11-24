"""Opt-in, idempotent SQLite snapshot before application startup/migrations."""

import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .config import UserError


def verify(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise UserError("Резервная копия SQLite не прошла проверку целостности.")
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='people'"
        ).fetchone():
            raise UserError("В резервной копии нет таблицы сотрудников. Запуск остановлен.")


def backup_before_start(settings):
    request = settings.backup_request
    if not request:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request):
        raise UserError("STUDIO_BACKUP_REQUEST: используйте до 64 латинских букв, цифр, _ или -.")
    source = settings.data / "studio.sqlite3"
    if not source.is_file():
        raise UserError("Рабочая SQLite не найдена. Запуск остановлен; пустая база не создавалась.")
    folder = settings.data / "backups"
    target = folder / f"studio-{request}.sqlite3"
    temporary = None
    try:
        folder.mkdir(exist_ok=True)
        if not folder.resolve().is_relative_to(settings.data.resolve()):
            raise UserError("Папка резервных копий должна находиться внутри data.")
        if target.is_symlink():
            raise UserError("Файл резервной копии не должен быть символической ссылкой.")
        if target.exists():
            verify(target)
        else:
            with tempfile.NamedTemporaryFile(
                dir=folder, prefix="sqlite-", suffix=".tmp", delete=False
            ) as f:
                temporary = Path(f.name)
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(temporary)) as dst:
                    src.backup(dst)
            verify(temporary)
            temporary.replace(target)
            temporary = None
        print(f"SQLite backup: OK; integrity=ok; file=data/backups/{target.name}", flush=True)
        return target
    except (OSError, sqlite3.Error) as error:
        raise UserError(
            f"Не удалось сохранить резервную копию SQLite ({type(error).__name__}). Запуск остановлен."
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
