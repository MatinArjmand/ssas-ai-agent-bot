"""Application-facing facade for connection checks, metadata refresh, and stateless questions."""

from __future__ import annotations

from .agent import DatabaseAgent
from .config import DatabaseTarget
from .knowledge import DaxExample
from .mcp_host import McpManager


class AgentService:
    def __init__(self, mcp: McpManager, agent: DatabaseAgent):
        self.mcp = mcp
        self.agent = agent

    async def connect(self, target: DatabaseTarget) -> str:
        return await self.mcp.health_check(target)

    async def ask(self, target: DatabaseTarget, question: str, knowledge_base: tuple[DaxExample, ...] = ()) -> str:
        return await self.agent.ask(target, question, knowledge_base)

    async def refresh(self, target: DatabaseTarget) -> str:
        return await self.mcp.refresh_metadata(target)

    async def close(self) -> None:
        await self.mcp.close_all()
