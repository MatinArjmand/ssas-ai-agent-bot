import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import load_catalog
from app.knowledge import DaxExample, KnowledgeBase


class ConfigKnowledgeTests(unittest.TestCase):
    def test_catalog_filter_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "databases.json"
            path.write_text(json.dumps({"databases": [
                {"id": "a", "name": "A", "server": "${A_SERVER}", "database": "A_DB", "questions": []},
                {"id": "b", "name": "B", "server": "b-server", "database": "B_DB", "questions": []},
            ]}), encoding="utf-8")
            with patch.dict(os.environ, {"A_SERVER": "a-server"}, clear=False):
                catalog = load_catalog(path)
                permitted = catalog.permitted({"b"})
                self.assertEqual([db.id for db in permitted.databases], ["b"])
                self.assertEqual(catalog.get("a").target().server, "a-server")

    def test_knowledge_base_prefers_exact_question(self):
        kb = KnowledgeBase([
            DaxExample("Total sales", "EVALUATE ROW(\"x\", 1)"),
            DaxExample("Sales by city", "EVALUATE TOPN(10, 'City')"),
        ])
        hits = kb.search("Total sales", limit=2)
        self.assertEqual(hits[0].question, "Total sales")
