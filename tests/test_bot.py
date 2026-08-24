from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import ConversationHandler

from digest_bot.bot import (
    CONFIRM,
    KEEP_VALUE,
    PATH,
    REF,
    REPOSITORY,
    SEND_TIME,
    TEXT_ACTION,
    TEXT_DELETE,
    TEXT_MENU,
    TEXT_TODAY,
    TEXT_WEIGHT,
    TEXT_YESTERDAY,
    TIMEZONE,
    TOKEN_ENV,
    cancel,
    channel_step,
    confirm_step,
    edit_start,
    list_command,
    path_step,
    pause_command,
    ref_step,
    repository_step,
    resume_command,
    send_time_step,
    setup_start,
    stats_command,
    text_action_step,
    text_delete_step,
    text_menu_step,
    text_today_step,
    text_weight_step,
    text_yesterday_step,
    texts_start,
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


class FakeGroupChat:
    id = 123
    type = "group"

    async def get_member(self, user_id: int) -> SimpleNamespace:
        assert user_id == 42
        return SimpleNamespace(status="member")


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
        chat_data={},
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
    assert "setup" not in context.chat_data


async def test_edit_requires_exactly_one_numeric_id(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    update = make_update()
    context = make_context(storage)

    assert await edit_start(update, context) == ConversationHandler.END
    assert update.effective_message.replies[-1][0] == "Использование: /edit ID"


async def test_pause_and_resume_subscription(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()

    await pause_command(update, make_context(storage, str(saved.id)))
    paused = storage.get_subscription(saved.id, saved.target)
    assert paused is not None
    assert paused.active is False
    assert update.effective_message.replies[-1][0] == "Рассылка #1 приостановлена."

    await resume_command(update, make_context(storage, str(saved.id)))
    resumed = storage.get_subscription(saved.id, saved.target)
    assert resumed is not None
    assert resumed.active is True
    assert update.effective_message.replies[-1][0] == "Рассылка #1 возобновлена."


@pytest.mark.parametrize(
    ("handler", "command"),
    [(pause_command, "pause"), (resume_command, "resume")],
)
@pytest.mark.parametrize("args", [(), ("not-an-id",), ("1", "2")])
async def test_active_state_commands_require_exactly_one_numeric_id(
    tmp_path,
    handler,
    command,
    args,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    update = make_update()

    await handler(update, make_context(storage, *args))

    assert update.effective_message.replies[-1][0] == f"Использование: /{command} ID"


async def test_pause_does_not_reveal_subscription_from_another_chat(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    update.effective_chat.id = 999

    await pause_command(update, make_context(storage, str(saved.id)))

    assert update.effective_message.replies[-1][0] == "Рассылка не найдена."
    assert storage.get_subscription(saved.id, saved.target).active is True


async def test_list_shows_active_and_paused_statuses(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    active = saved_subscription(storage)
    paused = saved_subscription(storage)
    assert active.id is not None
    assert paused.id is not None
    storage.set_subscription_active(paused.id, paused.target, False)
    update = make_update()

    await list_command(update, make_context(storage))

    response = update.effective_message.replies[-1][0]
    assert f"#{active.id} · активна ·" in response
    assert f"#{paused.id} · приостановлена ·" in response


async def test_stats_is_scoped_to_current_chat(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    now = datetime(2026, 8, 20, 9, tzinfo=UTC)
    claim = storage.claim_delivery(saved.id, "2026-08-19", now)
    assert claim is not None
    assert storage.complete_delivery(claim, now)
    update = make_update()

    await stats_command(update, make_context(storage, str(saved.id)))

    response = update.effective_message.replies[-1][0]
    assert "Статистика рассылки #1" in response
    assert "успешно: 1" in response
    assert "Попыток доставки: 1" in response

    update.effective_chat.id = 999
    await stats_command(update, make_context(storage, str(saved.id)))
    assert update.effective_message.replies[-1][0] == "Рассылка не найдена."


@pytest.mark.parametrize("args", [("bad",), ("1", "2")])
async def test_stats_rejects_invalid_arguments(tmp_path, args) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    update = make_update()

    await stats_command(update, make_context(storage, *args))

    assert update.effective_message.replies[-1][0] == "Использование: /stats [ID]"


async def test_texts_dialog_adds_and_deletes_variant(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    context = make_context(storage, str(saved.id))

    assert await texts_start(update, context) == TEXT_MENU
    update.effective_message.text = "Вступление"
    assert await text_menu_step(update, context) == TEXT_ACTION
    update.effective_message.text = "Добавить вариант"
    assert await text_action_step(update, context) == TEXT_TODAY
    update.effective_message.text = "Сегодня есть новости"
    assert await text_today_step(update, context) == TEXT_YESTERDAY
    update.effective_message.text = "Вчера были новости"
    assert await text_yesterday_step(update, context) == TEXT_WEIGHT
    update.effective_message.text = "5"
    assert await text_weight_step(update, context) == TEXT_ACTION

    assert storage.list_message_variants(saved.id, target=saved.target) == []
    update.effective_message.text = "Назад"
    assert await text_action_step(update, context) == TEXT_MENU
    update.effective_message.text = "Сохранить"
    assert await text_menu_step(update, context) == ConversationHandler.END
    variants = storage.list_message_variants(saved.id, target=saved.target)
    assert variants is not None
    assert len(variants) == 1
    assert variants[0].today_text == "Сегодня есть новости"
    variant_id = variants[0].id
    assert variant_id is not None

    context = make_context(storage, str(saved.id))
    assert await texts_start(update, context) == TEXT_MENU
    update.effective_message.text = "Вступление"
    assert await text_menu_step(update, context) == TEXT_ACTION
    update.effective_message.text = "Удалить вариант"
    assert await text_action_step(update, context) == TEXT_DELETE
    update.effective_message.text = str(variant_id)
    assert await text_delete_step(update, context) == TEXT_ACTION
    update.effective_message.text = "Назад"
    assert await text_action_step(update, context) == TEXT_MENU
    update.effective_message.text = "Сохранить"
    assert await text_menu_step(update, context) == ConversationHandler.END
    assert storage.list_message_variants(saved.id, target=saved.target) == []


async def test_texts_dialog_can_delete_unsaved_variant(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    context = make_context(storage, str(saved.id))

    assert await texts_start(update, context) == TEXT_MENU
    update.effective_message.text = "Вступление"
    assert await text_menu_step(update, context) == TEXT_ACTION
    update.effective_message.text = "Добавить вариант"
    assert await text_action_step(update, context) == TEXT_TODAY
    update.effective_message.text = "Сегодня"
    assert await text_today_step(update, context) == TEXT_YESTERDAY
    update.effective_message.text = "Вчера"
    assert await text_yesterday_step(update, context) == TEXT_WEIGHT
    update.effective_message.text = "1"
    assert await text_weight_step(update, context) == TEXT_ACTION
    update.effective_message.text = "Удалить вариант"
    assert await text_action_step(update, context) == TEXT_DELETE
    update.effective_message.text = "-1"
    assert await text_delete_step(update, context) == TEXT_ACTION

    assert context.chat_data["texts"]["draft"] == []


async def test_texts_cancel_discards_draft(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    context = make_context(storage, str(saved.id))
    assert await texts_start(update, context) == TEXT_MENU
    update.effective_message.text = "Вступление"
    assert await text_menu_step(update, context) == TEXT_ACTION
    update.effective_message.text = "Добавить вариант"
    assert await text_action_step(update, context) == TEXT_TODAY
    update.effective_message.text = "Сегодня"
    assert await text_today_step(update, context) == TEXT_YESTERDAY
    update.effective_message.text = "Вчера"
    assert await text_yesterday_step(update, context) == TEXT_WEIGHT
    update.effective_message.text = "1"
    assert await text_weight_step(update, context) == TEXT_ACTION

    assert await cancel(update, context) == ConversationHandler.END
    assert storage.list_message_variants(saved.id, target=saved.target) == []


async def test_texts_dialog_does_not_reveal_foreign_subscription(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    update.effective_chat.id = 999

    result = await texts_start(update, make_context(storage, str(saved.id)))

    assert result == ConversationHandler.END
    assert update.effective_message.replies[-1][0] == "Рассылка не найдена."


async def test_setup_and_texts_dialogs_cannot_overlap(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    context = make_context(storage)

    assert await setup_start(update, context) == 0
    context.args = [str(saved.id)]
    assert await texts_start(update, context) == 0
    assert "Сначала завершите текущую настройку" in update.effective_message.replies[-1][0]

    update.effective_message.text = "/cancel"
    assert await cancel(update, context) == ConversationHandler.END
    assert await texts_start(update, context) == TEXT_MENU
    assert await setup_start(update, context) == TEXT_MENU
    assert "Сначала завершите настройку текстов" in update.effective_message.replies[-1][0]
    assert await cancel(update, context) == ConversationHandler.END
    assert "workflow" not in context.chat_data
    assert "texts" not in context.chat_data


async def test_rejected_texts_reentry_preserves_current_setup_step(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    context = make_context(storage)

    assert await setup_start(update, context) == 0
    update.effective_message.text = "Telegram"
    assert await channel_step(update, context) == REPOSITORY
    context.args = [str(saved.id)]
    assert await texts_start(update, context) == REPOSITORY
    update.effective_message.text = "owner/another-repo"
    assert await repository_step(update, context) == PATH


async def test_setup_drafts_are_isolated_between_chats_for_same_user(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    first_update = make_update()
    second_update = make_update()
    second_update.effective_chat.id = 999
    shared_user_data: dict[str, object] = {}
    first_context = make_context(storage)
    second_context = make_context(storage)
    first_context.user_data = shared_user_data
    second_context.user_data = shared_user_data

    assert await setup_start(first_update, first_context) == 0
    assert await setup_start(second_update, second_context) == 0
    first_update.effective_message.text = "Telegram"
    second_update.effective_message.text = "Telegram"
    assert await channel_step(first_update, first_context) == REPOSITORY
    assert await channel_step(second_update, second_context) == REPOSITORY
    first_update.effective_message.text = "first/repo"
    second_update.effective_message.text = "second/repo"
    assert await repository_step(first_update, first_context) == PATH
    assert await repository_step(second_update, second_context) == PATH

    assert first_context.chat_data["setup"]["repository"] == "first/repo"
    assert second_context.chat_data["setup"]["repository"] == "second/repo"
    assert shared_user_data == {}


async def test_edit_save_rechecks_authorization(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    context = make_context(storage, str(saved.id))

    assert await edit_start(update, context) == REPOSITORY
    update.effective_message.text = KEEP_VALUE
    assert await repository_step(update, context) == PATH
    update.effective_message.text = "docs/forbidden.md"
    assert await path_step(update, context) == REF
    update.effective_message.text = KEEP_VALUE
    assert await ref_step(update, context) == TOKEN_ENV
    update.effective_message.text = KEEP_VALUE
    assert await token_env_step(update, context) == TIMEZONE
    update.effective_message.text = KEEP_VALUE
    assert await timezone_step(update, context) == SEND_TIME
    update.effective_message.text = KEEP_VALUE
    assert await send_time_step(update, context) == CONFIRM
    context.application.bot_data["settings"] = replace(
        context.application.bot_data["settings"], admin_user_ids=frozenset({99})
    )
    update.effective_message.text = "Да"

    assert await confirm_step(update, context) == ConversationHandler.END
    unchanged = storage.get_subscription(saved.id, saved.target)
    assert unchanged is not None
    assert unchanged.digest_path == saved.digest_path
    assert "setup" not in context.chat_data
    assert "workflow" not in context.chat_data


async def test_texts_save_rechecks_authorization(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    context = make_context(storage, str(saved.id))
    assert await texts_start(update, context) == TEXT_MENU
    context.chat_data["texts"]["draft"] = [
        {
            "id": -1,
            "kind": "intro",
            "today_text": "Сегодня",
            "yesterday_text": "Вчера",
            "weight": 1,
        }
    ]
    context.application.bot_data["settings"] = replace(
        context.application.bot_data["settings"], admin_user_ids=frozenset({99})
    )
    update.effective_message.text = "Сохранить"

    assert await text_menu_step(update, context) == ConversationHandler.END
    assert storage.list_message_variants(saved.id, target=saved.target) == []


async def test_texts_reentry_keeps_existing_draft(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    first = saved_subscription(storage)
    second = saved_subscription(storage)
    assert first.id is not None and second.id is not None
    update = make_update()
    context = make_context(storage, str(first.id))

    assert await texts_start(update, context) == TEXT_MENU
    context.chat_data["texts"]["draft"].append(
        {
            "id": -1,
            "kind": "intro",
            "today_text": "Черновик",
            "yesterday_text": "Черновик",
            "weight": 1,
        }
    )
    context.args = [str(second.id)]

    assert await texts_start(update, context) == TEXT_MENU
    assert context.chat_data["texts"]["subscription_id"] == first.id
    assert context.chat_data["texts"]["draft"][0]["today_text"] == "Черновик"


async def test_other_admin_cannot_overwrite_or_cancel_texts_draft(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    owner_update = make_update()
    context = make_context(storage, str(saved.id))
    context.application.bot_data["settings"] = replace(
        context.application.bot_data["settings"], admin_user_ids=frozenset({42, 43})
    )
    assert await texts_start(owner_update, context) == TEXT_MENU
    original_draft = context.chat_data["texts"]

    other_update = make_update()
    other_update.effective_user.id = 43
    assert await texts_start(other_update, context) == ConversationHandler.END
    assert context.chat_data["texts"] is original_draft
    assert await cancel(other_update, context) == ConversationHandler.END
    assert context.chat_data["texts"] is original_draft


async def test_other_admin_can_take_over_expired_workflow(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    owner_update = make_update()
    context = make_context(storage, str(saved.id))
    context.application.bot_data["settings"] = replace(
        context.application.bot_data["settings"], admin_user_ids=frozenset({42, 43})
    )
    assert await texts_start(owner_update, context) == TEXT_MENU
    context.chat_data["texts"]["draft"].append(
        {
            "id": -1,
            "kind": "intro",
            "today_text": "Брошенный черновик",
            "yesterday_text": "Брошенный черновик",
            "weight": 1,
        }
    )
    context.chat_data["workflow"]["touched_at"] -= 31 * 60

    other_update = make_update()
    other_update.effective_user.id = 43
    assert await texts_start(other_update, context) == TEXT_MENU
    assert context.chat_data["workflow"]["owner_user_id"] == 43
    assert context.chat_data["texts"]["draft"] == []


async def test_pause_requires_configured_admin_access(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    update.effective_user.id = 99

    await pause_command(update, make_context(storage, str(saved.id)))

    assert update.effective_message.replies[-1][0] == (
        "У вас нет доступа к настройке этого бота."
    )
    assert storage.get_subscription(saved.id, saved.target).active is True


async def test_pause_requires_group_admin_access(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    saved = saved_subscription(storage)
    assert saved.id is not None
    update = make_update()
    update.effective_chat = FakeGroupChat()

    await pause_command(update, make_context(storage, str(saved.id)))

    assert update.effective_message.replies[-1][0] == (
        "Настраивать рассылку в группе может только администратор группы."
    )
    subscription = storage.get_subscription(saved.id, saved.target)
    assert subscription is not None
    assert subscription.active is True
