"""Telegram front-end with first-use Technolife email registration."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from agent_service import AgentService
from bot_config import load_catalog
from bot_flow import BotFlow, Session
from email_auth import AuthStore, load_allowed_emails
from signup_flow import SignupFlow, SignupSession

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

BASE_DIR = Path(__file__).resolve().parent
TELEGRAM_MESSAGE_CHUNK = 3900
logger = logging.getLogger("ssas_telegram_bot")


def config_path(env_name: str, default_name: str) -> Path:
    path = Path(os.getenv(env_name, default_name))
    return path if path.is_absolute() else BASE_DIR / path


def text_chunks(text: str):
    """Leave room below Telegram's limit, including text containing emoji."""
    text = str(text).strip() or "The agent returned an empty answer."
    chunk = []
    units = 0
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
                [InlineKeyboardButton(label, callback_data=data)]
                for label, data in choices
            ])
        chunks = list(text_chunks(text))
        for i, chunk in enumerate(chunks):
            await update.effective_message.reply_text(
                chunk,
                parse_mode=None,
                reply_markup=keyboard if i == len(chunks) - 1 else None,
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


async def private_chat(update) -> bool:
    if update.effective_message is None or update.effective_chat is None or update.effective_user is None:
        return False
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Please use this bot in a private Telegram chat.")
        return False
    return True


async def authorize(update, context) -> bool:
    """Recheck the mapped email against the editable allowlist on every request."""
    if not await private_chat(update):
        return False
    send = make_sender(update)
    access = await signup(context).access(update.effective_user.id, send)
    if access and access.allowed:
        return True
    session(context).reset()
    if access is None:
        signup_session(context).reset()
        return False
    if access.account:
        signup_session(context).reset()
        await send("Your email is no longer approved to use the bot. Please contact the administrator.", None)
    elif signup_session(context).stage != "idle":
        await send("Finish your signup first, or use /start to begin again and /cancel to stop.", None)
    else:
        await signup(context).start(update.effective_user.id, signup_session(context), send)
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    session(context).reset()
    send = make_sender(update)
    if await signup(context).start(update.effective_user.id, signup_session(context), send):
        await flow(context).start(session(context), send)


async def select_database_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorize(update, context):
        await flow(context).start(session(context), make_sender(update))


async def questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorize(update, context):
        await flow(context).questions(session(context), make_sender(update))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    if signup_session(context).stage != "idle":
        session(context).reset()
        await signup(context).cancel(signup_session(context), make_sender(update))
    elif await authorize(update, context):
        await flow(context).questions(session(context), make_sender(update))


async def reload_schema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorize(update, context):
        await flow(context).reload_schema(session(context), make_sender(update))


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update) or not update.effective_message.text:
        return
    registration = signup_session(context)
    send = make_sender(update)
    access = await signup(context).access(update.effective_user.id, send)
    if access is None:
        registration.reset()
        session(context).reset()
        return
    if registration.stage != "idle" or not access.allowed:
        session(context).reset()
        complete = await signup(context).text(
            update.effective_user.id, registration, update.effective_message.text, send,
        )
        if complete:
            await flow(context).start(session(context), send)
        return
    await flow(context).text(session(context), update.effective_message.text, send)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import TelegramError

    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except TelegramError:
        logger.warning("Could not acknowledge callback query; it may have expired.")
    if await authorize(update, context):
        await flow(context).callback(session(context), query.data or "", make_sender(update))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    await update.effective_message.reply_text(
        "First use: send /start and enter your approved @technolife.com email.\n"
        "Your Telegram account is remembered after registration.\n\n"
        "After registration:\n"
        "1. Choose a database using /start or /selectdatabase.\n"
        "2. Choose a recommended question, then select or type its date period.\n"
        "3. Or type your own question to send it directly to the agent.\n\n"
        "/selectdatabase - change database\n"
        "/questions - show recommended questions\n"
        "/cancel - cancel signup or the current question/date selection\n"
        "/reloadschema - refresh the selected database schema\n"
        "/whoami - show your linked email and Telegram identity\n"
        "/help - show these instructions"
    )


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
    await send(f"Username: {username}\nTelegram user ID: {user.id}\n"
               f"Linked email: {email}\nStatus: {status}", None)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorize(update, context):
        await update.effective_message.reply_text("Unknown command. Use /help for available commands.")


async def post_init(application):
    from telegram import BotCommand

    await application.bot.set_my_commands([
        BotCommand("start", "Register or choose a database"),
        BotCommand("selectdatabase", "Choose or change database"),
        BotCommand("questions", "Show recommended questions"),
        BotCommand("cancel", "Cancel signup or the current selections"),
        BotCommand("reloadschema", "Refresh selected database schema"),
        BotCommand("whoami", "Show linked email and Telegram identity"),
        BotCommand("help", "Show instructions"),
    ])


async def on_error(update, context):
    # Exception strings from third-party clients can contain request data. Log
    # the type, never the Telegram update or credential-bearing message.
    logger.error("Unhandled Telegram error (%s).", type(context.error).__name__)


def main() -> None:
    from dotenv import load_dotenv
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Add TELEGRAM_BOT_TOKEN to the .env file next to telegram_bot.py.")
    email_file = config_path("TELEGRAM_ALLOWED_EMAILS_FILE", "allowed_emails.txt")
    account_file = config_path("TELEGRAM_AUTH_DB_FILE", "data/bot_auth.sqlite3")
    auth_store = AuthStore(account_file, email_file)
    database_file = config_path("TELEGRAM_DATABASES_FILE", "databases.json")
    load_catalog(database_file)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not load_allowed_emails(email_file):
        logger.warning("The email allowlist is empty. Add approved addresses to %s before signup.", email_file)
    application = Application.builder().token(token).concurrent_updates(False).post_init(post_init).build()
    application.bot_data["flow"] = BotFlow(database_file, AgentService())
    application.bot_data["signup"] = SignupFlow(auth_store)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("selectdatabase", select_database_command))
    application.add_handler(CommandHandler("questions", questions_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("reloadschema", reload_schema_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_question,
    ))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(on_error)
    logger.info("Starting Telegram bot with Technolife email registration.")
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
