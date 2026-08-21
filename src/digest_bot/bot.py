from __future__ import annotations

import logging
import re
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
from .service import DeliveryService
from .storage import Storage

LOGGER = logging.getLogger(__name__)
CHANNEL, REPOSITORY, PATH, REF, TOKEN_ENV, TIMEZONE, SEND_TIME, CONFIRM = range(8)
TOKEN_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEEP_VALUE = "Оставить без изменений"


def _storage(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["storage"]


def _is_editing(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "original" in context.user_data.get("setup", {})


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
    value = context.user_data["setup"][key]
    displayed = value if value is not None else "публичный доступ"
    return f"\n\nТекущее значение: {displayed}"


def _entered_value(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> Any:
    text = update.effective_message.text.strip()
    if _is_editing(context) and text == KEEP_VALUE:
        return context.user_data["setup"][key]
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
        "/preview ID — показать сообщение за подходящую дату\n"
        "/delete ID — удалить рассылку\n"
        "/cancel — отменить текущую настройку\n\n"
        "Допустимое время: 00:00–13:59 (дайджест за вчера) или "
        "20:00–23:59 (за сегодня)."
    )


async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _authorized(update, context):
        return ConversationHandler.END
    context.user_data["setup"] = {}
    await update.effective_message.reply_text(
        "Выберите канал доставки. Пока реализован только Telegram; email и другие "
        "каналы запланированы.",
        reply_markup=ReplyKeyboardMarkup(
            [["Telegram"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return CHANNEL


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    context.user_data["setup"] = {
        "channel": subscription.channel,
        "repository": subscription.repository,
        "digest_path": subscription.digest_path,
        "ref": subscription.ref,
        "token_env": subscription.token_env,
        "timezone": subscription.timezone,
        "send_time": subscription.send_time,
        "original": subscription,
    }
    await update.effective_message.reply_text(
        "Укажите новый репозиторий: GitHub owner/name либо полный HTTPS URL "
        "GitHub/GitLab."
        + _current_value(context, "repository"),
        reply_markup=_step_markup(context),
    )
    return REPOSITORY


async def channel_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message.text.casefold() != "telegram":
        await update.effective_message.reply_text(
            "Сейчас доступен только Telegram. Выберите Telegram."
        )
        return CHANNEL
    context.user_data["setup"]["channel"] = "telegram"
    await update.effective_message.reply_text(
        "Укажите репозиторий: GitHub owner/name либо полный HTTPS URL GitHub/GitLab.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REPOSITORY


async def repository_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        settings: Settings = context.application.bot_data["settings"]
        repository = normalize_repository(
            _entered_value(update, context, "repository"), settings.gitlab_allowed_hosts
        )
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return REPOSITORY
    context.user_data["setup"]["repository"] = repository
    await update.effective_message.reply_text(
        "Укажите путь к дайджесту внутри репозитория.\n\n"
        "Пример:\n"
        "docs/project-digest.md"
        + _current_value(context, "digest_path"),
        reply_markup=_step_markup(context),
    )
    return PATH


async def path_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        digest_path = normalize_digest_path(_entered_value(update, context, "digest_path"))
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return PATH
    context.user_data["setup"]["digest_path"] = digest_path
    await update.effective_message.reply_text(
        "Выберите ветку main или введите другой Git ref."
        + _current_value(context, "ref"),
        reply_markup=_step_markup(context, "main"),
    )
    return REF


async def ref_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ref = _entered_value(update, context, "ref")
    if ref == "-":
        ref = "main"
    if not ref or any(character.isspace() for character in ref):
        await update.effective_message.reply_text(
            "Git ref не должен быть пустым или содержать пробелы."
        )
        return REF
    context.user_data["setup"]["ref"] = ref
    repository = context.user_data["setup"]["repository"]
    suggested_token = "GITLAB_TOKEN" if "://" in repository else "GITHUB_TOKEN"
    await update.effective_message.reply_text(
        "Если репозиторий приватный, укажите имя переменной окружения с токеном "
        "или выберите предложенный вариант. Сам токен в Telegram не отправляйте."
        + _current_value(context, "token_env"),
        reply_markup=_step_markup(context, suggested_token, "Публичный репозиторий"),
    )
    return TOKEN_ENV


async def token_env_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = _entered_value(update, context, "token_env")
    if value is None:
        token_env = None
    else:
        value = value.strip()
        token_env = None if value in {"-", "Публичный репозиторий"} else value
    if token_env and not TOKEN_ENV_PATTERN.fullmatch(token_env):
        await update.effective_message.reply_text("Это не похоже на имя переменной окружения.")
        return TOKEN_ENV
    context.user_data["setup"]["token_env"] = token_env
    settings: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        "Выберите часовой пояс по умолчанию или введите другое имя IANA, "
        "например Europe/Moscow."
        + _current_value(context, "timezone"),
        reply_markup=_step_markup(context, settings.default_timezone),
    )
    return TIMEZONE


async def timezone_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.application.bot_data["settings"]
    value = _entered_value(update, context, "timezone")
    if value == "-":
        value = settings.default_timezone
    try:
        timezone = validate_timezone(value)
    except InvalidSchedule as exc:
        await update.effective_message.reply_text(str(exc))
        return TIMEZONE
    context.user_data["setup"]["timezone"] = timezone
    await update.effective_message.reply_text(
        "Укажите время отправки ЧЧ:ММ. Доступно 00:00–13:59 (за вчера) или "
        "20:00–23:59 (за сегодня); с 14:00 до 20:00 рассылка не выполняется.\n\n"
        "Команда /preview сможет проверить сообщение в любое время."
        + _current_value(context, "send_time"),
        reply_markup=_step_markup(context),
    )
    return SEND_TIME


async def send_time_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        send_time = validate_send_time(_entered_value(update, context, "send_time"))
    except InvalidSchedule as exc:
        await update.effective_message.reply_text(str(exc))
        return SEND_TIME
    setup: dict[str, Any] = context.user_data["setup"]
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
    return CONFIRM


async def confirm_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.effective_message.text.strip().casefold()
    if answer not in {"да", "нет"}:
        await update.effective_message.reply_text("Ответьте Да или Нет.")
        return CONFIRM
    if answer == "нет":
        context.user_data.pop("setup", None)
        await update.effective_message.reply_text(
            "Настройка отменена.", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    setup = context.user_data.pop("setup")
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
            await update.effective_message.reply_text(
                "Рассылка больше не найдена.", reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END
        result = f"Рассылка #{saved.id} обновлена."
    await update.effective_message.reply_text(
        f"{result} Проверить источник можно командой /preview {saved.id}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("setup", None)
    await update.effective_message.reply_text(
        "Настройка отменена.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    items = _storage(context).list_subscriptions(target=str(update.effective_chat.id))
    if not items:
        await update.effective_message.reply_text("В этом чате пока нет рассылок.")
        return
    lines = [
        f"#{item.id} · {item.repository}/{item.digest_path} · "
        f"{item.send_time} {item.timezone} · {item.channel}"
        for item in items
    ]
    await update.effective_message.reply_text("Рассылки:\n" + "\n".join(lines))


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
        )
        application.bot_data.update(
            {"settings": settings, "storage": storage, "source": source, "service": service}
        )
        await application.bot.set_my_commands(
            [
                BotCommand("setup", "добавить рассылку"),
                BotCommand("edit", "изменить рассылку"),
                BotCommand("list", "показать рассылки"),
                BotCommand("preview", "проверить сообщение"),
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
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("preview", preview_command))
    application.add_error_handler(error_handler)
    return application
