"""Email-only signup, persistent Telegram binding, and per-database permissions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


class RegistrationError(ValueError):
    pass


class AllowlistError(RuntimeError):
    pass


def normalize_email(value: str) -> str:
    email = value.strip().lower()

    if not re.fullmatch(
        r"[a-z0-9][a-z0-9._+\-]{0,63}@gmail\.com",
        email
    ):
        raise RegistrationError(
            "Enter your Gmail address in the form name@gmail.com."
        )

    local = email.split("@", 1)[0]

    if local.endswith(".") or ".." in local:
        raise RegistrationError(
            "Enter a valid name@gmail.com email address."
        )

    return email


@dataclass(frozen=True)
class Permission:
    email: str
    database_ids: frozenset[str]


def load_permissions(path: Path) -> dict[str, Permission]:
    try:
        root = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AllowlistError(f"Cannot read permission file {path}: {error}") from error
    if not isinstance(root, dict) or not isinstance(root.get("users"), list):
        raise AllowlistError(f'{path.name} must contain an object with a "users" list.')
    result: dict[str, Permission] = {}
    for index, raw in enumerate(root["users"]):
        location = f"users[{index}]"
        if not isinstance(raw, dict):
            raise AllowlistError(f"{location} must be an object.")
        try:
            email = normalize_email(raw.get("email", ""))
        except RegistrationError as error:
            raise AllowlistError(f"Invalid email at {location}.email.") from error
        databases = raw.get("databases")
        if not isinstance(databases, list) or not all(isinstance(x, str) for x in databases):
            raise AllowlistError(f"{location}.databases must be a list of database IDs.")
        cleaned: set[str] = set()
        for db_id in databases:
            db_id = db_id.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", db_id):
                raise AllowlistError(f"Invalid database ID {db_id!r} at {location}.databases.")
            cleaned.add(db_id)
        if email in result:
            raise AllowlistError(f"Duplicate email in {path.name}: {email}")
        result[email] = Permission(email, frozenset(cleaned))
    return result


@dataclass(frozen=True)
class Account:
    email: str
    telegram_id: int
    registered_at: str


@dataclass(frozen=True)
class Access:
    account: Account | None
    allowed: bool
    database_ids: frozenset[str] = frozenset()


class AuthStore:
    def __init__(self, database_path: Path, permissions_path: Path):
        self.database_path = database_path
        self.permissions_path = permissions_path
        load_permissions(permissions_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database_path.touch(mode=0o600, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    registered_at TEXT NOT NULL
                )
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
            if "password_hash" in columns:
                connection.execute("""
                    CREATE TABLE accounts_without_password (
                        email TEXT PRIMARY KEY COLLATE NOCASE,
                        telegram_id INTEGER NOT NULL UNIQUE,
                        registered_at TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    INSERT INTO accounts_without_password (email, telegram_id, registered_at)
                    SELECT email, telegram_id, registered_at FROM accounts
                """)
                connection.execute("DROP TABLE accounts")
                connection.execute("ALTER TABLE accounts_without_password RENAME TO accounts")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def access(self, telegram_id: int) -> Access:
        permissions = load_permissions(self.permissions_path)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT email, telegram_id, registered_at FROM accounts WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        account = Account(**dict(row)) if row else None
        permission = permissions.get(account.email) if account else None
        return Access(account, permission is not None, permission.database_ids if permission else frozenset())

    def available_email(self, value: str) -> str:
        email = normalize_email(value)
        if email not in load_permissions(self.permissions_path):
            raise RegistrationError("This email is not approved to use the bot. Ask the administrator to add it.")
        with self._connection() as connection:
            existing = connection.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone()
        if existing:
            raise RegistrationError(
                "That email is already linked to a Telegram account. Contact the administrator if it must be reset."
            )
        return email

    def register(self, telegram_id: int, email: str) -> Account:
        if not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or telegram_id <= 0:
            raise RegistrationError("A valid private Telegram account is required.")
        email = normalize_email(email)
        registered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if email not in load_permissions(self.permissions_path):
                    raise RegistrationError("This email is not approved to use the bot. Ask the administrator to add it.")
                connection.execute(
                    "INSERT INTO accounts (email, telegram_id, registered_at) VALUES (?, ?, ?)",
                    (email, telegram_id, registered_at),
                )
        except sqlite3.IntegrityError as error:
            raise RegistrationError(
                "That email or Telegram account is already registered. Use /start, or contact the administrator."
            ) from error
        return Account(email, telegram_id, registered_at)

    def list_accounts(self) -> list[Account]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT email, telegram_id, registered_at FROM accounts ORDER BY email"
            ).fetchall()
        return [Account(**dict(row)) for row in rows]

    def remove_account(self, email: str) -> bool:
        email = normalize_email(email)
        with self._connection() as connection:
            connection.execute("PRAGMA secure_delete = ON")
            result = connection.execute("DELETE FROM accounts WHERE email = ?", (email,))
        return result.rowcount > 0
