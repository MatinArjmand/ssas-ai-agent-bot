import json
import tempfile
import unittest
from pathlib import Path

from app.auth import AuthStore, load_permissions


class AuthTests(unittest.TestCase):
    def test_permissions_are_per_email_and_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            permissions = root / "allowed_emails.json"
            db = root / "auth.sqlite3"
            permissions.write_text(json.dumps({"users": [
                {"email": "one@gmail.com", "databases": ["main"]},
                {"email": "two@gmail.com", "databases": ["second"]},
            ]}), encoding="utf-8")
            store = AuthStore(db, permissions)
            store.register(100, "one@gmail.com")
            access = store.access(100)
            self.assertTrue(access.allowed)
            self.assertEqual(access.database_ids, frozenset({"main"}))

            # Admin edits take effect on the next request; no restart or DB migration.
            permissions.write_text(json.dumps({"users": [
                {"email": "one@gmail.com", "databases": ["second"]},
            ]}), encoding="utf-8")
            access = store.access(100)
            self.assertEqual(access.database_ids, frozenset({"second"}))

    def test_empty_database_list_is_fail_closed_for_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowed_emails.json"
            path.write_text(json.dumps({"users": [
                {"email": "one@gmail.com", "databases": []}
            ]}), encoding="utf-8")
            loaded = load_permissions(path)
            self.assertIn("one@gmail.com", loaded)
            self.assertEqual(loaded["one@gmail.com"].database_ids, frozenset())
