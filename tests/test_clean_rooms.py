from pathlib import Path
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMediaGroup, SendMessage
from conftest import click, texts

from studio_bot.config import Settings
from studio_bot.store import Store


def add_photos(ui):
    directory = ui.settings.root / "assets" / "rooms"
    directory.mkdir(parents=True)
    for name in ("4-1.png", "4-2.png"):
        (directory / name).write_bytes(b"test-photo")
    ui.settings.room_photos = {"4": ["assets/rooms/4-1.png", "assets/rooms/4-2.png"]}


async def test_room_album_then_only_yes_and_persisted_confirmation(ui):
    add_photos(ui)
    await click(ui, "room:0")
    await click(ui, "end:1")
    assert ui.store.active_shifts()
    calls = ui.bots["model"].session.calls
    album = next(c for c in calls if isinstance(c, SendMediaGroup))
    assert [Path(photo.media.path).name for photo in album.media] == ["4-1.png", "4-2.png"]
    prompt = [c for c in calls if isinstance(c, SendMessage)][-1]
    buttons = [b for row in prompt.reply_markup.inline_keyboard for b in row]
    assert len(buttons) == 1 and buttons[0].text == "✅ Да, всё чисто"
    token = ui.pending[("model", 10)][2]
    await click(ui, f"cleanconfirm:{token}", 20)
    assert ui.store.active_shifts()
    await click(ui, f"cleanconfirm:{token}")
    assert not ui.store.active_shifts()
    assert ui.store.one("SELECT clean_confirmed_at FROM shifts WHERE id=1")[0] is not None
    await click(ui, f"cleanconfirm:{token}")
    assert ui.store.one("SELECT count(*) FROM audit WHERE action='confirm_room_clean'")[0] == 1
    reopened = Store(ui.settings.root / "studio.sqlite3")
    try:
        assert reopened.one("SELECT clean_confirmed_at FROM shifts WHERE id=1")[0] is not None
    finally:
        reopened.close()


async def test_stale_end_button_does_not_end_next_shift(ui):
    await click(ui, "room:0")
    await click(ui, "end:1")
    token = ui.pending[("model", 10)][2]
    ui.store.end_shift(1, 99, admin=True)
    await click(ui, "room:0")
    await click(ui, f"cleanconfirm:{token}")
    assert ui.store.active_shifts()[0]["id"] == 2
    await click(ui, "end:1")
    assert ui.store.active_shifts()[0]["id"] == 2


async def test_failed_or_missing_album_keeps_shift_open(ui, monkeypatch):
    add_photos(ui)
    await click(ui, "room:0")
    monkeypatch.setattr(
        ui.bots["model"],
        "send_media_group",
        AsyncMock(
            side_effect=TelegramNetworkError(
                method=SendMediaGroup(chat_id=10, media=[]), message="network"
            )
        ),
    )
    await click(ui, "end:1")
    assert ui.store.active_shifts() and ("model", 10) not in ui.pending
    assert "Не удалось показать фотографии" in texts(ui)
    (ui.settings.root / "assets/rooms/4-1.png").unlink()
    await click(ui, "end:1")
    assert ui.store.active_shifts() and ("model", 10) not in ui.pending


async def test_clean_confirmation_expires_and_admin_can_end_without_attestation(ui):
    await click(ui, "room:0")
    await click(ui, "end:1")
    token = ui.pending[("model", 10)][2]
    stamp = ui.store.clock()
    ui.store.clock = lambda: stamp + 1801
    await click(ui, f"cleanconfirm:{token}")
    assert ui.store.active_shifts()
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "adminend:1", 99, "admin")
    assert not ui.store.active_shifts()
    assert ui.store.one("SELECT clean_confirmed_at FROM shifts")[0] is None


def test_shipped_room_mapping_and_image_bounds():
    root = Path(__file__).resolve().parent.parent
    settings = Settings.load(root, require_tokens=False)
    assert list(settings.room_photos) == ["4", "8", "9", "10", "12", "13", "14"]
    for room in settings.room_photos:
        paths = settings.photos(room)
        assert len(paths) == 2
        for i, path in enumerate(paths, 1):
            assert path.name == f"{room}-{i}.png"
            assert 0 < path.stat().st_size < 10_000_000
            assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert settings.photos("5") == []
