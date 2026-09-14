"""Telegram front-end for the existing SSAS/Qwen AI agent.

The AI/database logic stays in new_qwen_agent.py. This file only:
- receives Telegram messages via long polling,
- checks an editable allowlist,
- passes approved questions to ask(question, model_schema), and
- sends the answer back to Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

# Load the .env next to this script even if the bot is started from another folder.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Import after loading .env because new_qwen_agent validates its settings at import time.
from new_qwen_agent import ask, load_schema  # noqa: E402

from telegram import Chat, Update  # noqa: E402
from telegram.constants import ChatAction  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWLIST_FILE = Path(
    os.getenv(
        "TELEGRAM_ALLOWED_USERS_FILE",
        str(BASE_DIR / "allowed_users.txt"),
    )
)

# Telegram text messages are limited to 4096 characters. Leave some margin.
TELEGRAM_MESSAGE_CHUNK = 3900

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ssas_telegram_bot")

# The schema is loaded once at startup and can be refreshed with /reloadschema.
model_schema: str | None = None

# The existing agent does blocking network/database work. Serializing calls is a
# conservative first setup for PyADOMD/SSAS and prevents several users from
# hammering the laptop at the same time.
query_lock = asyncio.Lock()
schema_lock = asyncio.Lock()


def _normalize_allowlist_entry(value: str) -> str:
    """Normalize an allowlist line for comparison."""
    value = value.strip()
    if not value:
        return ""

    if value.startswith("@"):
        return "@" + value[1:].strip().lower()

    # Numeric Telegram IDs are kept as strings.
    return value


def load_allowed_users() -> set[str]:
    """Read the allowlist from disk.

    This is intentionally called for every request so adding/removing a line in
    allowed_users.txt takes effect without restarting the bot.
    """
    try:
        lines: Iterable[str] = ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        logger.error("Allowlist file does not exist: %s", ALLOWLIST_FILE)
        return set()
    except OSError:
        logger.exception("Could not read allowlist file: %s", ALLOWLIST_FILE)
        return set()

    allowed: set[str] = set()

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Permit comments after an entry: @someone  # note
        entry = line.split("#", 1)[0].strip()
        normalized = _normalize_allowlist_entry(entry)
        if normalized:
            allowed.add(normalized)

    return allowed


def user_is_allowed(update: Update) -> bool:
    """Return True when the Telegram sender is in allowed_users.txt."""
    user = update.effective_user
    if user is None:
        return False

    allowed = load_allowed_users()

    # Numeric Telegram user ID is the preferred stable identifier.
    if str(user.id) in allowed:
        return True

    # Username support is convenient for initial setup.
    if user.username:
        username = "@" + user.username.lower()
        if username in allowed:
            return True

    return False


async def authorize(update: Update) -> bool:
    """Apply private-chat and allowlist checks."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        return False

    # Keep database answers out of group chats by default.
    if chat.type != Chat.PRIVATE:
        await message.reply_text(
            "For security, please talk to this bot in a private Telegram chat."
        )
        return False

    if user_is_allowed(update):
        return True

    username = f"@{user.username}" if user.username else "(no username)"
    await message.reply_text(
        "Access denied. You are not in allowed_users.txt.\n\n"
        f"Your username: {username}\n"
        f"Your Telegram user ID: {user.id}\n\n"
        "Ask the bot administrator to add either your @username or numeric user ID."
    )
    logger.warning(
        "Denied Telegram user id=%s username=%s",
        user.id,
        user.username,
    )
    return False


async def send_long_text(update: Update, text: str) -> None:
    """Send an answer safely even if it exceeds one Telegram message."""
    message = update.effective_message
    if message is None:
        return

    text = str(text).strip() or "The agent returned an empty answer."

    for start in range(0, len(text), TELEGRAM_MESSAGE_CHUNK):
        await message.reply_text(text[start : start + TELEGRAM_MESSAGE_CHUNK])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update):
        return

    await update.effective_message.reply_text(
        "Connected to the SSAS AI agent.\n\n"
        "Send me a business/data question as a normal message and I will pass it "
        "to the agent.\n\n"
        "Commands:\n"
        "/help - show instructions\n"
        "/whoami - show your Telegram identity\n"
        "/reloadschema - re-read the live SSAS model schema"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update):
        return

    await update.effective_message.reply_text(
        "Just send a question, for example:\n\n"
        '"Which customers had the highest delivery volume last month?"\n\n'
        "The bot uses the same SSAS/Qwen agent as new_qwen_agent.py. "
        "The allowlist is read from allowed_users.txt on every request, so you "
        "can edit that file without restarting the bot."
    )


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /whoami is intentionally available even to a denied user so the admin can
    # obtain the person's stable Telegram numeric ID and add it to the allowlist.
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if message is None or user is None or chat is None:
        return

    if chat.type != Chat.PRIVATE:
        await message.reply_text("Please use /whoami in a private chat with this bot.")
        return

    username = f"@{user.username}" if user.username else "(no username)"
    await message.reply_text(
        f"Username: {username}\n"
        f"Telegram user ID: {user.id}\n\n"
        "For a long-term allowlist, the numeric user ID is recommended."
    )


async def reload_schema_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    global model_schema

    if not await authorize(update):
        return

    message = update.effective_messsage
    await message.reply_text("Reloading the SSAS schema...")

    try:
        async with schema_lock:
            # Do not block Telegram's asyncio event loop while querying SSAS.
            new_schema = await asyncio.to_thread(load_schema)
            model_schema = new_schema
    except Exception:
        logger.exception("Schema reload failed")
        await message.reply_text(
            "I could not reload the SSAS schema. Check the bot console/log for the "
            "full error."
        )
        return

    await message.reply_text("SSAS schema reloaded successfully.")


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update):
        return

    message = update.effective_message
    if message is None or not message.text:
        return

    question = message.text.strip()
    if not question:
        return

    if model_schema is None:
        await message.reply_text(
            "The SSAS schema is not loaded. Restart the bot or use /reloadschema."
        )
        return

    user = update.effective_user
    logger.info(
        "Question from id=%s username=%s: %s",
        user.id if user else None,
        user.username if user else None,
        question,
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    try:
        # ask() calls Qwen and SSAS synchronously, so run it in a worker thread.
        # Serialize initial usage for predictable SSAS behavior on the laptop.
        async with query_lock:
            schema_snapshot = model_schema
            answer = await asyncio.to_thread(
                ask,
                question,
                schema_snapshot,
            )
    except Exception:
        logger.exception("Agent failed while answering a Telegram question")
        await message.reply_text(
            "I could not answer that question because the AI/SSAS agent returned "
            "an error. Check the bot console/log for details."
        )
        return

    await send_long_text(update, answer)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorize(update):
        return
    await update.effective_message.reply_text(
        "Unknown command. Use /help, or send your question as normal text."
    )


def main() -> None:
    global model_schema

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN. Add it to the .env file next to "
            "telegram_bot.py."
        )

    if not ALLOWLIST_FILE.exists():
        raise RuntimeError(
            f"Allowlist file not found: {ALLOWLIST_FILE}. "
            "Create allowed_users.txt before starting the bot."
        )

    logger.info("Loading SSAS schema before starting Telegram polling...")
    model_schema = load_schema()
    logger.info("SSAS schema loaded. Starting Telegram bot.")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("reloadschema", reload_schema_command))

    # Normal questions. Exclude commands and keep this bot private-chat only.
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_question,
        )
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Long polling means no public web server, FastAPI endpoint, domain, or TLS
    # certificate is needed for this local-laptop setup.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
