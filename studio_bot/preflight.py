"""Local startup checks: no Telegram messages, Google requests or database changes."""

import tempfile

from .config import UserError


def validate_local(settings):
    from google.oauth2.service_account import Credentials

    if not settings.sources:
        raise UserError(
            "Не подключены зарплатные таблицы: заполните payroll_sources в studio.toml."
        )
    path = settings.credentials_path
    if not path.is_file():
        raise UserError(
            "Ключ сервисного аккаунта Google не найден. Положите JSON в "
            "data/google-service-account.json (Python) или secrets/google-service-account.json "
            "(Docker). Другой путь задаётся GOOGLE_APPLICATION_CREDENTIALS. "
            "Ключ переносится отдельно от ZIP."
        )
    try:
        Credentials.from_service_account_file(
            str(path), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
    except Exception:
        raise UserError(
            "Ключ Google недоступен или некорректен. Нужен JSON-ключ сервисного аккаунта; "
            "проверьте формат и права чтения. Содержимое ключа не выводится."
        ) from None
    for room in settings.room_photos:
        if any(not photo.is_file() for photo in settings.photos(room)):
            raise UserError(f"Нет фотографий для комнаты {room}. Распакуйте папку assets из ZIP.")
    try:
        settings.data.mkdir(exist_ok=True)
        with tempfile.TemporaryFile(dir=settings.data):
            pass
    except OSError:
        raise UserError(
            "Нет права записи в data. Проверьте папку и владельца Docker-тома."
        ) from None
