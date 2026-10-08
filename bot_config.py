"""Editable menu configuration and immutable SSAS connection targets."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re

from agent_knowledge import DaxExample


MAX_DAX_LENGTH = 50_000


class ConfigError(ValueError):
    """An actionable configuration error for the administrator's console."""


def _text(value, location: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ConfigError(f"{location} must be nonempty text of at most {limit} characters.")
    return value.strip()


def _object(value, location: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be a JSON object.")
    return value


def _dax(value, location: str) -> str:
    """Allow a JSON string or an editable list of lines; blank means no example."""
    if isinstance(value, list):
        if not all(isinstance(line, str) for line in value):
            raise ConfigError(f"{location} must contain only text lines.")
        value = "\n".join(value)
    if not isinstance(value, str):
        raise ConfigError(f"{location} must be text or a list of text lines; use \"\" to leave it empty.")
    if len(value) > MAX_DAX_LENGTH:
        raise ConfigError(f"{location} must be at most {MAX_DAX_LENGTH} characters.")
    return value.strip()


def _list(value, location: str) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"{location} must be a JSON list.")
    return value


def _expand(value: str, location: str) -> str:
    """Support ${ENV_NAME} references without evaluating any code."""
    def substitute(match):
        name = match.group(1)
        resolved = os.environ.get(name, "").strip()
        if not resolved:
            raise ConfigError(f"Set {name} in .env for {location}, then restart the bot.")
        return resolved

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value)


def _connection_value(value: str) -> str:
    # Quote values so a semicolon or quote in a catalog cannot change properties.
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True)
class DatabaseTarget:
    database_id: str
    name: str
    server: str = field(repr=False)
    catalog: str
    connection_string: str = field(repr=False)

    @property
    def identity(self) -> str:
        content = json.dumps([self.database_id, self.server, self.catalog, self.connection_string])
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DatePeriod:
    label: str
    value: str


@dataclass(frozen=True)
class Question:
    label: str
    question: str
    date_periods: tuple[DatePeriod, ...]
    dax: str = ""


@dataclass(frozen=True)
class Database:
    id: str
    name: str
    server: str = field(repr=False)
    database: str
    questions: tuple[Question, ...]
    connection_string_env: str | None = None

    @property
    def knowledge_base(self) -> tuple[DaxExample, ...]:
        return tuple(DaxExample(q.question, q.dax) for q in self.questions if q.dax.strip())

    def target(self) -> DatabaseTarget:
        server = _expand(self.server, f"database {self.id}.server")
        catalog = _expand(self.database, f"database {self.id}.database")
        if self.connection_string_env:
            # An advanced connection string is authoritative; the administrator
            # must keep its Data Source/Catalog consistent with this entry.
            connection = os.environ.get(self.connection_string_env, "").strip()
            if not connection:
                raise ConfigError(f"Set {self.connection_string_env} in .env and restart the bot.")
        else:
            connection = (
                f"Provider=MSOLAP;Data Source={_connection_value(server)};"
                f"Catalog={_connection_value(catalog)};"
            )
        return DatabaseTarget(self.id, self.name, server, catalog, connection)


@dataclass(frozen=True)
class Catalog:
    revision: str
    databases: tuple[Database, ...]

    def get(self, database_id: str | None) -> Database | None:
        return next((db for db in self.databases if db.id == database_id), None)


def load_catalog(path: Path) -> Catalog:
    """Read on each interaction; invalid edits fail closed with a useful log."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
        root = _object(json.loads(raw), "Root")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"Cannot read {path.name}: {error}") from error

    databases = []
    seen_ids = set()
    for i, entry in enumerate(_list(root.get("databases"), "databases")):
        loc = f"databases[{i}]"
        entry = _object(entry, loc)
        database_id = _text(entry.get("id"), f"{loc}.id", 64)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", database_id):
            raise ConfigError(f"{loc}.id may contain only letters, numbers, _ and -.")
        if database_id in seen_ids:
            raise ConfigError(f"Duplicate database id: {database_id}")
        seen_ids.add(database_id)
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{loc}.enabled must be true or false.")
        if not enabled:
            continue
        questions = []
        for j, item in enumerate(_list(entry.get("questions", []), f"{loc}.questions")):
            qloc = f"{loc}.questions[{j}]"
            item = _object(item, qloc)
            question = _text(item.get("question"), f"{qloc}.question")
            label = _text(item.get("label", question), f"{qloc}.label", 100)
            periods = []
            for k, period in enumerate(_list(item.get("date_periods", []), f"{qloc}.date_periods")):
                ploc = f"{qloc}.date_periods[{k}]"
                if isinstance(period, str):
                    label_text = _text(period, ploc, 100)
                    periods.append(DatePeriod(label_text, label_text))
                else:
                    period = _object(period, ploc)
                    periods.append(DatePeriod(
                        _text(period.get("label"), f"{ploc}.label", 100),
                        _text(period.get("value"), f"{ploc}.value", 1000),
                    ))
            questions.append(Question(label, question, tuple(periods), _dax(item.get("dax", ""), f"{qloc}.dax")))
        connection_env = entry.get("connection_string_env")
        if connection_env is not None:
            connection_env = _text(connection_env, f"{loc}.connection_string_env", 100)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", connection_env):
                raise ConfigError(f"{loc}.connection_string_env must be an environment variable name.")
        databases.append(Database(
            id=database_id,
            name=_text(entry.get("name"), f"{loc}.name", 100),
            server=_text(entry.get("server"), f"{loc}.server", 1000),
            database=_text(entry.get("database"), f"{loc}.database", 1000),
            questions=tuple(questions),
            connection_string_env=connection_env,
        ))
    if not databases:
        raise ConfigError("Enable at least one database in databases.json.")
    canonical = json.dumps(root, sort_keys=True, ensure_ascii=False)
    return Catalog(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), tuple(databases))
