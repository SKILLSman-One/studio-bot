from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiogram.methods import DeleteMessage, SendMessage
from conftest import click, message

from studio_bot.auth import AdminAuth, hash_password
from studio_bot.config import Settings


def sent_text(ui, uid):
    return "\n".join(
        x.text
        for x in ui.bots["model"].session.calls
        if isinstance(x, SendMessage) and int(x.chat_id) == uid
    )


async def test_password_only_login_default_and_notification_registration(ui):
    ui.settings.admin_ids = ()
    assert ui.bots["admin"] is ui.bots["model"]
    await message(ui, "/start", 30)
    assert "СТУДИЯ · АДМИНИСТРАТОР" not in sent_text(ui, 30)
    await message(ui, "/admin", 30)
    await message(ui, "incorrect", 30)
    assert not ui.auth.logged_in(30)
    await message(ui, "correct-password", 30)
    assert ui.auth.logged_in(30)
    assert "СТУДИЯ · АДМИНИСТРАТОР" in sent_text(ui, 30)
    assert ui.auth.recipients() == (30,)
    assert any(isinstance(x, DeleteMessage) for x in ui.bots["model"].session.calls)
    assert "correct-password" not in sent_text(ui, 30)
    await click(ui, "register", 40)
    await message(ui, "Новая модель", 40)
    await message(ui, "Комментарий", 40)
    assert ui.store.one("SELECT user_id FROM outbox WHERE role='admin'")["user_id"] == 30
    restored = AdminAuth(ui.store, ui.settings, clock=ui.store.clock)
    assert restored.recipients() == (30,) and not restored.logged_in(30)
    ui.settings.password_hash = hash_password("another-password")
    assert restored.recipients() == ()


async def test_admin_password_cannot_be_sent_as_model_contact(ui):
    ui.settings.admin_ids = ()
    await click(ui, "contact", 10)
    assert ui.pending[("model", 10)][0] == "contact"
    await message(ui, "/admin", 10)
    await message(ui, "correct-password", 10)
    assert not ui.store.all("SELECT * FROM tickets")
    assert ("model", 10) not in ui.pending
    assert ui.auth.logged_in(10)


async def test_model_button_while_admin_only_shows_own_salary(ui):
    ui.settings.admin_ids = ()
    assert await ui.auth.login(10, "correct-password")
    await click(ui, "salary:current", 10, "admin")
    assert "Мария" in sent_text(ui, 10)
    ui.bots["model"].session.calls.clear()
    await click(ui, "salary:current", 10, "model")
    assert "Мария" not in sent_text(ui, 10)
    assert "125.50" in sent_text(ui, 10)
    assert not ui.auth.logged_in(10)
    ui.payroll.get.reset_mock()
    await click(ui, "salary:current", 10, "admin")
    ui.payroll.get.assert_not_called()


async def test_expired_or_forged_admin_callback_cannot_mutate(ui):
    ui.settings.admin_ids = ()
    ui.store.start_shift(10, "4", ui.settings.rooms)
    await click(ui, "adminend:1", 20, "admin")
    assert len(ui.store.active_shifts()) == 1
    assert await ui.auth.login(20, "correct-password")
    ui.auth.sessions[20] = ui.store.clock() - 1
    await click(ui, "adminend:1", 20, "admin")
    assert len(ui.store.active_shifts()) == 1


async def test_cancel_login_and_logout_return_to_model_menu(ui):
    ui.settings.admin_ids = ()
    await message(ui, "/admin", 10)
    await message(ui, "/cancel", 10)
    assert ("admin", 10) not in ui.pending
    assert "ЛИЧНЫЙ КАБИНЕТ" in sent_text(ui, 10)
    await message(ui, "/admin", 10)
    await message(ui, "correct-password", 10)
    await message(ui, "/model", 10)
    assert not ui.auth.logged_in(10)
    assert ("admin", 10) not in ui.pending
    await message(ui, "   ", 10)  # No IndexError in middleware.


async def test_global_password_attempt_limit_persists(ui):
    ui.settings.admin_ids = ()
    for uid in range(100, 125):
        assert not await ui.auth.login(uid, "incorrect")
    restored = AdminAuth(ui.store, ui.settings, clock=ui.store.clock)
    assert not await restored.login(30, "correct-password")
    restored.clock = lambda: ui.store.clock() + 901
    assert await restored.login(30, "correct-password")


async def test_runtime_polls_and_closes_exactly_one_bot(tmp_path, monkeypatch):
    from studio_bot import __main__ as main

    bot = SimpleNamespace(
        get_me=AsyncMock(), delete_webhook=AsyncMock(), session=SimpleNamespace(close=AsyncMock())
    )
    bot_factory = Mock(return_value=bot)
    dispatcher = SimpleNamespace(
        start_polling=AsyncMock(), storage=SimpleNamespace(close=AsyncMock())
    )
    ui_factory = Mock(return_value=SimpleNamespace(dispatcher=dispatcher))
    monkeypatch.setattr(main, "Bot", bot_factory)
    monkeypatch.setattr(main, "UI", ui_factory)
    settings = Settings(tmp_path, bot_token="12345:" + "x" * 35)
    await main.run(settings)
    bot_factory.assert_called_once_with(settings.bot_token)
    assert dispatcher.start_polling.call_args.args == (bot,)
    bot.session.close.assert_awaited_once()


def test_single_token_configuration_without_admin_ids(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    (tmp_path / "studio.toml").write_text(
        'rooms=["4"]\n[[slots]]\nstart="00:00"\nend="06:00"\n', encoding="utf8"
    )
    (tmp_path / ".env").write_text(
        "BOT_TOKEN='12345:"
        + "x" * 35
        + "'\nADMIN_IDS=''\nADMIN_PASSWORD_HASH='"
        + hash_password("correct-password")
        + "'\n",
        encoding="utf8",
    )
    settings = Settings.load(tmp_path)
    assert not settings.admin_ids
    assert settings.bot_token.startswith("12345:")
