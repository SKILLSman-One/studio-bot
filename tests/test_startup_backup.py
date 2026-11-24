import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from studio_bot.backup import backup_before_start
from studio_bot.config import Settings, UserError


def database(settings):
    settings.data.mkdir()
    db = sqlite3.connect(settings.data / "studio.sqlite3")
    db.execute("CREATE TABLE people(user_id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE admin_members(user_id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE bookings(id INTEGER PRIMARY KEY, room TEXT)")
    db.execute("INSERT INTO people VALUES (1,'PRIVATE_NAME')")
    db.execute("INSERT INTO admin_members VALUES (99)")
    db.execute("INSERT INTO bookings VALUES (5,'4')")
    db.commit()
    return db


def test_backup_is_opt_in_and_never_creates_an_empty_source(tmp_path):
    settings = Settings(tmp_path)
    assert backup_before_start(settings) is None
    assert not settings.data.exists()
    settings.backup_request = "before-079"
    with pytest.raises(UserError, match="пустая база не создавалась"):
        backup_before_start(settings)
    assert not settings.data.exists()


def test_copy_integrity_all_tables_and_idempotence_without_source_changes(tmp_path, capsys):
    settings = Settings(tmp_path, backup_request="before-079")
    database(settings).close()
    source = settings.data / "studio.sqlite3"
    before = source.read_bytes()
    target = backup_before_start(settings)
    assert source.read_bytes() == before
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("SELECT * FROM people").fetchall() == [(1, "PRIVATE_NAME")]
        assert db.execute("SELECT * FROM admin_members").fetchall() == [(99,)]
        assert db.execute("SELECT * FROM bookings").fetchall() == [(5, "4")]
    saved = target.read_bytes()
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO people VALUES (2,'new')")
    assert backup_before_start(settings) == target
    assert target.read_bytes() == saved  # A restart never overwrites the requested snapshot.
    assert "PRIVATE_NAME" not in capsys.readouterr().out


def test_committed_wal_records_are_included_in_snapshot(tmp_path):
    settings = Settings(tmp_path, backup_request="wal-test")
    db = database(settings)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO people VALUES (2,'wal entry')")
        db.commit()
        assert Path(str(settings.data / "studio.sqlite3") + "-wal").stat().st_size > 0
        target = backup_before_start(settings)
        with sqlite3.connect(target) as copy:
            assert copy.execute("SELECT count(*) FROM people").fetchone()[0] == 2
    finally:
        db.close()


@pytest.mark.parametrize("label", ["../escape", "/tmp/x", "a/b", "a\\b", "x" * 65])
def test_invalid_request_cannot_escape_backup_directory(tmp_path, label):
    settings = Settings(tmp_path, backup_request=label)
    with pytest.raises(UserError, match="STUDIO_BACKUP_REQUEST"):
        backup_before_start(settings)
    assert not settings.data.exists()


@pytest.mark.parametrize("existing", ["source", "backup"])
def test_corruption_stops_backup_without_overwriting_existing_data(tmp_path, existing):
    settings = Settings(tmp_path, backup_request="corrupt")
    database(settings).close()
    source = settings.data / "studio.sqlite3"
    bad = source if existing == "source" else settings.data / "backups" / "studio-corrupt.sqlite3"
    bad.parent.mkdir(exist_ok=True)
    bad.write_bytes(b"not a database")
    before = source.read_bytes()
    with pytest.raises(UserError):
        backup_before_start(settings)
    assert source.read_bytes() == before
    assert bad.read_bytes() == b"not a database"
    assert not list((settings.data / "backups").glob("*.tmp"))


def test_backup_with_wrong_schema_is_not_accepted(tmp_path):
    settings = Settings(tmp_path, backup_request="wrong-schema")
    database(settings).close()
    folder = settings.data / "backups"
    folder.mkdir()
    with sqlite3.connect(folder / "studio-wrong-schema.sqlite3") as db:
        db.execute("CREATE TABLE other(x)")
    with pytest.raises(UserError, match="нет таблицы сотрудников"):
        backup_before_start(settings)


def test_settings_loads_opt_in_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_BACKUP_REQUEST", raising=False)
    (tmp_path / "studio.toml").write_text('rooms=["4"]', encoding="utf-8")
    (tmp_path / ".env").write_text('STUDIO_BACKUP_REQUEST="before-079"', encoding="utf-8")
    assert Settings.load(tmp_path, require_tokens=False).backup_request == "before-079"


def test_run_checks_backup_before_starting_workers(tmp_path, monkeypatch):
    import studio_bot.__main__ as cli

    settings = Settings(tmp_path, backup_request="missing-working-db")
    settings.data.mkdir()
    monkeypatch.setattr(cli.Settings, "load", Mock(return_value=settings))
    monkeypatch.setattr(cli, "validate_local", Mock())
    run = Mock()
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr("sys.argv", ["studio_bot", "run"])
    assert cli.main() == 1
    run.assert_not_called()
