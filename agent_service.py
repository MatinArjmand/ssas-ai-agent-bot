"""Connect, cache schemas per target, and call the existing Qwen agent."""

from __future__ import annotations

import importlib
import threading

from bot_config import DatabaseTarget


class AgentService:
    def __init__(self, agent=None):
        self._agent = agent
        self._schemas: dict[str, tuple[str, str]] = {}
        # Keep native SSAS and AI work serialized as in the original bot.
        self._lock = threading.RLock()

    def _get_agent(self):
        if self._agent is None:
            self._agent = importlib.import_module("new_qwen_agent")
        return self._agent

    def connect(self, target: DatabaseTarget) -> str:
        """Every selection/reload verifies a real connection and loads its schema."""
        with self._lock:
            self._schemas.pop(target.database_id, None)
            schema = self._get_agent().load_schema(
                connection_string=target.connection_string,
                database_name=target.catalog,
            )
            self._schemas[target.database_id] = (target.identity, schema)
            return schema

    def ask(self, target: DatabaseTarget, question: str) -> str:
        with self._lock:
            cached = self._schemas.get(target.database_id)
            if cached is None or cached[0] != target.identity:
                schema = self.connect(target)
            else:
                schema = cached[1]
            return self._get_agent().ask(
                question,
                schema,
                connection_string=target.connection_string,
            )
