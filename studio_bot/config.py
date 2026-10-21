from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(os.environ.get("STUDIO_ROOT", Path(__file__).resolve().parent.parent))


class UserError(ValueError):
    pass


@dataclass
class Settings:
    root: Path
    bot_token: str = field(default="", repr=False)
    password_hash: str = field(default="", repr=False)
    google_credentials: str = field(default="", repr=False)
    admin_ids: tuple[int, ...] = ()
    timezone: str = "Europe/Moscow"
    rooms: tuple[str, ...] = tuple(str(x) for x in range(4, 16))
    slots: tuple[tuple[str, str], ...] = (
        ("00:00", "06:00"),
        ("06:00", "12:00"),
        ("12:00", "18:00"),
        ("18:00", "00:00"),
    )
    sources: list[dict] = field(default_factory=list)
    payroll_period: str = "monthly"
    room_photos: dict[str, list[str]] = field(default_factory=dict)

    def photos(self, room):
        paths = []
        for relative in self.room_photos.get(room, []):
            path = (self.root / relative).resolve()
            if not path.is_relative_to(
                (self.root / "assets").resolve()
            ) or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                raise UserError("Проверьте пути фотографий комнат в studio.toml.")
            paths.append(path)
        return paths

    @property
    def zone(self):
        return ZoneInfo(self.timezone)

    @property
    def data(self):
        return self.root / "data"

    @property
    def credentials_path(self):
        path = Path(self.google_credentials).expanduser() if self.google_credentials else (
            self.data / "google-service-account.json"
        )
        return path if path.is_absolute() else self.root / path

    @classmethod
    def load(cls, root=ROOT, require_tokens=True):
        try:
            values = {**dotenv_values(root / ".env"), **os.environ}
        except OSError:
            raise UserError("Не удалось прочитать .env. Проверьте права доступа.") from None
        try:
            conf = tomllib.loads((root / "studio.toml").read_text(encoding="utf8"))
            settings = cls(
                root=root,
                bot_token=values.get("BOT_TOKEN", ""),
                password_hash=values.get("ADMIN_PASSWORD_HASH", ""),
                google_credentials=values.get("GOOGLE_APPLICATION_CREDENTIALS", "") or "",
                admin_ids=tuple(
                    int(x.strip()) for x in values.get("ADMIN_IDS", "").split(",") if x.strip()
                ),
                timezone=conf.get("timezone", "Europe/Moscow"),
                rooms=tuple(conf["rooms"]),
                slots=tuple((x["start"], x["end"]) for x in conf.get("slots", [])),
                sources=conf.get("payroll_sources", []),
                payroll_period=conf.get("payroll_period", "monthly"),
                room_photos=conf.get("room_photos", {}),
            )
            if settings.payroll_period not in ("monthly", "semimonthly"):
                raise ValueError()
            _ = settings.zone
            if not 1 <= len(settings.rooms) <= 30 or len(set(settings.rooms)) != len(
                settings.rooms
            ):
                raise ValueError()
            if any(not isinstance(x, str) or not 1 <= len(x) <= 40 for x in settings.rooms):
                raise ValueError()
            if len(settings.slots) > 12:
                raise ValueError()
            for start, end in settings.slots:
                if start == end or not all(
                    re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in (start, end)
                ):
                    raise ValueError()
            if any(x <= 0 for x in settings.admin_ids):
                raise ValueError()
            for room, photos in settings.room_photos.items():
                if room not in settings.rooms or not isinstance(photos, list) or len(photos) != 2:
                    raise ValueError()
                settings.photos(room)
            if require_tokens:
                if not re.fullmatch(r"\d+:[\w-]{30,}", settings.bot_token, flags=re.ASCII):
                    raise UserError(
                        "Не задан или неверен BOT_TOKEN. Выполните Setup.cmd / python3 bootstrap.py setup; "
                        "на сервере — sh Server.sh setup. Рабочий .env в архив не входит."
                    )
                if not settings.password_hash.startswith("pbkdf2_sha256$"):
                    raise UserError("Не настроен пароль администратора. Выполните setup.")
            return settings
        except UserError:
            raise
        except FileNotFoundError:
            raise UserError("Не найден studio.toml. Распакуйте ZIP полностью, вместе с настройками.") from None
        except tomllib.TOMLDecodeError:
            raise UserError("В studio.toml ошибка формата TOML. Проверьте кавычки и секции.") from None
        except Exception as error:
            raise UserError("Проверьте studio.toml и локальную настройку Setup.cmd.") from error
