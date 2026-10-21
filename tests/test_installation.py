import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from studio_bot.config import Settings, UserError
from studio_bot.preflight import validate_local

ROOT = Path(__file__).resolve().parents[1]


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("studio_bootstrap", ROOT / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configured(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    target = tmp_path / "key.json"
    target.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "test@example.invalid",
                "token_uri": "https://oauth2.googleapis.com/token",
                "private_key": private,
            }
        ),
        encoding="utf-8",
    )
    return Settings(tmp_path, google_credentials="key.json", sources=[{}])


def test_preflight_valid_key_creates_data_without_database(configured):
    validate_local(configured)
    assert configured.data.is_dir()
    assert not list(configured.data.iterdir())


def test_missing_google_key_explains_separate_transfer(configured):
    configured.credentials_path.unlink()
    with pytest.raises(UserError, match="Ключ переносится отдельно"):
        validate_local(configured)
    assert not configured.data.exists()


def test_bad_google_key_never_reveals_contents(configured):
    configured.credentials_path.write_text("invalid-private-secret", encoding="utf-8")
    with pytest.raises(UserError, match="некорректен") as caught:
        validate_local(configured)
    assert "invalid-private-secret" not in str(caught.value)


def test_missing_photos_block_startup(configured):
    configured.room_photos = {"4": ["assets/a.png", "assets/b.png"]}
    with pytest.raises(UserError, match="Нет фотографий для комнаты 4"):
        validate_local(configured)


def test_google_key_from_dotenv_and_environment_override(tmp_path, monkeypatch):
    (tmp_path / "studio.toml").write_text('rooms=["4"]', encoding="utf-8")
    (tmp_path / ".env").write_text(
        "GOOGLE_APPLICATION_CREDENTIALS='keys/local.json'", encoding="utf-8"
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert Settings.load(tmp_path, False).credentials_path == tmp_path / "keys/local.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "override.json"))
    assert Settings.load(tmp_path, False).credentials_path == tmp_path / "override.json"


def test_missing_token_has_setup_instruction(tmp_path, monkeypatch):
    for name in ("BOT_TOKEN", "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "studio.toml").write_text('rooms=["4"]', encoding="utf-8")
    with pytest.raises(UserError, match="BOT_TOKEN.*Setup.cmd"):
        Settings.load(tmp_path)


def test_unsupported_python_stops_before_install(monkeypatch, capsys):
    module = load_bootstrap()
    monkeypatch.setattr(module.sys, "version_info", (3, 10, 9))
    assert module.main(["install"]) == 1
    assert "3.11" in capsys.readouterr().out


def test_manifest_detects_missing_or_changed_files(tmp_path):
    module = load_bootstrap()
    data = b"complete bundle"
    file = tmp_path / ".env.example"
    file.write_bytes(data)
    (tmp_path / "MANIFEST.sha256").write_text(
        f"{hashlib.sha256(data).hexdigest()}  .env.example\n", encoding="utf-8"
    )
    assert module.verify_bundle(tmp_path)
    file.write_bytes(b"broken")
    assert not module.verify_bundle(tmp_path)
    file.unlink()
    assert not module.verify_bundle(tmp_path)


def test_manifest_rejects_path_outside_bundle(tmp_path):
    module = load_bootstrap()
    (tmp_path / "MANIFEST.sha256").write_text("0  ../outside.txt\n", encoding="utf-8")
    assert not module.verify_bundle(tmp_path)
