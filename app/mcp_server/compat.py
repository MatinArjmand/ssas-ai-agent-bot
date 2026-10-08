"""Tiny compatibility shim for MCP Python SDK v2 (preferred) and v1."""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger("ssas_mcp.compat")

try:  # MCP Python SDK v2
    from mcp.server import MCPServer as ServerClass  # type: ignore
    from mcp.server.mcpserver.exceptions import ToolError  # type: ignore
    MCP_MAJOR = 2
except ImportError:  # MCP Python SDK v1
    from mcp.server.fastmcp import FastMCP as ServerClass  # type: ignore
    try:
        from mcp.server.fastmcp.exceptions import ToolError  # type: ignore
    except ImportError:
        class ToolError(Exception):  # type: ignore[no-redef]
            """Deliberate, model-visible tool failure."""
    MCP_MAJOR = 1


def create_server(name: str, instructions: str) -> Any:
    return ServerClass(name, instructions=instructions)


def run_server(server: Any, transport: str, host: str, port: int) -> None:
    if transport == "stdio":
        server.run(transport="stdio")
        return
    if MCP_MAJOR >= 2:
        server.run(transport=transport, host=host, port=port)
        return
    try:
        server.settings.host = host
        server.settings.port = port
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not set MCP v1 host/port; using SDK defaults.")
    server.run(transport=transport)
