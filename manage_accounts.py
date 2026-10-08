"""Run on the bot computer to inspect or reset email/Telegram registrations."""

import argparse
from pathlib import Path

from email_auth import AuthStore
from telegram_bot import config_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show registered email/Telegram pairs; never prints password hashes.")
    reset = commands.add_parser("reset", help="Remove one registration so its email can register again.")
    reset.add_argument("email", help="Exact @technolife.com email to reset.")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
    database_path = config_path("TELEGRAM_AUTH_DB_FILE", "data/bot_auth.sqlite3")
    if not database_path.exists():
        parser.exit(message="No account database exists yet. Start the bot first.\n")
    store = AuthStore(database_path, config_path("TELEGRAM_ALLOWED_EMAILS_FILE", "allowed_emails.txt"))
    if args.command == "list":
        accounts = store.list_accounts()
        if not accounts:
            print("No registered accounts.")
        for account in accounts:
            print(f"{account.email}\t{account.telegram_id}\t{account.registered_at}")
    elif store.remove_account(args.email):
        print("Registration removed. The email can register again if it remains in allowed_emails.txt.")
    else:
        print("No registration found for that email.")


if __name__ == "__main__":
    main()
