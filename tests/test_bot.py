from pathlib import Path
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from digest_bot.bot import (
    CONFIRM,
    KEEP_VALUE,
    PATH,
    REF,
    REPOSITORY,
    SEND_TIME,
    TIMEZONE,
    TOKEN_ENV,
    confirm_step,
    edit_start,
    path_step,
    ref_step,
    repository_step,
    send_time_step,
    timezone_step,
    token_env_step,
)
from digest_bot.config import Settings
from digest_bot.models import Subscription
from digest_bot.storage import Storage


class FakeMessage:
    def __init__(self) -> None:
        self.text = ""
        self.replies: list[tuple[str, object | None]] = []

    async def reply_text(self, text: str, reply_markup: object | None = None) -> None:
        self.replies.append((text, reply_markup))


def make_update() -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=FakeMessage(),
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_user=SimpleNamespace(id=42),
    )


def make_context(storage: Storage, *args: str) -> SimpleNamespace:
    settings = Settings(
        telegram_bot_token="test-token",
        telegram_proxy_url=None,
        gitlab_allowed_hosts=frozenset(),
        database_path=Path("unused.sqlite3"),
        admin_user_ids=frozenset({42}),
        default_timezone="UTC",
        scheduler_interval_seconds=30,
    )
    return SimpleNamespace(
        args=list(args),
        user_data={},
        application=SimpleNamespace(bot_data={"storage": storage, "settings": settings}),
    )


def saved_subscription(storage: Storage) -> Subscription:
    return storage.add_subscription(
        Subscription(
            id=None,
            channel="telegram",
            target="123",
            repository="owner/repo",
            digest_path="docs/project-digest.md",
            ref="main",
            token_env=None,
            timezone="UTC",
            send_time="09:00",
            created_by=42,
        )
    )


async def test_edit_keeps_saved_values_and_changes_selected_fields(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    update = make_update()
    context = make_context(storage, str(saved.id))

    assert await edit_start(update, context) == REPOSITORY
    assert "Текущее значение: owner/repo" in update.effective_message.replies[-1][0]

    update.effective_message.text = KEEP_VALUE
    assert await repository_step(update, context) == PATH
    update.effective_message.text = "docs/new-digest.md"
    assert await path_step(update, context) == REF
    update.effective_message.text = KEEP_VALUE
    assert await ref_step(update, context) == TOKEN_ENV
    update.effective_message.text = KEEP_VALUE
    assert await token_env_step(update, context) == TIMEZONE
    update.effective_message.text = KEEP_VALUE
    assert await timezone_step(update, context) == SEND_TIME
    update.effective_message.text = "22:00"
    assert await send_time_step(update, context) == CONFIRM
    update.effective_message.text = "Да"
    assert await confirm_step(update, context) == ConversationHandler.END

    assert saved.id is not None
    updated = storage.get_subscription(saved.id, saved.target)
    assert updated is not None
    assert updated.id == saved.id
    assert updated.channel == saved.channel
    assert updated.target == saved.target
    assert updated.created_by == saved.created_by
    assert updated.repository == saved.repository
    assert updated.digest_path == "docs/new-digest.md"
    assert updated.ref == saved.ref
    assert updated.token_env is None
    assert updated.timezone == saved.timezone
    assert updated.send_time == "22:00"
    assert len(storage.list_subscriptions(target=saved.target)) == 1
    assert "Рассылка #1 обновлена." in update.effective_message.replies[-1][0]


async def test_edit_does_not_reveal_subscription_from_another_chat(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    update = make_update()
    update.effective_chat.id = 999
    context = make_context(storage, str(saved.id))

    assert await edit_start(update, context) == ConversationHandler.END
    assert update.effective_message.replies[-1][0] == "Рассылка не найдена."
    assert "setup" not in context.user_data


async def test_edit_requires_exactly_one_numeric_id(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    update = make_update()
    context = make_context(storage)

    assert await edit_start(update, context) == ConversationHandler.END
    assert update.effective_message.replies[-1][0] == "Использование: /edit ID"
