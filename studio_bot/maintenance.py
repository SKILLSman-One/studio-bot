"""One-off local maintenance, never exposed as a Telegram action."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import UserError
from .lock import InstanceLock
from .store import Store


def reset_test_history(root: Path):
    root = root.resolve()
    source = root / "data" / "studio.sqlite3"
    if not source.is_file():
        raise UserError("Рабочая база не найдена; очистка отменена.")
    with InstanceLock(root / "data" / "app.lock"):
        backups = root / "backups"
        backups.mkdir(exist_ok=True)
        destination = (
            backups / f"before-reset-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.sqlite3"
        )
        # SQLite backup includes committed WAL contents, unlike copying the main file.
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        backup = sqlite3.connect(destination)
        try:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise UserError("Резервная копия не прошла проверку. Очистка отменена.")
            if backup.execute("PRAGMA foreign_key_check").fetchall():
                raise UserError("Связи резервной копии повреждены. Очистка отменена.")
        finally:
            backup.close()
            original.close()
        store = Store(source)
        try:
            store.clear_test_history()
            if store.db.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
                raise UserError("Проверьте базу перед запуском. Резервная копия сохранена.")
        finally:
            store.close()
        return destination
