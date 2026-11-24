import hashlib
import json
import sqlite3
from unittest.mock import MagicMock, Mock
from zipfile import ZipFile

import pytest
from dotenv import dotenv_values

from studio_bot.config import Settings, UserError
from studio_bot.full_backup import REQUIRED, export_before_start, verify_full_archive


@pytest.fixture
def full_settings(tmp_path):
    for name in REQUIRED - {
        ".env",
        "data/studio.sqlite3",
        "data/google-service-account.json",
        "secrets/google-service-account.json",
        "RESTORE-FULL.txt",
        "FULL-BACKUP.json",
    }:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# synthetic application file\n", encoding="utf-8")
    (tmp_path / "studio.toml").write_text('rooms=["4"]', encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    key = {
        "type": "service_account",
        "client_email": "synthetic@example.test",
        "private_key": "PRIVATE_TEST_KEY",
    }
    (data / "google-service-account.json").write_text(json.dumps(key), encoding="utf-8")
    (data / "operator-note.txt").write_text("PRIVATE_NOTE", encoding="utf-8")
    (data / "app.lock").write_text("transient")
    (data / "heartbeat").write_text("transient")
    photo = tmp_path / "assets" / "rooms" / "4-1.png"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"ORIGINAL_PHOTO_BYTES")
    (tmp_path / ".env").write_text("OLD_ENV_SHOULD_NOT_OVERRIDE_WORKING_CONFIG=1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("NOT_AN_APPLICATION_SECRET")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "cache.txt").write_text("NOT_RECOVERY_DATA")
    db = sqlite3.connect(data / "studio.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE people(user_id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE admin_members(user_id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO people VALUES (1,'PRIVATE_EMPLOYEE')")
    db.execute("INSERT INTO admin_members VALUES (99)")
    db.commit()
    settings = Settings(
        tmp_path,
        bot_token="PRIVATE_BOT_TOKEN",
        password_hash="PRIVATE_PASSWORD_HASH",
        admin_ids=(99,),
        backup_request="old-backup",
        export_request="test-full",
    )
    yield settings, db
    db.close()


def test_complete_recovery_contains_exact_data_credentials_and_assets(
    full_settings, tmp_path, capsys
):
    settings, db = full_settings
    db.execute("INSERT INTO people VALUES (2,'COMMITTED_WAL_EMPLOYEE')")
    db.commit()
    target = export_before_start(settings)
    verify_full_archive(target)
    with ZipFile(target) as archive:
        names = archive.namelist()
        assert {"studio-bot/" + name for name in REQUIRED}.issubset(names)
        assert archive.read("studio-bot/assets/rooms/4-1.png") == b"ORIGINAL_PHOTO_BYTES"
        assert archive.read("studio-bot/data/operator-note.txt") == b"PRIVATE_NOTE"
        assert archive.read("studio-bot/data/google-service-account.json") == archive.read(
            "studio-bot/secrets/google-service-account.json"
        )
        restored = tmp_path / "restored"
        archive.extractall(restored)
        env = dotenv_values(restored / "studio-bot/.env")
        assert env["BOT_TOKEN"] == settings.bot_token
        assert env["ADMIN_PASSWORD_HASH"] == settings.password_hash
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "data/google-service-account.json"
        assert env["ADMIN_IDS"] == "99"
        assert env["STUDIO_BACKUP_REQUEST"] == env["STUDIO_EXPORT_REQUEST"] == ""
        meta = json.loads(archive.read("studio-bot/FULL-BACKUP.json"))
        assert meta["database_integrity"] == "ok" and meta["contains_working_credentials"] is True
        assert not any(
            "/.git/" in name
            or "/.venv/" in name
            or name.endswith(("-wal", "-shm", ".tmp", "/heartbeat", "/app.lock"))
            for name in names
        )
    with sqlite3.connect(restored / "studio-bot/data/studio.sqlite3") as copy:
        assert copy.execute("SELECT count(*) FROM people").fetchone()[0] == 2
        assert copy.execute("SELECT * FROM admin_members").fetchall() == [(99,)]
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("SELECT count(*) FROM people").fetchone()[0] == 2
    output = capsys.readouterr().out
    assert "Full backup: OK" in output
    assert not any(
        value in output
        for value in (
            settings.bot_token,
            settings.password_hash,
            "PRIVATE_TEST_KEY",
            "PRIVATE_EMPLOYEE",
            "PRIVATE_NOTE",
        )
    )


def test_requested_export_is_idempotent_and_does_not_include_previous_exports(full_settings):
    settings, db = full_settings
    target = export_before_start(settings)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    db.execute("INSERT INTO people VALUES (2,'later')")
    db.commit()
    assert export_before_start(settings) == target
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    settings.export_request = "second-full"
    second = export_before_start(settings)
    with ZipFile(second) as archive:
        assert not any("/exports/" in name for name in archive.namelist())


def test_source_release_still_excludes_private_recovery_archive(full_settings, tmp_path):
    import package_release

    settings, _ = full_settings
    export_before_start(settings)
    for name in package_release.FILES:
        file = settings.root / name
        if not file.exists():
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("synthetic public file\n", encoding="utf-8")
    target = package_release.build(settings.root, tmp_path / "public.zip")
    with ZipFile(target) as archive:
        assert not any(
            "/data/" in n or "/secrets/" in n or n.endswith("/.env") for n in archive.namelist()
        )


@pytest.mark.parametrize("problem", ["token", "hash", "key", "source", "database", "invalid_label"])
def test_partial_recovery_is_never_claimed_complete(full_settings, problem):
    settings, db = full_settings
    if problem == "token":
        settings.bot_token = ""
    elif problem == "hash":
        settings.password_hash = ""
    elif problem == "key":
        settings.credentials_path.unlink()
    elif problem == "source":
        (settings.root / "studio_bot/config.py").unlink()
    elif problem == "database":
        db.close()
        (settings.data / "studio.sqlite3").unlink()
    else:
        settings.export_request = "../escape"
    with pytest.raises(UserError):
        export_before_start(settings)
    assert not list((settings.data / "exports").glob("*.zip"))
    assert not list((settings.data / "exports").glob("*.tmp"))


def test_archive_corruption_is_detected_without_overwriting_it(full_settings):
    settings, _ = full_settings
    target = export_before_start(settings)
    target.write_bytes(b"not a ZIP")
    with pytest.raises(UserError):
        export_before_start(settings)
    assert target.read_bytes() == b"not a ZIP"


def test_export_is_disabled_without_explicit_request(tmp_path):
    assert export_before_start(Settings(tmp_path)) is None
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("failed", [False, True])
def test_configured_sheet_values_and_formulas_are_backed_up_read_only(
    full_settings, monkeypatch, failed
):
    from google.auth.transport import requests as transport
    from google.oauth2 import service_account

    settings, _ = full_settings
    settings.sources = [
        {
            "spreadsheet_id": "test-sheet-123",
            "format": "studio_daily_share",
            "month": "2026-11",
            "range": "'Чеки'!H1:AR",
            "shares_range": "'Модели'!G1:J",
        }
    ]
    credentials = Mock()
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", credentials)
    factory = MagicMock()
    monkeypatch.setattr(transport, "AuthorizedSession", factory)
    session = factory.return_value.__enter__.return_value
    response = session.get.return_value
    response.json.return_value = {
        "valueRanges": [{"values": [["PRIVATE_PAYROLL"]]}, {"values": [["PRIVATE_SHARE"]]}]
    }
    if failed:
        response.raise_for_status.side_effect = RuntimeError("PRIVATE_TOKEN_URL")
        with pytest.raises(UserError) as error:
            export_before_start(settings)
        assert "PRIVATE_TOKEN_URL" not in str(error.value)
        assert not list((settings.data / "exports").glob("*.zip"))
    else:
        target = export_before_start(settings)
        with ZipFile(target) as archive:
            for rendering in ("unformatted_value", "formula"):
                payload = json.loads(archive.read(f"studio-bot/sheets/source-01-{rendering}.json"))
                assert payload["response"]["valueRanges"][0]["values"] == [["PRIVATE_PAYROLL"]]
        assert session.get.call_count == 2
        assert {
            call.kwargs["params"]["valueRenderOption"] for call in session.get.call_args_list
        } == {"UNFORMATTED_VALUE", "FORMULA"}
    assert credentials.call_args.kwargs["scopes"] == [
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
    assert not session.post.called and not session.put.called
