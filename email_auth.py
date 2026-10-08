"""Email allowlist and persistent Telegram account bindings.

Registration intentionally does not verify mailbox ownership. The administrator
controls which exact @technolife.com addresses may register.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3


class RegistrationError(ValueError):
    """A registration problem that can be explained to the user."""


class AllowlistError(RuntimeError):
    """An administrator configuration error; access must fail closed."""


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._+\-]{0,63}@technolife\.com", email):
        raise RegistrationError("Enter your work email in the form name@technolife.com.")
    local = email.split("@", 1)[0]
    if local.endswith(".") or ".." in local:
        raise RegistrationError("Enter a valid name@technolife.com email address.")
    return email


def load_allowed_emails(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise AllowlistError(f"Cannot read email allowlist: {path}") from error
    emails = set()
    for number, line in enumerate(lines, 1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        try:
            emails.add(normalize_email(entry))
        except RegistrationError as error:
            raise AllowlistError(f"Invalid Technolife email on line {number} of {path.name}.") from error
    return emails


@dataclass(frozen=True)
class Account:
    email: str
    telegram_id: int
    registered_at: str


@dataclass(frozen=True)
class Access:
    account: Account | None
    allowed: bool


class AuthStore:
    def __init__(self, database_path: Path, allowlist_path: Path):
        self.database_path = database_path
        self.allowlist_path = allowlist_path
        load_allowed_emails(allowlist_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database_path.touch(mode=0o600, exist_ok=True)
        with self._connection() as connection:
            # Serialize startup/migration with other writers. DDL and row copies
            # commit together, so a failed migration preserves the old table.
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
                # Upgrade the previous release while retaining every email/ID
                # binding and registration date. No password data is copied.
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
        allowed_emails = load_allowed_emails(self.allowlist_path)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT email, telegram_id, registered_at FROM accounts WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        account = Account(**dict(row)) if row else None
        return Access(account, account is not None and account.email in allowed_emails)

    def available_email(self, value: str) -> str:
        email = normalize_email(value)
        if email not in load_allowed_emails(self.allowlist_path):
            raise RegistrationError("This email is not approved to use the bot. Ask the administrator to add it.")
        with self._connection() as connection:
            existing = connection.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone()
        if existing:
            raise RegistrationError(
                "That email is already linked to a Telegram account. "
                "Contact the administrator if the link needs to be reset."
            )
        return email

    def register(self, telegram_id: int, email: str) -> Account:
        if not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or telegram_id <= 0:
            raise RegistrationError("A valid private Telegram account is required.")
        email = normalize_email(email)
        registered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._connection() as connection:
                # A competing signup cannot overwrite an existing email or ID.
                connection.execute("BEGIN IMMEDIATE")
                if email not in load_allowed_emails(self.allowlist_path):
                    raise RegistrationError("This email is not approved to use the bot. Ask the administrator to add it.")
                connection.execute(
                    "INSERT INTO accounts (email, telegram_id, registered_at) VALUES (?, ?, ?)",
                    (email, telegram_id, registered_at),
                )
        except sqlite3.IntegrityError as error:
            raise RegistrationError(
                "That email or Telegram account is already registered. Use /start, "
                "or contact the administrator to reset the existing link."
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
