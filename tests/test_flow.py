import json
import tempfile
import unittest
from pathlib import Path

from app.telegram.flow import BotFlow, Session


class FakeBackend:
    async def connect(self, target):
        return "ok"
    async def ask(self, target, question, knowledge_base=()):
        return "answer"
    async def refresh(self, target):
        return "ok"


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_menu_only_contains_permitted_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "databases.json"
            path.write_text(json.dumps({"databases": [
                {"id": "a", "name": "Database A", "server": "s", "database": "A", "questions": []},
                {"id": "b", "name": "Database B", "server": "s", "database": "B", "questions": []},
            ]}), encoding="utf-8")
            sent = []
            async def send(text, choices=None):
                sent.append((text, choices))
            flow = BotFlow(path, FakeBackend())
            await flow.start(Session(), send, frozenset({"b"}))
            labels = [label for label, _ in sent[-1][1]]
            self.assertEqual(labels, ["Database B"])
