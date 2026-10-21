import os
import subprocess
import sys
from pathlib import Path


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
