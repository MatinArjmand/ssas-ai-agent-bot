"""Telegram UI for the combined stateless SSAS MCP + AI application."""

from __future__ import annotations

import asyncio
from functools import wraps
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent import DatabaseAgent
from ..agent_service import AgentService
from ..ai_provider import load_ai_settings
from ..auth import Access, AuthStore, load_permissions
from ..config import load_catalog
from ..mcp_host import McpManager
from ..signup import SignupFlow, SignupSession
from .flow import BotFlow, Session

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TELEGRAM_MESSAGE_CHUNK = 3900
logger = logging.getLogger("ssas_telegram_bot")


def config_path(env_name: str, default_name: str) -> Path:
    path = Path(os.getenv(env_name, default_name))
    return path if path.is_absolute() else PROJECT_ROOT / path


def text_chunks(text: str):
    text = str(text).strip() or "The agent returned an empty answer."
    chunk, units = [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > TELEGRAM_MESSAGE_CHUNK:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(char)
        units += size
    if chunk:
        yield "".join(chunk)


def make_sender(update):
    async def send(text, choices=None):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = None
        if choices:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=data)] for label, data in choices
            ])
        chunks = list(text_chunks(text))
        for i, chunk in enumerate(chunks):
            await update.effective_message.reply_text(
                chunk, parse_mode=None, reply_markup=keyboard if i == len(chunks) - 1 else None,
            )
    return send


def session(context) -> Session:
    return context.user_data.setdefault("session", Session())


def signup_session(context) -> SignupSession:
    return context.user_data.setdefault("signup", SignupSession())


def flow(context) -> BotFlow:
    return context.application.bot_data["flow"]


def signup(context) -> SignupFlow:
    return context.application.bot_data["signup"]


def serialize_user(handler):
    @wraps(handler)
    async def wrapped(update, context):
        user = update.effective_user
        if user is None:
            return await handler(update, context)
        locks = context.application.bot_data.setdefault("user_locks", {})
        lock = locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            return await handler(update, context)
    return wrapped


async def private_chat(update) -> bool:
    if update.effective_message is None or update.effective_chat is None or update.effective_user is None:
        return False
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Please use this bot in a private Telegram chat.")
        return False
    return True


async def authorize(update, context) -> Access | None:
    if not await private_chat(update):
        return None
    send = make_sender(update)
    access = await signup(context).access(update.effective_user.id, send)
    if access and access.allowed:
        return access
    session(context).reset()
    if access is None:
        signup_session(context).reset()
        return None
    if access.account:
        signup_session(context).reset()
        await send("Your email is no longer approved to use the bot. Please contact the administrator.", None)
    elif signup_session(context).stage != "idle":
        await send("Finish your signup first, or use /start to begin again and /cancel to stop.", None)
    else:
        await signup(context).start(update.effective_user.id, signup_session(context), send)
    return None


@serialize_user
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    session(context).reset()
    send = make_sender(update)
    if await signup(context).start(update.effective_user.id, signup_session(context), send):
        access = await signup(context).access(update.effective_user.id, send)
        if access and access.allowed:
            await flow(context).start(session(context), send, access.database_ids)


@serialize_user
async def select_database_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await authorize(update, context)
    if access:
        await flow(context).start(session(context), make_sender(update), access.database_ids)


@serialize_user
async def questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await authorize(update, context)
    if access:
        await flow(context).questions(session(context), make_sender(update), access.database_ids)


@serialize_user
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    if signup_session(context).stage != "idle":
        session(context).reset()
        await signup(context).cancel(signup_session(context), make_sender(update))
    else:
        access = await authorize(update, context)
        if access:
            await flow(context).questions(session(context), make_sender(update), access.database_ids)


@serialize_user
async def reload_schema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await authorize(update, context)
    if access:
        await flow(context).reload_schema(session(context), make_sender(update), access.database_ids)


@serialize_user
async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update) or not update.effective_message.text:
        return
    registration = signup_session(context)
    send = make_sender(update)
    access = await signup(context).access(update.effective_user.id, send)
    if access is None:
        registration.reset(); session(context).reset(); return
    if registration.stage != "idle" or not access.allowed:
        session(context).reset()
        complete = await signup(context).text(
            update.effective_user.id, registration, update.effective_message.text, send,
        )
        if complete:
            access = await signup(context).access(update.effective_user.id, send)
            if access and access.allowed:
                await flow(context).start(session(context), send, access.database_ids)
        return
    await flow(context).text(session(context), update.effective_message.text, send, access.database_ids)


@serialize_user
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import TelegramError
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except TelegramError:
        logger.warning("Could not acknowledge callback query; it may have expired.")
    access = await authorize(update, context)
    if access:
        await flow(context).callback(session(context), query.data or "", make_sender(update), access.database_ids)


@serialize_user
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    await update.effective_message.reply_text(
        "First use: send /start and enter your approved @gmail.com email.\n"
        "Your email determines which databases you can see.\n\n"
        "After registration:\n"
        "1. Choose an allowed database with /start or /selectdatabase.\n"
        "2. Choose a recommended question and date period, or type your own question.\n"
        "3. Each question is independent; previous questions are not used as memory.\n\n"
        "/selectdatabase - change database\n/questions - show recommended questions\n"
        "/cancel - cancel signup or current selection\n/reloadschema - refresh selected database metadata\n"
        "/whoami - show linked email and allowed database IDs\n/help - show instructions"
    )


@serialize_user
async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    send = make_sender(update)
    access = await signup(context).access(update.effective_user.id, send)
    if access is None:
        return
    user = update.effective_user
    username = f"@{user.username}" if user.username else "(no username)"
    email = access.account.email if access.account else "Not registered"
    status = "Authorized" if access.allowed else "Not authorized"
    databases = ", ".join(sorted(access.database_ids)) if access.database_ids else "(none assigned)"
    await send(
        f"Username: {username}\nTelegram user ID: {user.id}\nLinked email: {email}\n"
        f"Status: {status}\nAllowed database IDs: {databases}", None,
    )


@serialize_user
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await authorize(update, context)
    if access:
        await update.effective_message.reply_text("Unknown command. Use /help for available commands.")


async def post_init(application):
    from telegram import BotCommand
    await application.bot.set_my_commands([
        BotCommand("start", "Register or choose a database"),
        BotCommand("selectdatabase", "Choose or change database"),
        BotCommand("questions", "Show recommended questions"),
        BotCommand("cancel", "Cancel signup or the current selection"),
        BotCommand("reloadschema", "Refresh selected database metadata"),
        BotCommand("whoami", "Show linked email and database access"),
        BotCommand("help", "Show instructions"),
    ])


async def post_shutdown(application):
    service: AgentService = application.bot_data["service"]
    await service.close()


async def on_error(update, context):
    logger.error("Unhandled Telegram error (%s).", type(context.error).__name__)


def main() -> None:
    from dotenv import load_dotenv
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Add TELEGRAM_BOT_TOKEN to .env.")
    ai_settings = load_ai_settings()
    permissions_file = config_path("TELEGRAM_ALLOWED_EMAILS_FILE", "allowed_emails.json")
    account_file = config_path("TELEGRAM_AUTH_DB_FILE", "data/bot_auth.sqlite3")
    database_file = config_path("TELEGRAM_DATABASES_FILE", "databases.json")
    permissions = load_permissions(permissions_file)
    load_catalog(database_file)
    auth_store = AuthStore(account_file, permissions_file)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logger.info("AI provider: %s | model: %s", ai_settings.provider, ai_settings.model)
    if not permissions:
        logger.warning("The email permission file is empty: %s", permissions_file)

    mcp = McpManager(PROJECT_ROOT)
    agent = DatabaseAgent(ai_settings, mcp)
    service = AgentService(mcp, agent)
    concurrency = max(1, min(int(os.getenv("TELEGRAM_CONCURRENT_UPDATES", "16")), 64))
    application = (
        Application.builder().token(token).concurrent_updates(concurrency)
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
    application.bot_data["flow"] = BotFlow(database_file, service)
    application.bot_data["signup"] = SignupFlow(auth_store)
    application.bot_data["service"] = service
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("selectdatabase", select_database_command))
    application.add_handler(CommandHandler("questions", questions_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("reloadschema", reload_schema_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_question))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(on_error)
    logger.info("Starting combined Telegram + MCP SSAS bot.")
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
