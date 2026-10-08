"""Database-scoped MCP stdio workers shared safely by concurrent Telegram updates."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

from .config import DatabaseTarget

LOGGER = logging.getLogger(__name__)


def _schema(tool: Any) -> dict[str, Any]:
    value = getattr(tool, "input_schema", None)
    if value is None:
        value = getattr(tool, "inputSchema", None)
    return value if isinstance(value, dict) else {"type": "object", "properties": {}}


def _tool_text(result: Any) -> str:
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str)
    chunks = []
    for item in getattr(result, "content", []) or []:
        value = getattr(item, "text", None)
        if value is not None:
            chunks.append(str(value))
    return "\n".join(chunks).strip() or "{}"


@dataclass
class _Request:
    kind: str
    name: str | None
    arguments: dict[str, Any] | None
    future: asyncio.Future


class _DatabaseWorker:
    """Own one stdio transport in one asyncio task for its entire lifetime."""

    def __init__(self, manager: "McpManager", target: DatabaseTarget):
        self.manager = manager
        self.target = target
        self.target_identity = target.identity
        self.queue: asyncio.Queue[_Request] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        self.ready: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        self.tools: list[dict[str, Any]] = []
        self.tool_names: frozenset[str] = frozenset()
        self.task = asyncio.create_task(self._run(), name=f"mcp-{target.database_id}")

    async def _run(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        stack = AsyncExitStack()
        close_future: asyncio.Future | None = None
        current: _Request | None = None
        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "app.mcp_server.server"],
                env=self.manager._environment(self.target),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            response = await session.list_tools()
            names: set[str] = set()
            tools: list[dict[str, Any]] = []
            for tool in response.tools:
                name = str(tool.name)
                names.add(name)
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(getattr(tool, "description", "") or ""),
                        "parameters": _schema(tool),
                    },
                })
            self.tools = tools
            self.tool_names = frozenset(names)
            if not self.ready.done():
                self.ready.set_result(list(tools))

            while True:
                current = await self.queue.get()
                if current.kind == "close":
                    close_future = current.future
                    current = None
                    break
                if current.kind != "call" or current.name is None:
                    if not current.future.done():
                        current.future.set_exception(ValueError("Invalid MCP worker request."))
                    current = None
                    continue
                if current.name not in self.tool_names:
                    if not current.future.done():
                        current.future.set_exception(ValueError(f"Unknown MCP tool: {current.name}"))
                    current = None
                    continue
                try:
                    result = await session.call_tool(current.name, arguments=current.arguments or {})
                    text = _tool_text(result)
                    is_error = getattr(result, "is_error", None)
                    if is_error is None:
                        is_error = getattr(result, "isError", False)
                    if is_error:
                        text = "MCP tool error: " + text
                    if not current.future.done():
                        current.future.set_result(text)
                except Exception as error:  # noqa: BLE001
                    if not current.future.done():
                        current.future.set_exception(error)
                finally:
                    current = None
        except Exception as error:  # noqa: BLE001
            if current is not None and not current.future.done():
                current.future.set_exception(error)
            if not self.ready.done():
                self.ready.set_exception(error)
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if not pending.future.done():
                    pending.future.set_exception(error)
            LOGGER.error("MCP worker for %s stopped (%s).", self.target.database_id, type(error).__name__)
        finally:
            try:
                await stack.aclose()
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("MCP cleanup for %s failed (%s).", self.target.database_id, type(error).__name__)
            if close_future is not None and not close_future.done():
                close_future.set_result(None)

    async def start(self) -> list[dict[str, Any]]:
        return list(await self.ready)

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        await self.ready
        if self.task.done():
            # Propagate the worker exception if it crashed.
            await self.task
            raise RuntimeError("MCP worker stopped unexpectedly.")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await self.queue.put(_Request("call", name, arguments or {}, future))
        return await future

    async def close(self) -> None:
        if self.task.done():
            try:
                await self.task
            except Exception:  # noqa: BLE001
                pass
            return
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.queue.put(_Request("close", None, None, future))
        await future
        await self.task


class McpManager:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self._workers: dict[str, _DatabaseWorker] = {}
        self._lock = asyncio.Lock()

    def _environment(self, target: DatabaseTarget) -> dict[str, str]:
        env = os.environ.copy()
        adomd_path = env.get("ADOMD_PATH", "").strip()
        if not adomd_path:
            raise RuntimeError("ADOMD_PATH is not configured in .env.")
        cache_dir = self.project_root / "data" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        env.update({
            "SSAS_SERVER": target.server,
            "SSAS_DATABASE": target.catalog,
            "ADOMD_PATH": adomd_path,
            "MCP_TRANSPORT": "stdio",
            "APPLICATION_NAME": f"SSAS-Telegram-MCP-{target.database_id}",
            "SCHEMA_CACHE_PATH": str(cache_dir / f"schema-{target.database_id}-{target.identity[:12]}.json"),
        })
        if target.custom_connection:
            env["SSAS_CONNECTION_STRING"] = target.connection_string
        else:
            env.pop("SSAS_CONNECTION_STRING", None)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.project_root) + (os.pathsep + existing if existing else "")
        return env

    async def _get_worker(self, target: DatabaseTarget) -> _DatabaseWorker:
        async with self._lock:
            current = self._workers.get(target.database_id)
            if current and current.target_identity == target.identity:
                worker = current
            else:
                if current:
                    self._workers.pop(target.database_id, None)
                    await current.close()
                worker = _DatabaseWorker(self, target)
                self._workers[target.database_id] = worker
            try:
                await worker.start()
            except Exception:
                if self._workers.get(target.database_id) is worker:
                    self._workers.pop(target.database_id, None)
                await worker.close()
                raise
            return worker

    async def tools(self, target: DatabaseTarget) -> list[dict[str, Any]]:
        worker = await self._get_worker(target)
        return list(worker.tools)

    async def call_tool(self, target: DatabaseTarget, name: str, arguments: dict[str, Any] | None = None) -> str:
        worker = await self._get_worker(target)
        return await worker.call(name, arguments)

    async def health_check(self, target: DatabaseTarget) -> str:
        return await self.call_tool(target, "health_check", {})

    async def refresh_metadata(self, target: DatabaseTarget) -> str:
        return await self.call_tool(target, "refresh_metadata", {})

    async def close_all(self) -> None:
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            await worker.close()
