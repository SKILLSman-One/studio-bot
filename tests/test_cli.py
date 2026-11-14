import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError

from studio_bot.__main__ import network_failure_types, prepare_telegram, telegram_startup_step
from studio_bot.config import UserError


def test_network_diagnostic_keeps_urls_and_credentials_private():
    failure = TelegramNetworkError(method=None, message="bot-secret-token/request")
    failure.__cause__ = TimeoutError("https://api.telegram.org/bot-secret-token/getMe")
    failure.__cause__.__context__ = failure
    assert network_failure_types(failure) == "TelegramNetworkError → TimeoutError"


async def test_startup_recovers_from_temporary_errors_without_discarding_updates(monkeypatch, capsys):
    secret = "https://api.telegram.org/bot-secret/getMe"
    bot = SimpleNamespace(
        me=AsyncMock(side_effect=[TelegramServerError(method=None, message=secret), object()]),
        delete_webhook=AsyncMock(
            side_effect=[TelegramNetworkError(method=None, message=secret), True]
        ),
    )
    delay = AsyncMock()
    monkeypatch.setattr("studio_bot.__main__.asyncio.sleep", delay)
    await prepare_telegram(bot)
    assert bot.me.await_count == 2
    assert bot.delete_webhook.await_args_list == [call(drop_pending_updates=False)] * 2
    assert delay.await_args_list == [call(2), call(2)]
    out = capsys.readouterr().out
    assert "getMe" in out and "deleteWebhook" in out and secret not in out


async def test_startup_stops_after_six_server_errors(monkeypatch, capsys):
    secret = "secret-response"
    error = TelegramServerError(method=None, message=secret)
    action = AsyncMock(side_effect=error)
    delay = AsyncMock()
    monkeypatch.setattr("studio_bot.__main__.asyncio.sleep", delay)
    with pytest.raises(TelegramServerError):
        await telegram_startup_step("getMe", action)
    assert action.await_count == 6
    assert delay.await_args_list == [call(s) for s in (2, 4, 8, 16, 32)]
    result = capsys.readouterr().out
    assert "все попытки исчерпаны" in result and secret not in result


async def test_startup_does_not_retry_invalid_token():
    action = AsyncMock(
        side_effect=TelegramUnauthorizedError(method=None, message="private response")
    )
    with pytest.raises(UserError, match="BOT_TOKEN") as info:
        await telegram_startup_step("getMe", action)
    assert "private response" not in str(info.value)
    action.assert_awaited_once()


def test_uv_hosting_installs_the_same_runtime_dependencies_as_pip():
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf8"))
    pinned = {
        line.strip()
        for line in (root / "requirements.txt").read_text(encoding="utf8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert set(metadata["project"]["dependencies"]) == pinned
    assert metadata["project"]["requires-python"] == ">=3.11"
    assert metadata["tool"]["uv"]["package"] is False


def test_check_google_without_tokens_or_key_has_readable_error(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "studio.toml").write_bytes((root / "studio.toml").read_bytes())
    env = {
        **os.environ,
        "STUDIO_ROOT": str(tmp_path),
        "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "missing.json"),
        "PYTHONIOENCODING": "cp1252",
    }
    result = subprocess.run(
        [sys.executable, "-m", "studio_bot", "check-google"],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "Ключ сервисного аккаунта Google не найден" in result.stdout.decode("utf8")
    assert b"Traceback" not in result.stderr


def test_hosting_file_entry_starts_run_and_reports_missing_configuration(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "studio.toml").write_text('rooms=["4"]', encoding="utf8")
    env = {k: v for k, v in os.environ.items() if k not in {"BOT_TOKEN", "ADMIN_PASSWORD_HASH"}}
    env["STUDIO_ROOT"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "main.py"], cwd=root, env=env, capture_output=True, timeout=30
    )
    assert result.returncode == 1
    assert "BOT_TOKEN" in result.stdout.decode("utf8")
    assert b"Traceback" not in result.stderr
    assert not (tmp_path / "data/studio.sqlite3").exists()


def test_hosting_file_entry_accepts_explicit_command():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "main.py", "--help"], cwd=root, capture_output=True, timeout=30
    )
    assert result.returncode == 0
    assert b"check-google" in result.stdout
    assert b"Traceback" not in result.stderr
