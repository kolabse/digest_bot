from __future__ import annotations

import logging
import re
import time
from typing import Any

from telegram import BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .channels.telegram import TelegramChannel
from .config import Settings
from .github import RepositoryContentsSource, normalize_digest_path, normalize_repository
from .models import Subscription
from .schedule import InvalidSchedule, validate_send_time, validate_timezone
from .service import DeliveryService, RetryPolicy
from .storage import StaleMessageVariantsError, Storage

LOGGER = logging.getLogger(__name__)
CHANNEL, REPOSITORY, PATH, REF, TOKEN_ENV, TIMEZONE, SEND_TIME, CONFIRM = range(8)
TEXT_MENU, TEXT_ACTION, TEXT_TODAY, TEXT_YESTERDAY, TEXT_WEIGHT, TEXT_DELETE = range(
    8, 14
)
TOKEN_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEEP_VALUE = "Оставить без изменений"
WORKFLOW_LEASE_SECONDS = 30 * 60
TEXT_KIND_LABELS = {
    "Вступление": "intro",
    "Сообщение без изменений": "fallback",
    "Завершение: короткий дайджест": "outro_small",
    "Завершение: обычный дайджест": "outro_regular",
    "Завершение: большой дайджест": "outro_large",
}
TEXT_KIND_TITLES = {value: key for key, value in TEXT_KIND_LABELS.items()}


def _storage(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["storage"]


def _workflow_owned(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
) -> bool:
    workflow = context.chat_data.get("workflow")
    if workflow and _workflow_expired(workflow):
        _discard_workflow(context)
        return False
    user = update.effective_user
    owned = bool(
        workflow
        and user
        and workflow.get("kind") == kind
        and workflow.get("owner_user_id") == user.id
    )
    if owned:
        workflow["touched_at"] = time.monotonic()
    return owned


def _workflow_expired(workflow: dict[str, Any]) -> bool:
    touched_at = workflow.get("touched_at")
    return not isinstance(touched_at, (int, float)) or (
        time.monotonic() - touched_at > WORKFLOW_LEASE_SECONDS
    )


def _discard_workflow(context: ContextTypes.DEFAULT_TYPE) -> None:
    workflow = context.chat_data.pop("workflow", None)
    if workflow and workflow.get("kind") in {"setup", "texts"}:
        context.chat_data.pop(workflow["kind"], None)


def _workflow_state(context: ContextTypes.DEFAULT_TYPE) -> int:
    workflow = context.chat_data.get("workflow")
    state = workflow.get("state") if workflow else None
    return state if isinstance(state, int) else ConversationHandler.END


def _set_workflow_state(context: ContextTypes.DEFAULT_TYPE, state: int) -> int:
    workflow = context.chat_data.get("workflow")
    if workflow is not None:
        workflow["state"] = state
    return state


def _release_workflow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
) -> None:
    if _workflow_owned(update, context, kind):
        context.chat_data.pop("workflow", None)


async def _workflow_available(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
) -> bool:
    workflow = context.chat_data.get("workflow")
    if workflow is not None and _workflow_expired(workflow):
        _discard_workflow(context)
        workflow = None
    if workflow is None:
        return True
    if update.effective_user and workflow.get("owner_user_id") == update.effective_user.id:
        workflow["touched_at"] = time.monotonic()
        return True
    await update.effective_message.reply_text(
        "В этом чате другой администратор уже выполняет настройку. "
        "Дождитесь её завершения."
    )
    return False


def _is_editing(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "original" in context.chat_data.get("setup", {})


def _step_markup(
    context: ContextTypes.DEFAULT_TYPE, *choices: str
) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    rows = [[KEEP_VALUE]] if _is_editing(context) else []
    rows.extend([[choice] for choice in choices])
    return (
        ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
        if rows
        else ReplyKeyboardRemove()
    )


def _current_value(context: ContextTypes.DEFAULT_TYPE, key: str) -> str:
    if not _is_editing(context):
        return ""
    value = context.chat_data["setup"][key]
    displayed = value if value is not None else "публичный доступ"
    return f"\n\nТекущее значение: {displayed}"


def _entered_value(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> Any:
    text = update.effective_message.text.strip()
    if _is_editing(context) and text == KEEP_VALUE:
        return context.chat_data["setup"][key]
    return text


async def _authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    settings: Settings = context.application.bot_data["settings"]
    if user is None or chat is None:
        return False
    if settings.admin_user_ids and user.id not in settings.admin_user_ids:
        await update.effective_message.reply_text("У вас нет доступа к настройке этого бота.")
        return False
    if chat.type in {"group", "supergroup"}:
        member = await chat.get_member(user.id)
        if member.status not in {"administrator", "creator"}:
            await update.effective_message.reply_text(
                "Настраивать рассылку в группе может только администратор группы."
            )
            return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Я отправляю ежедневный дайджест проекта из GitHub или GitLab по расписанию.\n\n"
        "Настройте рассылку командой /setup. В личном диалоге адресатом будете вы; "
        "в группе — текущая группа. Изменить сохранённую рассылку можно командой "
        "/edit ID. Команда /help покажет остальные возможности."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "/setup — добавить рассылку\n"
        "/edit ID — изменить рассылку\n"
        "/list — показать рассылки текущего чата\n"
        "/pause ID — приостановить рассылку\n"
        "/resume ID — возобновить рассылку\n"
        "/preview ID — показать сообщение за подходящую дату\n"
        "/stats [ID] — показать статистику доставок\n"
        "/texts ID — настроить обрамляющие тексты\n"
        "/delete ID — удалить рассылку\n"
        "/cancel — отменить текущую настройку\n\n"
        "Допустимое время: 00:00–13:59 (дайджест за вчера) или "
        "20:00–23:59 (за сегодня)."
    )


async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _workflow_available(update, context, "setup"):
        return ConversationHandler.END
    workflow = context.chat_data.get("workflow")
    if workflow is not None and workflow.get("kind") != "setup":
        await update.effective_message.reply_text(
            "Сначала завершите настройку текстов или отправьте /cancel."
        )
        return _workflow_state(context)
    if not await _authorized(update, context):
        return ConversationHandler.END
    context.chat_data["workflow"] = {
        "kind": "setup",
        "owner_user_id": update.effective_user.id,
        "touched_at": time.monotonic(),
        "state": CHANNEL,
    }
    context.chat_data["setup"] = {}
    await update.effective_message.reply_text(
        "Выберите канал доставки. Пока реализован только Telegram; email и другие "
        "каналы запланированы.",
        reply_markup=ReplyKeyboardMarkup(
            [["Telegram"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return CHANNEL


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _workflow_available(update, context, "setup"):
        return ConversationHandler.END
    workflow = context.chat_data.get("workflow")
    if workflow is not None and workflow.get("kind") != "setup":
        await update.effective_message.reply_text(
            "Сначала завершите настройку текстов или отправьте /cancel."
        )
        return _workflow_state(context)
    if not await _authorized(update, context):
        return ConversationHandler.END
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Использование: /edit ID")
        return ConversationHandler.END
    target = str(update.effective_chat.id)
    subscription = _storage(context).get_subscription(int(context.args[0]), target)
    if subscription is None:
        await update.effective_message.reply_text("Рассылка не найдена.")
        return ConversationHandler.END
    context.chat_data["setup"] = {
        "channel": subscription.channel,
        "repository": subscription.repository,
        "digest_path": subscription.digest_path,
        "ref": subscription.ref,
        "token_env": subscription.token_env,
        "timezone": subscription.timezone,
        "send_time": subscription.send_time,
        "original": subscription,
    }
    context.chat_data["workflow"] = {
        "kind": "setup",
        "owner_user_id": update.effective_user.id,
        "touched_at": time.monotonic(),
        "state": REPOSITORY,
    }
    await update.effective_message.reply_text(
        "Укажите новый репозиторий: GitHub owner/name либо полный HTTPS URL "
        "GitHub/GitLab."
        + _current_value(context, "repository"),
        reply_markup=_step_markup(context),
    )
    return _set_workflow_state(context, REPOSITORY)


async def channel_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        return ConversationHandler.END
    if update.effective_message.text.casefold() != "telegram":
        await update.effective_message.reply_text(
            "Сейчас доступен только Telegram. Выберите Telegram."
        )
        return CHANNEL
    context.chat_data["setup"]["channel"] = "telegram"
    await update.effective_message.reply_text(
        "Укажите репозиторий: GitHub owner/name либо полный HTTPS URL GitHub/GitLab.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return _set_workflow_state(context, REPOSITORY)


async def repository_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        return ConversationHandler.END
    try:
        settings: Settings = context.application.bot_data["settings"]
        repository = normalize_repository(
            _entered_value(update, context, "repository"), settings.gitlab_allowed_hosts
        )
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return REPOSITORY
    context.chat_data["setup"]["repository"] = repository
    await update.effective_message.reply_text(
        "Укажите путь к дайджесту внутри репозитория.\n\n"
        "Пример:\n"
        "docs/project-digest.md"
        + _current_value(context, "digest_path"),
        reply_markup=_step_markup(context),
    )
    return _set_workflow_state(context, PATH)


async def path_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        return ConversationHandler.END
    try:
        digest_path = normalize_digest_path(_entered_value(update, context, "digest_path"))
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return PATH
    context.chat_data["setup"]["digest_path"] = digest_path
    await update.effective_message.reply_text(
        "Выберите ветку main или введите другой Git ref."
        + _current_value(context, "ref"),
        reply_markup=_step_markup(context, "main"),
    )
    return _set_workflow_state(context, REF)


async def ref_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        return ConversationHandler.END
    ref = _entered_value(update, context, "ref")
    if ref == "-":
        ref = "main"
    if not ref or any(character.isspace() for character in ref):
        await update.effective_message.reply_text(
            "Git ref не должен быть пустым или содержать пробелы."
        )
        return REF
    context.chat_data["setup"]["ref"] = ref
    repository = context.chat_data["setup"]["repository"]
    suggested_token = "GITLAB_TOKEN" if "://" in repository else "GITHUB_TOKEN"
    await update.effective_message.reply_text(
        "Если репозиторий приватный, укажите имя переменной окружения с токеном "
        "или выберите предложенный вариант. Сам токен в Telegram не отправляйте."
        + _current_value(context, "token_env"),
        reply_markup=_step_markup(context, suggested_token, "Публичный репозиторий"),
    )
    return _set_workflow_state(context, TOKEN_ENV)


async def token_env_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        return ConversationHandler.END
    value = _entered_value(update, context, "token_env")
    if value is None:
        token_env = None
    else:
        value = value.strip()
        token_env = None if value in {"-", "Публичный репозиторий"} else value
    if token_env and not TOKEN_ENV_PATTERN.fullmatch(token_env):
        await update.effective_message.reply_text("Это не похоже на имя переменной окружения.")
        return TOKEN_ENV
    context.chat_data["setup"]["token_env"] = token_env
    settings: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        "Выберите часовой пояс по умолчанию или введите другое имя IANA, "
        "например Europe/Moscow."
        + _current_value(context, "timezone"),
        reply_markup=_step_markup(context, settings.default_timezone),
    )
    return _set_workflow_state(context, TIMEZONE)


async def timezone_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        return ConversationHandler.END
    settings: Settings = context.application.bot_data["settings"]
    value = _entered_value(update, context, "timezone")
    if value == "-":
        value = settings.default_timezone
    try:
        timezone = validate_timezone(value)
    except InvalidSchedule as exc:
        await update.effective_message.reply_text(str(exc))
        return TIMEZONE
    context.chat_data["setup"]["timezone"] = timezone
    await update.effective_message.reply_text(
        "Укажите время отправки ЧЧ:ММ. Доступно 00:00–13:59 (за вчера) или "
        "20:00–23:59 (за сегодня); с 14:00 до 20:00 рассылка не выполняется.\n\n"
        "Команда /preview сможет проверить сообщение в любое время."
        + _current_value(context, "send_time"),
        reply_markup=_step_markup(context),
    )
    return _set_workflow_state(context, SEND_TIME)


async def send_time_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        return ConversationHandler.END
    try:
        send_time = validate_send_time(_entered_value(update, context, "send_time"))
    except InvalidSchedule as exc:
        await update.effective_message.reply_text(str(exc))
        return SEND_TIME
    setup: dict[str, Any] = context.chat_data["setup"]
    setup["send_time"] = send_time
    access = setup["token_env"] or "публичный доступ"
    await update.effective_message.reply_text(
        "Проверьте настройку:\n\n"
        f"• Канал: Telegram, текущий чат\n"
        f"• Репозиторий: {setup['repository']}\n"
        f"• Файл: {setup['digest_path']}\n"
        f"• Ref: {setup['ref']}\n"
        f"• Доступ: {access}\n"
        f"• Расписание: {send_time}, {setup['timezone']}\n\n"
        "Сохранить?",
        reply_markup=ReplyKeyboardMarkup(
            [["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return _set_workflow_state(context, CONFIRM)


async def confirm_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "setup"):
        await update.effective_message.reply_text(
            "Эта настройка принадлежит другому администратору."
        )
        return ConversationHandler.END
    answer = update.effective_message.text.strip().casefold()
    if answer not in {"да", "нет"}:
        await update.effective_message.reply_text("Ответьте Да или Нет.")
        return CONFIRM
    if answer == "нет":
        context.chat_data.pop("setup", None)
        _release_workflow(update, context, "setup")
        await update.effective_message.reply_text(
            "Настройка отменена.", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    if not await _authorized(update, context):
        context.chat_data.pop("setup", None)
        _release_workflow(update, context, "setup")
        return ConversationHandler.END
    setup = context.chat_data.pop("setup")
    original: Subscription | None = setup.get("original")
    if original is None:
        saved = _storage(context).add_subscription(
            Subscription(
                id=None,
                channel=setup["channel"],
                target=str(update.effective_chat.id),
                repository=setup["repository"],
                digest_path=setup["digest_path"],
                ref=setup["ref"],
                token_env=setup["token_env"],
                timezone=setup["timezone"],
                send_time=setup["send_time"],
                created_by=update.effective_user.id,
            )
        )
        result = f"Рассылка #{saved.id} сохранена."
    else:
        saved = _storage(context).update_subscription(
            Subscription(
                id=original.id,
                channel=original.channel,
                target=original.target,
                repository=setup["repository"],
                digest_path=setup["digest_path"],
                ref=setup["ref"],
                token_env=setup["token_env"],
                timezone=setup["timezone"],
                send_time=setup["send_time"],
                created_by=original.created_by,
                active=original.active,
            )
        )
        if saved is None:
            _release_workflow(update, context, "setup")
            await update.effective_message.reply_text(
                "Рассылка больше не найдена.", reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END
        result = f"Рассылка #{saved.id} обновлена."
    _release_workflow(update, context, "setup")
    await update.effective_message.reply_text(
        f"{result} Проверить источник можно командой /preview {saved.id}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if _workflow_owned(update, context, "setup"):
        context.chat_data.pop("setup", None)
        _release_workflow(update, context, "setup")
    elif _workflow_owned(update, context, "texts"):
        context.chat_data.pop("texts", None)
        _release_workflow(update, context, "texts")
    else:
        await update.effective_message.reply_text(
            "Активная настройка принадлежит другому администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Настройка отменена.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


def _text_menu_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[label] for label in TEXT_KIND_LABELS]
        + [["Сбросить все тексты"], ["Сохранить"]],
        resize_keyboard=True,
    )


def _text_action_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Добавить вариант"],
            ["Удалить вариант"],
            ["Вернуть встроенные варианты"],
            ["Назад"],
        ],
        resize_keyboard=True,
    )


async def _show_text_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prefix: str | None = None,
) -> int:
    data = context.chat_data["texts"]
    variants = data["draft"]
    counts = {
        kind: sum(1 for item in variants if item["kind"] == kind)
        for kind in TEXT_KIND_TITLES
    }
    lines = [prefix] if prefix else []
    lines.append(f"Тексты рассылки #{data['subscription_id']}:")
    lines.extend(
        f"• {title}: {'встроенные' if counts[kind] == 0 else f'свои — {counts[kind]}'}"
        for kind, title in TEXT_KIND_TITLES.items()
    )
    lines.append("\nВыберите категорию.")
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=_text_menu_markup(),
    )
    return _set_workflow_state(context, TEXT_MENU)


async def _show_text_category(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prefix: str | None = None,
) -> int:
    data = context.chat_data["texts"]
    kind = data["kind"]
    current = [item for item in data["draft"] if item["kind"] == kind]
    lines = [prefix] if prefix else []
    lines.append(f"Категория «{TEXT_KIND_TITLES[kind]}».")
    if current:
        lines.append("Свои варианты заменяют встроенные:")
        lines.extend(
            f"#{item['id']} · вес {item['weight']} · {item['today_text'][:80]}"
            for item in current
        )
    else:
        lines.append("Используются встроенные варианты.")
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=_text_action_markup(),
    )
    return _set_workflow_state(context, TEXT_ACTION)


async def texts_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _workflow_available(update, context, "texts"):
        return ConversationHandler.END
    workflow = context.chat_data.get("workflow")
    if workflow is not None and workflow.get("kind") == "texts":
        await update.effective_message.reply_text(
            "В этом чате уже открыт черновик текстов. Сохраните его или отправьте /cancel."
        )
        return _workflow_state(context)
    if workflow is not None:
        await update.effective_message.reply_text(
            "Сначала завершите текущую настройку или отправьте /cancel."
        )
        return _workflow_state(context)
    if not await _authorized(update, context):
        return ConversationHandler.END
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Использование: /texts ID")
        return ConversationHandler.END
    subscription_id = int(context.args[0])
    target = str(update.effective_chat.id)
    if _storage(context).get_subscription(subscription_id, target) is None:
        await update.effective_message.reply_text("Рассылка не найдена.")
        return ConversationHandler.END
    variants = _storage(context).list_message_variants(subscription_id, target=target) or []
    context.chat_data["workflow"] = {
        "kind": "texts",
        "owner_user_id": update.effective_user.id,
        "touched_at": time.monotonic(),
        "state": TEXT_MENU,
    }
    context.chat_data["texts"] = {
        "subscription_id": subscription_id,
        "target": target,
        "draft": [
            {
                "id": item.id,
                "kind": item.kind,
                "today_text": item.today_text,
                "yesterday_text": item.yesterday_text,
                "weight": item.weight,
            }
            for item in variants
        ],
        "revision": _storage(context).message_variants_revision(variants),
        "next_temp_id": -1,
    }
    return await _show_text_menu(update, context)


async def text_menu_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    data = context.chat_data.get("texts")
    if data is None or not _workflow_owned(update, context, "texts"):
        return ConversationHandler.END
    if text == "Сохранить":
        if not await _authorized(update, context):
            context.chat_data.pop("texts", None)
            _release_workflow(update, context, "texts")
            return ConversationHandler.END
        current_target = str(update.effective_chat.id)
        if data["target"] != current_target:
            context.chat_data.pop("texts", None)
            _release_workflow(update, context, "texts")
            await update.effective_message.reply_text(
                "Контекст чата изменился. Настройка отменена.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END
        try:
            saved = _storage(context).replace_message_variants(
                data["subscription_id"],
                current_target,
                [
                    (
                        item["kind"],
                        item["today_text"],
                        item["yesterday_text"],
                        item["weight"],
                    )
                    for item in data["draft"]
                ],
                expected_revision=data["revision"],
            )
        except StaleMessageVariantsError:
            context.chat_data.pop("texts", None)
            _release_workflow(update, context, "texts")
            await update.effective_message.reply_text(
                "Тексты уже изменил другой администратор. Откройте /texts заново.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END
        context.chat_data.pop("texts", None)
        _release_workflow(update, context, "texts")
        if saved is None:
            await update.effective_message.reply_text(
                "Рассылка не найдена.", reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Настройка текстов сохранена.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    if text == "Сбросить все тексты":
        data["draft"].clear()
        return await _show_text_menu(update, context, "Все категории возвращены к встроенным.")
    kind = TEXT_KIND_LABELS.get(text)
    if kind is None:
        await update.effective_message.reply_text("Выберите категорию кнопкой ниже.")
        return TEXT_MENU
    data["kind"] = kind
    return await _show_text_category(update, context)


async def text_action_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    data = context.chat_data.get("texts")
    if data is None or not _workflow_owned(update, context, "texts"):
        return ConversationHandler.END
    if text == "Назад":
        data.pop("kind", None)
        return await _show_text_menu(update, context)
    if text == "Вернуть встроенные варианты":
        data["draft"] = [
            item for item in data["draft"] if item["kind"] != data["kind"]
        ]
        return await _show_text_category(
            update,
            context,
            "Категория возвращена к встроенным вариантам.",
        )
    if text == "Удалить вариант":
        await update.effective_message.reply_text(
            "Отправьте номер варианта без символа #.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return _set_workflow_state(context, TEXT_DELETE)
    if text == "Добавить вариант":
        if sum(1 for item in data["draft"] if item["kind"] == data["kind"]) >= 20:
            return await _show_text_category(
                update,
                context,
                "В категории уже максимальные 20 вариантов.",
            )
        await update.effective_message.reply_text(
            "Отправьте текст варианта для дайджеста за сегодня (1–500 символов).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return _set_workflow_state(context, TEXT_TODAY)
    await update.effective_message.reply_text("Выберите действие кнопкой ниже.")
    return TEXT_ACTION


def _valid_custom_text(text: str) -> bool:
    return 1 <= len(text.strip()) <= 500


async def text_today_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "texts"):
        return ConversationHandler.END
    text = update.effective_message.text.strip()
    if not _valid_custom_text(text):
        await update.effective_message.reply_text("Текст должен содержать от 1 до 500 символов.")
        return TEXT_TODAY
    context.chat_data["texts"]["today_text"] = text
    await update.effective_message.reply_text(
        "Теперь отправьте вариант для дайджеста за вчера (1–500 символов)."
    )
    return _set_workflow_state(context, TEXT_YESTERDAY)


async def text_yesterday_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "texts"):
        return ConversationHandler.END
    text = update.effective_message.text.strip()
    if not _valid_custom_text(text):
        await update.effective_message.reply_text("Текст должен содержать от 1 до 500 символов.")
        return TEXT_YESTERDAY
    context.chat_data["texts"]["yesterday_text"] = text
    await update.effective_message.reply_text(
        "Укажите вес варианта от 1 до 100. Чем больше вес, тем чаще выбирается вариант."
    )
    return _set_workflow_state(context, TEXT_WEIGHT)


async def text_weight_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "texts"):
        return ConversationHandler.END
    text = update.effective_message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 100:
        await update.effective_message.reply_text("Вес должен быть целым числом от 1 до 100.")
        return TEXT_WEIGHT
    data = context.chat_data["texts"]
    variant_id = data["next_temp_id"]
    data["next_temp_id"] -= 1
    data["draft"].append(
        {
            "id": variant_id,
            "kind": data["kind"],
            "today_text": data.pop("today_text"),
            "yesterday_text": data.pop("yesterday_text"),
            "weight": int(text),
        }
    )
    return await _show_text_category(
        update,
        context,
        f"Вариант #{variant_id} добавлен в черновик.",
    )


async def text_delete_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _workflow_owned(update, context, "texts"):
        return ConversationHandler.END
    text = update.effective_message.text.strip()
    try:
        variant_id = int(text)
    except ValueError:
        await update.effective_message.reply_text("Отправьте числовой номер варианта.")
        return TEXT_DELETE
    data = context.chat_data["texts"]
    original_count = len(data["draft"])
    data["draft"] = [
        item
        for item in data["draft"]
        if not (item["id"] == variant_id and item["kind"] == data["kind"])
    ]
    deleted = len(data["draft"]) < original_count
    message = "Вариант удалён." if deleted else "Вариант не найден в этой рассылке."
    return await _show_text_category(update, context, message)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    items = _storage(context).list_subscriptions(target=str(update.effective_chat.id))
    if not items:
        await update.effective_message.reply_text("В этом чате пока нет рассылок.")
        return
    lines = [
        f"#{item.id} · {'активна' if item.active else 'приостановлена'} · "
        f"{item.repository}/{item.digest_path} · "
        f"{item.send_time} {item.timezone} · {item.channel}"
        for item in items
    ]
    await update.effective_message.reply_text("Рассылки:\n" + "\n".join(lines))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    if len(context.args) > 1 or (context.args and not context.args[0].isdigit()):
        await update.effective_message.reply_text("Использование: /stats [ID]")
        return
    subscription_id = int(context.args[0]) if context.args else None
    stats = _storage(context).delivery_stats(
        str(update.effective_chat.id),
        subscription_id,
    )
    if stats is None:
        await update.effective_message.reply_text("Рассылка не найдена.")
        return
    title = (
        f"Статистика рассылки #{subscription_id}:"
        if subscription_id is not None
        else "Статистика доставок:"
    )
    if stats.total == 0:
        await update.effective_message.reply_text(
            f"{title}\nДля {'этой рассылки' if subscription_id else 'этого чата'} "
            "пока нет доставок."
        )
        return
    lines = [
        title,
        f"Всего: {stats.total} · успешно: {stats.sent} · провалено: {stats.failed}",
        f"Ожидают повтора: {stats.retrying} · выполняются: {stats.sending}",
        f"Попыток доставки: {stats.attempts}",
    ]
    if subscription_id is not None and stats.latest is not None:
        status_names = {
            "sent": "успешно",
            "failed": "провалено",
            "retrying": "ожидает повтора",
            "sending": "выполняется",
        }
        lines.append(
            f"Последняя доставка: {stats.latest.digest_date} — "
            f"{status_names[stats.latest.status]}, попыток: {stats.latest.attempt_count}."
        )
    await update.effective_message.reply_text("\n".join(lines))


async def _set_subscription_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    active: bool,
) -> None:
    if not await _authorized(update, context):
        return
    command = "resume" if active else "pause"
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text(f"Использование: /{command} ID")
        return
    subscription_id = int(context.args[0])
    subscription = _storage(context).set_subscription_active(
        subscription_id,
        str(update.effective_chat.id),
        active,
    )
    if subscription is None:
        await update.effective_message.reply_text("Рассылка не найдена.")
        return
    action = "возобновлена" if active else "приостановлена"
    await update.effective_message.reply_text(f"Рассылка #{subscription_id} {action}.")


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_subscription_active(update, context, active=False)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_subscription_active(update, context, active=True)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Использование: /delete ID")
        return
    deleted = _storage(context).delete_subscription(
        int(context.args[0]), str(update.effective_chat.id)
    )
    await update.effective_message.reply_text(
        "Рассылка удалена." if deleted else "Рассылка не найдена."
    )


async def preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Использование: /preview ID")
        return
    service: DeliveryService = context.application.bot_data["service"]
    try:
        message = await service.preview(int(context.args[0]), str(update.effective_chat.id))
    except (LookupError, ValueError, RuntimeError) as exc:
        await update.effective_message.reply_text(f"Не удалось подготовить сообщение: {exc}")
        return
    await TelegramChannel(context.bot).send(str(update.effective_chat.id), message)


async def scheduler_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    service: DeliveryService = context.application.bot_data["service"]
    await service.dispatch_due()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Unhandled Telegram update error", exc_info=context.error)


def build_application(settings: Settings) -> Application:
    storage = Storage(settings.database_path)
    storage.initialize()
    source = RepositoryContentsSource(
        allowed_gitlab_hosts=settings.gitlab_allowed_hosts
    )

    async def post_init(application: Application) -> None:
        service = DeliveryService(
            storage=storage,
            source=source,
            channels={"telegram": TelegramChannel(application.bot)},
            retry_policy=RetryPolicy(
                max_attempts=settings.delivery_max_attempts,
                initial_delay_seconds=settings.delivery_retry_initial_seconds,
                max_delay_seconds=settings.delivery_retry_max_seconds,
                claim_lease_seconds=settings.delivery_claim_lease_seconds,
            ),
            history_retention_days=settings.delivery_history_retention_days,
            cleanup_batch_size=settings.delivery_cleanup_batch_size,
        )
        application.bot_data.update(
            {"settings": settings, "storage": storage, "source": source, "service": service}
        )
        await application.bot.set_my_commands(
            [
                BotCommand("setup", "добавить рассылку"),
                BotCommand("edit", "изменить рассылку"),
                BotCommand("list", "показать рассылки"),
                BotCommand("pause", "приостановить рассылку"),
                BotCommand("resume", "возобновить рассылку"),
                BotCommand("preview", "проверить сообщение"),
                BotCommand("stats", "статистика доставок"),
                BotCommand("texts", "настроить тексты"),
                BotCommand("delete", "удалить рассылку"),
                BotCommand("help", "помощь"),
            ]
        )
        if application.job_queue is None:
            raise RuntimeError("JobQueue is unavailable; install the job-queue extra")
        application.job_queue.run_repeating(
            scheduler_callback,
            interval=settings.scheduler_interval_seconds,
            first=1,
            name="digest-scheduler",
        )

    async def post_shutdown(application: Application) -> None:
        await source.close()

    builder = ApplicationBuilder().token(settings.telegram_bot_token)
    if settings.telegram_proxy_url:
        builder = builder.proxy(settings.telegram_proxy_url).get_updates_proxy(
            settings.telegram_proxy_url
        )
    application = (
        builder.concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("setup", setup_start),
            CommandHandler("edit", edit_start),
            CommandHandler("texts", texts_start),
        ],
        states={
            CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, channel_step)],
            REPOSITORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, repository_step)],
            PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, path_step)],
            REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_step)],
            TOKEN_ENV: [MessageHandler(filters.TEXT & ~filters.COMMAND, token_env_step)],
            TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, timezone_step)],
            SEND_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_time_step)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_step)],
            TEXT_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, text_menu_step)],
            TEXT_ACTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_action_step)
            ],
            TEXT_TODAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_today_step)
            ],
            TEXT_YESTERDAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_yesterday_step)
            ],
            TEXT_WEIGHT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_weight_step)
            ],
            TEXT_DELETE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_delete_step)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("preview", preview_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_error_handler(error_handler)
    return application
