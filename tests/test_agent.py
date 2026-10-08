import json
import unittest
from types import SimpleNamespace

from app.agent import DatabaseAgent
from app.ai_provider import AISettings
from app.config import DatabaseTarget


def call(call_id, name, args):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


class FakeAI:
    def __init__(self, messages):
        self.responses = list(messages)
        self.seen = []

    async def chat(self, messages, tools):
        self.seen.append(list(messages))
        return self.responses.pop(0)


class FakeMcp:
    def __init__(self):
        self.calls = []

    async def tools(self, target):
        return [{
            "type": "function",
            "function": {
                "name": "run_dax",
                "description": "run",
                "parameters": {"type": "object", "properties": {"dax": {"type": "string"}}, "required": ["dax"]},
            },
        }]

    async def call_tool(self, target, name, arguments=None):
        self.calls.append((name, arguments))
        return json.dumps({"columns": ["Total"], "rows": [{"Total": 42}], "row_count": 1, "truncated": False})


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_requires_live_dax_before_final_answer(self):
        first = SimpleNamespace(content="I think it is 42.", tool_calls=[])
        second = SimpleNamespace(content=None, tool_calls=[call("c1", "run_dax", {"dax": 'EVALUATE ROW("Total", 42)'})])
        third = SimpleNamespace(content="The total is 42.", tool_calls=[])
        fake_ai = FakeAI([first, second, third])
        mcp = FakeMcp()
        settings = AISettings("custom", "model", "key", "https://example.test/v1", max_agent_steps=5)
        agent = DatabaseAgent(settings, mcp, ai_client=fake_ai)
        target = DatabaseTarget("main", "Main", "server", "catalog", "Provider=MSOLAP;")
        answer = await agent.ask(target, "What is the total?")
        self.assertEqual(answer, "The total is 42.")
        self.assertEqual(mcp.calls[0][0], "run_dax")
        # The second AI turn contains the enforcement message because the first tried to answer without data.
        self.assertTrue(any(
            m.get("role") == "user" and "execute a read-only run_dax" in m.get("content", "")
            for m in fake_ai.seen[1]
        ))
