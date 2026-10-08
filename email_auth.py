"""Email allowlist, password hashing and persistent Telegram account bindings.

Registration intentionally does not verify mailbox ownership. The administrator
controls which exact @technolife.com addresses may register.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
from pathlib import Path
import re
import secrets
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


SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                          r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=SCRYPT_MAXMEM)


def _decode_hash(encoded: str) -> tuple[bytes, bytes]:
    algorithm, n, r, p, salt_text, digest_text = encoded.split("$")
    if (algorithm, n, r, p) != ("scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)):
        raise ValueError("Unsupported password hash")
    salt = base64.b64decode(salt_text, validate=True)
    digest = base64.b64decode(digest_text, validate=True)
    if len(salt) != 16 or len(digest) != 32:
        raise ValueError("Invalid password hash")
    return salt, digest


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128 or not password.strip():
        raise RegistrationError("Choose a bot password between 12 and 128 characters.")
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt)
    return "$".join(("scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                     base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")))


def password_matches(password: str, encoded: str) -> bool:
    if not 12 <= len(password) <= 128:
        return False
    try:
        salt, digest = _decode_hash(encoded)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_derive(password, salt), digest)


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
            connection.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                )
            """)

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

    def register(self, telegram_id: int, email: str, password_hash: str) -> Account:
        if not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or telegram_id <= 0:
            raise RegistrationError("A valid private Telegram account is required.")
        email = normalize_email(email)
        # Reject accidental plaintext writes even if this API is called directly.
        try:
            _decode_hash(password_hash)
        except (ValueError, TypeError) as error:
            raise RegistrationError("The password could not be saved. Please restart signup.") from error
        registered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._connection() as connection:
                # A competing signup cannot overwrite an existing email or ID.
                connection.execute("BEGIN IMMEDIATE")
                if email not in load_allowed_emails(self.allowlist_path):
                    raise RegistrationError("This email is no longer approved. Contact the administrator.")
                connection.execute(
                    "INSERT INTO accounts (email, telegram_id, password_hash, registered_at) VALUES (?, ?, ?, ?)",
                    (email, telegram_id, password_hash, registered_at),
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
