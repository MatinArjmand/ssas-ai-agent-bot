"""Inspect or reset persisted email <-> Telegram registrations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.auth import AuthStore

ROOT = Path(__file__).resolve().parent


def config_path(env_name: str, default_name: str) -> Path:
    path = Path(os.getenv(env_name, default_name))
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show registered emails, Telegram IDs and registration dates.")
    reset = commands.add_parser("reset", help="Remove one registration so its email can register again.")
    reset.add_argument("email", help="Exact @gmail.com email to reset.")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    database_path = config_path("TELEGRAM_AUTH_DB_FILE", "data/bot_auth.sqlite3")
    permissions_path = config_path("TELEGRAM_ALLOWED_EMAILS_FILE", "allowed_emails.json")
    if not database_path.exists():
        parser.exit(message="No account database exists yet. Start the bot first.\n")
    store = AuthStore(database_path, permissions_path)
    if args.command == "list":
        accounts = store.list_accounts()
        if not accounts:
            print("No registered accounts.")
        for account in accounts:
            print(f"{account.email}\t{account.telegram_id}\t{account.registered_at}")
    elif store.remove_account(args.email):
        print("Registration removed. The email can register again if it remains in allowed_emails.json.")
    else:
        print("No registration found for that email.")


if __name__ == "__main__":
    main()
