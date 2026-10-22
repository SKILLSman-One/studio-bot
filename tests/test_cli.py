import os
import subprocess
import sys
import tomllib
from pathlib import Path


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
