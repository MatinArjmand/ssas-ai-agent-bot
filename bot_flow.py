"""Conversation flow, independent of Telegram so it can be tested offline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import secrets
from typing import Awaitable, Callable

from agent_service import AgentService
from bot_config import Catalog, ConfigError, Database, DatabaseTarget, load_catalog


logger = logging.getLogger(__name__)
PAGE_SIZE = 8
Choices = list[tuple[str, str]]  # (button label, opaque callback data)
Send = Callable[[str, Choices | None], Awaitable[None]]


@dataclass(frozen=True)
class Action:
    kind: str
    value: int | str = ""


@dataclass(frozen=True)
class Menu:
    nonce: str
    revision: str
    actions: tuple[Action, ...]


@dataclass
class Session:
    database_id: str | None = None
    target_identity: str | None = None
    pending_question: int | None = None
    pending_revision: str | None = None
    menu: Menu | None = None

    def clear_pending(self):
        self.pending_question = None
        self.pending_revision = None
        self.menu = None

    def reset(self):
        self.database_id = None
        self.target_identity = None
        self.clear_pending()


def combine_question(database: Database, target: DatabaseTarget, question: str,
                     period: str, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    return (
        f"Selected database: {database.name}\n"
        f"Database catalog: {target.catalog}\n"
        f"Question: {question}\n"
        f"Date period: {period}\n"
        f"Reference date: {now.date().isoformat()} "
        f"(bot computer local time, UTC offset {now.strftime('%z')}).\n"
        "Answer the question for this date period using the selected database schema. "
        "Resolve relative periods against the reference date."
    )


class BotFlow:
    def __init__(self, config_path: Path, backend: AgentService):
        self.config_path = config_path
        self.backend = backend

    async def _catalog(self, state: Session, send: Send) -> Catalog | None:
        try:
            return load_catalog(self.config_path)
        except ConfigError:
            logger.exception("Invalid database menu configuration")
            state.reset()
            await send("The database options are temporarily unavailable. Please contact the bot administrator.", None)
            return None

    async def _menu(self, state: Session, catalog: Catalog, send: Send,
                    text: str, items: list[tuple[str, Action]],
                    page_kind: str, page: int = 0,
                    extras: list[tuple[str, Action]] | None = None):
        page_count = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        visible = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        if page > 0:
            visible.append(("Previous page", Action(page_kind, page - 1)))
        if page + 1 < page_count:
            visible.append(("Next page", Action(page_kind, page + 1)))
        visible.extend(extras or [])
        nonce = secrets.token_hex(8)
        menu = Menu(nonce, catalog.revision, tuple(action for _, action in visible))
        buttons = [(label, f"menu:{nonce}:{i}") for i, (label, _) in enumerate(visible)]
        if page_count > 1:
            text += f"\nPage {page + 1} of {page_count}."
        state.menu = None
        await send(text, buttons or None)
        state.menu = menu

    async def _databases(self, state: Session, catalog: Catalog, send: Send,
                         prefix: str = "", page: int = 0):
        state.reset()
        await self._menu(
            state, catalog, send,
            prefix + "Which database do you have a question about? Choose one below.",
            [(db.name, Action("database", db.id)) for db in catalog.databases],
            "database_page", page,
        )

    async def _questions(self, state: Session, catalog: Catalog, database: Database,
                         send: Send, prefix: str = "", page: int = 0):
        state.clear_pending()
        text = prefix + f"Selected database: {database.name}\n\n"
        text += ("Choose a recommended question below, or type and send your own question."
                 if database.questions else "Type and send your question.")
        await self._menu(
            state, catalog, send, text,
            [(q.label, Action("question", i)) for i, q in enumerate(database.questions)],
            "question_page", page,
            [("Ask my own question", Action("custom_question")),
             ("Change database", Action("change_database"))],
        )

    async def _periods(self, state: Session, catalog: Catalog, database: Database,
                       question_index: int, send: Send, page: int = 0):
        question = database.questions[question_index]
        state.pending_question = question_index
        state.pending_revision = catalog.revision
        await self._menu(
            state, catalog, send,
            f"Database: {database.name}\nQuestion: {question.question}\n\n"
            "Choose a date period below, or type and send your own date period.\n"
            "Example: 2026-01-01 to 2026-03-31.\n"
            "Use /cancel to choose a different question.",
            [(period.label, Action("period", i)) for i, period in enumerate(question.date_periods)],
            "period_page", page,
            [("Type my own date period", Action("custom_period")),
             ("Back to questions", Action("questions"))],
        )

    async def _selected(self, state: Session, catalog: Catalog, send: Send
                        ) -> tuple[Database, DatabaseTarget] | None:
        database = catalog.get(state.database_id)
        if database is None:
            await self._databases(state, catalog, send)
            return None
        try:
            target = database.target()
        except ConfigError:
            logger.exception("Invalid connection configuration for database %s", database.id)
            await self._databases(state, catalog, send,
                                  "That database needs administrator setup. Please choose a database.\n\n")
            return None
        if target.identity != state.target_identity:
            await self._databases(state, catalog, send,
                                  "The database connection settings changed. Please select it again.\n\n")
            return None
        return database, target

    async def start(self, state: Session, send: Send):
        catalog = await self._catalog(state, send)
        if catalog:
            await self._databases(state, catalog, send)

    async def questions(self, state: Session, send: Send):
        catalog = await self._catalog(state, send)
        if catalog:
            selected = await self._selected(state, catalog, send)
            if selected:
                await self._questions(state, catalog, selected[0], send)

    async def _connect(self, state: Session, catalog: Catalog, database: Database, send: Send):
        state.reset()
        await send(f"Connecting to {database.name}...", None)
        try:
            target = database.target()
            await asyncio.to_thread(self.backend.connect, target)
        except Exception:
            logger.exception("Cannot connect to database %s", database.id)
            await self._databases(state, catalog, send,
                                  f"I could not connect to {database.name}. Please try again or choose another database.\n\n")
            return
        state.database_id = database.id
        state.target_identity = target.identity
        await self._questions(state, catalog, database, send, "Connected successfully.\n\n")

    async def _ask(self, state: Session, catalog: Catalog, database: Database,
                   target: DatabaseTarget, question: str, send: Send):
        # Consume the old menu/date choice before doing any blocking work so a
        # repeated callback cannot issue the same query twice.
        state.clear_pending()
        await send(f"Checking {database.name}. Please wait...", None)
        try:
            answer = await asyncio.to_thread(self.backend.ask, target, question)
        except Exception:
            logger.exception("Agent failed for database %s", database.id)
            await send("I could not answer that question. Please try again or contact the bot administrator.", None)
        else:
            await send(str(answer).strip() or "The agent returned an empty answer.", None)
        await self._questions(state, catalog, database, send)

    async def text(self, state: Session, text: str, send: Send):
        text = text.strip()
        if not text:
            return
        catalog = await self._catalog(state, send)
        if catalog is None:
            return
        selected = await self._selected(state, catalog, send)
        if selected is None:
            return
        database, target = selected
        if state.pending_question is not None:
            if state.pending_revision != catalog.revision:
                await self._questions(state, catalog, database, send,
                                      "The recommended options changed. Please choose your question again.\n\n")
                return
            question = database.questions[state.pending_question]
            prompt = combine_question(database, target, question.question, text)
        else:
            # A typed message is a custom question even if it matches a button label.
            prompt = text
        await self._ask(state, catalog, database, target, prompt, send)

    async def callback(self, state: Session, data: str, send: Send):
        catalog = await self._catalog(state, send)
        if catalog is None:
            return
        menu = state.menu
        try:
            prefix, nonce, raw_index = data.split(":")
            index = int(raw_index)
            if prefix != "menu" or menu is None or nonce != menu.nonce or not 0 <= index < len(menu.actions):
                raise ValueError("Inactive button")
            action = menu.actions[index]
        except (ValueError, AttributeError):
            await send("That menu is no longer active. Use the latest buttons, /questions, or /selectdatabase.", None)
            return
        if menu.revision != catalog.revision:
            if state.database_id is None:
                await self._databases(state, catalog, send, "The database options changed. Please choose again.\n\n")
            else:
                selected = await self._selected(state, catalog, send)
                if selected:
                    await self._questions(state, catalog, selected[0], send,
                                          "The recommended options changed. Please choose again.\n\n")
            return
        state.menu = None
        if action.kind in ("change_database", "database_page"):
            await self._databases(state, catalog, send,
                                  page=int(action.value) if action.kind == "database_page" else 0)
            return
        if action.kind == "database":
            database = catalog.get(str(action.value))
            if database:
                await self._connect(state, catalog, database, send)
            return
        selected = await self._selected(state, catalog, send)
        if selected is None:
            return
        database, target = selected
        if action.kind in ("questions", "question_page"):
            await self._questions(state, catalog, database, send,
                                  page=int(action.value) if action.kind == "question_page" else 0)
        elif action.kind == "custom_question":
            state.clear_pending()
            await send(f"Selected database: {database.name}\nType and send your question.", None)
        elif action.kind == "question":
            await self._periods(state, catalog, database, int(action.value), send)
        elif action.kind == "period_page" and state.pending_question is not None:
            await self._periods(state, catalog, database, state.pending_question, send, int(action.value))
        elif action.kind == "custom_period" and state.pending_question is not None:
            await send("Type your date period, for example: 2026-01-01 to 2026-03-31.\n"
                       "Use /cancel to choose a different question.", None)
        elif action.kind == "period" and state.pending_question is not None:
            question = database.questions[state.pending_question]
            period = question.date_periods[int(action.value)]
            prompt = combine_question(database, target, question.question, period.value)
            await self._ask(state, catalog, database, target, prompt, send)

    async def reload_schema(self, state: Session, send: Send):
        catalog = await self._catalog(state, send)
        if catalog is None:
            return
        selected = await self._selected(state, catalog, send)
        if selected is None:
            return
        database, target = selected
        state.clear_pending()
        await send(f"Refreshing {database.name}...", None)
        try:
            await asyncio.to_thread(self.backend.connect, target)
        except Exception:
            logger.exception("Schema reload failed for database %s", database.id)
            await self._databases(state, catalog, send,
                                  "I could not refresh that database. Please reconnect or choose another database.\n\n")
            return
        await self._questions(state, catalog, database, send, "Database schema refreshed.\n\n")
