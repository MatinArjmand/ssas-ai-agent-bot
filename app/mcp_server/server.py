"""MCP server exposing a read-only SSAS Tabular model.

The LLM no longer lives inside this process. The MCP host (Claude Desktop,
Claude Code, any MCP client) is the model; this server only exposes
  * metadata tools, so the model can discover the real schema, and
  * one guarded execution tool, so it can run the DAX it wrote.

Design notes
------------
* All ADOMD work happens on ONE dedicated worker thread. pythonnet objects do
  not enjoy being shared across threads, and a single worker also serializes
  cube access so a chatty client cannot open ten concurrent sessions.
* Nothing is printed to stdout. On the stdio transport stdout *is* the protocol
  channel; a stray print() corrupts the stream. Logs go to stderr.
* Every tool returns JSON-serializable dicts, so the host gets structured
  content instead of a wall of text it has to re-parse.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .compat import ToolError, create_server, run_server
from .client import SsasClient
from .dax import DAX_RULES, DaxValidationError, validate_dax
from .metadata import (
    ModelIndex,
    build_schema,
    build_table_detail,
    get_column_data_type,
    get_metadata_column_name,
    load_index,
)
from .settings import Settings, load_settings
from .util import dax_table_name, get_bool, truncate

LOGGER = logging.getLogger("ssas_mcp")

# =========================================================
# PROCESS STATE (built lazily, on the worker thread)
# =========================================================

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ssas-worker")
_LOCK = threading.Lock()

_settings: Settings | None = None
_client: SsasClient | None = None
_index: ModelIndex | None = None
_schema_text: str | None = None


def _bootstrap() -> tuple[Settings, SsasClient]:
    """Create the settings + ADOMD client once. Runs on the worker thread."""
    global _settings, _client
    with _LOCK:
        if _settings is None:
            _settings = load_settings()
        if _client is None:
            _client = SsasClient(_settings)
    return _settings, _client


def _get_index(refresh: bool = False) -> tuple[Settings, ModelIndex]:
    """Return the model index, discovering it on first use."""
    global _index, _schema_text
    settings, client = _bootstrap()
    with _LOCK:
        if _index is None or refresh:
            _index = load_index(client, settings, use_cache=not refresh)
            _schema_text = None
        return settings, _index


def _get_schema_text(refresh: bool = False) -> str:
    global _schema_text
    settings, index = _get_index(refresh)
    with _LOCK:
        if _schema_text is None:
            _schema_text = build_schema(index, settings)
        return _schema_text


async def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Push a blocking call onto the single SSAS worker thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, functools.partial(fn, *args, **kwargs))


def _fail(exc: Exception) -> ToolError:
    """Turn an internal exception into a message the model can act on.

    ToolError is the only exception type whose text the SDK forwards to the
    client, so error details (a bad column name, an SSAS syntax error) reach
    the model and it can repair its own query instead of guessing.
    """
    return ToolError(f"{type(exc).__name__}: {truncate(exc, 1500)}")


# =========================================================
# SERVER
# =========================================================

INSTRUCTIONS = """\
Read-only access to a Microsoft SSAS Tabular (semantic) model.

Recommended workflow for any data question:
  1. list_tables() to see the model, or search_model_objects("revenue") to
     jump straight to the objects you need.
  2. describe_table("Sales") for the exact column and measure names, or
     get_model_schema() if you want the whole model at once.
  3. Write a bounded DAX query and run it with run_dax().
  4. Answer using ONLY the rows that came back. Never invent values.

Writes are impossible: the server rejects anything that is not a
DEFINE/EVALUATE query, and the connection itself should be read-only.
"""


# Built once, at import time: the decorators below register on this instance,
# so it must never be replaced afterwards. Host/port are applied in main().
mcp = create_server("ssas-tabular", INSTRUCTIONS)


# ---------------------------------------------------------
# TOOLS - health
# ---------------------------------------------------------

@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Check that the SSAS server is reachable and report the active configuration.

    Call this first if any other tool fails, to separate a connection problem
    from a query problem.
    """
    def work() -> dict[str, Any]:
        settings, client = _bootstrap()
        started = time.monotonic()
        client.ping()
        return {
            "status": "ok",
            "database": settings.ssas_database,
            "roundtrip_ms": round((time.monotonic() - started) * 1000, 1),
            "query_timeout_seconds": settings.query_timeout_seconds,
            "max_result_rows": settings.max_result_rows,
            "metadata_loaded": _index is not None,
        }

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ---------------------------------------------------------
# TOOLS - metadata
# ---------------------------------------------------------

@mcp.tool()
async def list_tables() -> dict[str, Any]:
    """List every visible table in the model with its column and measure counts.

    Cheap. Use it to orient yourself before asking for a full schema.
    """
    def work() -> dict[str, Any]:
        settings, index = _get_index()
        tables = [
            {
                "name": str(table.get("Name")),
                "dax_reference": dax_table_name(table.get("Name")),
                "columns": len(index.columns_of(table)),
                "measures": len(index.measures_of(table)),
                "hidden": get_bool(table.get("IsHidden")),
                "description": truncate(table.get("Description") or "", 200),
            }
            for table in index.tables
        ]
        return {
            "database": settings.ssas_database,
            "table_count": len(tables),
            "tables": tables,
        }

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool()
async def describe_table(table_name: str) -> dict[str, Any]:
    """Return the columns, measures and relationships of one table.

    Prefer this over get_model_schema when you already know which table you
    need: it is far smaller and always complete for that table.

    Args:
        table_name: table name as shown by list_tables, with or without quotes.
    """
    def work() -> dict[str, Any]:
        settings, index = _get_index()
        table = index.find_table(table_name)
        if table is None:
            candidates = [
                hit["name"] for hit in index.search(table_name, limit=10)
                if hit["kind"] == "table"
            ]
            raise ValueError(
                f"Table {table_name!r} was not found. "
                + (f"Did you mean: {', '.join(candidates)}?" if candidates
                   else "Call list_tables() to see the available tables.")
            )
        return {
            "table": str(table.get("Name")),
            "dax_reference": dax_table_name(table.get("Name")),
            "detail": build_table_detail(index, table, settings),
        }

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool()
async def list_measures(table_name: str | None = None) -> dict[str, Any]:
    """List measures with their DAX expressions.

    Args:
        table_name: restrict to one table. Omit for every measure in the model.
    """
    def work() -> dict[str, Any]:
        settings, index = _get_index()
        if table_name:
            table = index.find_table(table_name)
            if table is None:
                raise ValueError(f"Table {table_name!r} was not found.")
            tables = [table]
        else:
            tables = index.tables

        measures = []
        for table in tables:
            for measure in index.measures_of(table):
                expression = measure.get("Expression")
                measures.append({
                    "name": str(measure.get("Name")),
                    "home_table": str(table.get("Name")),
                    "dax_reference": f"[{measure.get('Name')}]",
                    "description": truncate(measure.get("Description") or "", 200),
                    "expression": truncate(
                        " ".join(str(expression).split()) if expression else "",
                        settings.max_expression_chars,
                    ),
                    "format": str(measure.get("FormatString") or ""),
                })
        return {"measure_count": len(measures), "measures": measures}

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool()
async def search_model_objects(query: str, limit: int = 50) -> dict[str, Any]:
    """Find tables, columns and measures whose name contains `query`.

    The fastest way to map a business word ("revenue", "customer", "fiscal")
    onto real objects without pulling the whole schema.

    Args:
        query: case-insensitive substring.
        limit: maximum number of hits (default 50).
    """
    def work() -> dict[str, Any]:
        _, index = _get_index()
        hits = index.search(query, limit=max(1, min(limit, 200)))
        return {"query": query, "hit_count": len(hits), "hits": hits}

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool()
async def list_relationships() -> dict[str, Any]:
    """List the relationships of the model, with cardinality and filter direction.

    Use it to check that a join you are relying on actually exists and is active.
    """
    def work() -> dict[str, Any]:
        _, index = _get_index()
        lines = [
            line for line in
            (index.describe_relationship(r) for r in index.relationships)
            if line
        ]
        return {"relationship_count": len(lines), "relationships": lines}

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool()
async def get_model_schema(refresh: bool = False) -> str:
    """Return the full model schema as text: tables, columns, measures, relationships.

    Large on big models and bounded by SCHEMA_CHAR_BUDGET. If you only need one
    or two tables, describe_table is cheaper and never truncates.

    Args:
        refresh: re-read the metadata from the server, ignoring the cache.
    """
    try:
        return await _run(_get_schema_text, refresh)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool()
async def refresh_metadata() -> dict[str, Any]:
    """Re-read the model metadata from the server.

    Call this after the model has been redeployed or a measure has been added.
    """
    def work() -> dict[str, Any]:
        _, index = _get_index(refresh=True)
        return {
            "status": "refreshed",
            "tables": len(index.tables),
            "columns": index.column_count,
            "measures": index.measure_count,
            "relationships": len(index.relationships),
        }

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ---------------------------------------------------------
# TOOLS - execution
# ---------------------------------------------------------

def _execute(dax: str, max_rows: int | None) -> dict[str, Any]:
    """Validate, execute and bound one DAX query. Runs on the worker thread."""
    settings, client = _bootstrap()

    limit = settings.default_result_rows if max_rows is None else max_rows
    limit = max(1, min(int(limit), settings.max_result_rows))

    validate_dax(dax, max_chars=settings.max_dax_chars)

    started = time.monotonic()
    result = client.run_query(dax, max_rows=limit)
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    # Bound the payload by characters as well as by rows: 500 rows of a wide
    # table can still be megabytes, and it all lands in the host's context.
    rows: list[dict[str, Any]] = []
    size = 0
    char_truncated = False
    for row in result.rows:
        record = dict(zip(result.columns, row))
        encoded = len(json.dumps(record, default=str, ensure_ascii=False))
        if size + encoded > settings.max_result_chars:
            char_truncated = True
            break
        rows.append(record)
        size += encoded

    truncated = result.truncated or char_truncated
    payload: dict[str, Any] = {
        "columns": result.columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "row_limit": limit,
        "elapsed_ms": elapsed_ms,
        "dax": dax,
    }
    if truncated:
        payload["note"] = (
            "The result was cut off. Say so in your answer, or re-run with a "
            "tighter filter, an aggregation, or a higher max_rows (ceiling: "
            f"{settings.max_result_rows})."
        )
    if not rows:
        payload["note"] = "The query succeeded but returned no rows."
    return payload


@mcp.tool()
async def run_dax(dax: str, max_rows: int | None = None) -> dict[str, Any]:
    """Execute a read-only DAX query against the model and return the rows.

    The query must start with DEFINE or EVALUATE and contain EVALUATE. Anything
    that looks like a write, a DMV read, XMLA/TMSL or multiple statements is
    rejected before it reaches the server.

    Always bound your query (TOPN, filters, aggregation). The server caps rows
    too, but an unbounded query still costs the cube time and memory.

    Args:
        dax: the DAX query.
        max_rows: rows to return; clamped to the server ceiling.

    Returns:
        columns, rows, row_count, truncated, elapsed_ms and the DAX that ran.
    """
    try:
        return await _run(_execute, dax, max_rows)
    except DaxValidationError as exc:
        raise ToolError(
            f"DAX rejected: {exc} Fix the query and try again; do not attempt to "
            "bypass the check."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Server-side DAX errors come back verbatim so the model can self-repair.
        raise _fail(exc) from exc


@mcp.tool()
async def preview_table(table_name: str, rows: int = 10) -> dict[str, Any]:
    """Return a small sample of rows from one table.

    Useful for seeing how values are actually spelled before you filter on them.

    Args:
        table_name: table name as shown by list_tables.
        rows: number of sample rows (default 10).
    """
    def work() -> dict[str, Any]:
        _, index = _get_index()
        table = index.find_table(table_name)
        if table is None:
            raise ValueError(f"Table {table_name!r} was not found.")
        quoted = dax_table_name(table.get("Name"))
        count = max(1, min(int(rows), 100))
        return _execute(f"EVALUATE TOPN({count}, {quoted})", count)

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@mcp.tool()
async def get_column_values(
    table_name: str, column_name: str, limit: int = 50
) -> dict[str, Any]:
    """Return the distinct values of one column.

    Call this before writing a filter on a text column so you use the exact
    spelling stored in the model instead of guessing.

    Args:
        table_name: table name as shown by list_tables.
        column_name: column name as shown by describe_table.
        limit: maximum distinct values (default 50).
    """
    def work() -> dict[str, Any]:
        _, index = _get_index()
        table = index.find_table(table_name)
        if table is None:
            raise ValueError(f"Table {table_name!r} was not found.")

        wanted = column_name.strip().strip("[]").lower()
        match = next(
            (c for c in index.columns_of(table)
             if get_metadata_column_name(c).lower() == wanted),
            None,
        )
        if match is None:
            available = [get_metadata_column_name(c) for c in index.columns_of(table)]
            raise ValueError(
                f"Column {column_name!r} was not found in {table_name!r}. "
                f"Available: {', '.join(available[:40])}"
            )

        real_name = get_metadata_column_name(match)
        quoted = dax_table_name(table.get("Name"))
        count = max(1, min(int(limit), 1000))
        dax = (
            f"EVALUATE TOPN({count}, "
            f"VALUES({quoted}[{real_name}]), {quoted}[{real_name}], ASC)"
        )
        payload = _execute(dax, count)
        payload["data_type"] = get_column_data_type(match)
        return payload

    try:
        return await _run(work)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ---------------------------------------------------------
# RESOURCES
# ---------------------------------------------------------

@mcp.resource("ssas://model/schema", mime_type="text/plain")
async def resource_schema() -> str:
    """The full model schema as text."""
    return await _run(_get_schema_text, False)


@mcp.resource("ssas://model/tables", mime_type="application/json")
async def resource_tables() -> str:
    """The table list as JSON."""
    def work() -> str:
        _, index = _get_index()
        return json.dumps(
            [
                {
                    "name": str(t.get("Name")),
                    "columns": len(index.columns_of(t)),
                    "measures": len(index.measures_of(t)),
                }
                for t in index.tables
            ],
            ensure_ascii=False,
            indent=2,
        )

    return await _run(work)


@mcp.resource("ssas://table/{table_name}", mime_type="text/plain")
async def resource_table(table_name: str) -> str:
    """One table's columns, measures and relationships."""
    def work() -> str:
        settings, index = _get_index()
        table = index.find_table(table_name)
        if table is None:
            return f"Table {table_name!r} was not found."
        return build_table_detail(index, table, settings)

    return await _run(work)


# ---------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------

@mcp.prompt()
def dax_guidelines() -> str:
    """House rules for writing DAX against this model."""
    return DAX_RULES


@mcp.prompt()
def analyze(question: str) -> str:
    """Answer a business question against the SSAS model, end to end."""
    return f"""\
Answer this question using the SSAS Tabular model exposed by this server.

Question:
\"\"\"{question}\"\"\"

Procedure:
1. Discover the real objects with search_model_objects / list_tables /
   describe_table. Do not assume any table, column or measure exists.
2. If the question filters on text values, confirm the spelling with
   get_column_values first.
3. Write one bounded DAX query and run it with run_dax.
4. If the server returns an error, read it, fix the query and retry once or
   twice. Do not fall back to guessing numbers.
5. Answer from the returned rows only. Format numbers clearly, state the units
   and the period, and say so if the result was truncated.

{DAX_RULES}
"""


# ---------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SSAS Tabular MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=None,
        help="Transport to serve on (default: stdio, or MCP_TRANSPORT).",
    )
    parser.add_argument("--host", default=None, help="Bind host for HTTP transports.")
    parser.add_argument("--port", type=int, default=None, help="Bind port for HTTP.")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--preload",
        action="store_true",
        help="Read the model metadata at start-up instead of on first use.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Connect, read the metadata, print a summary to stderr and exit.",
    )
    args = parser.parse_args(argv)

    # stderr only: on stdio, stdout is the protocol channel.
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    try:
        settings = load_settings()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    global _settings
    overrides: dict[str, Any] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if overrides:
        settings = dataclasses.replace(settings, **overrides)
    _settings = settings


    if args.check:
        try:
            _, index = _get_index(refresh=True)
        except Exception as error:  # noqa: BLE001
            LOGGER.error("Check failed: %s", error)
            return 1
        LOGGER.info(
            "OK - %s tables, %s columns, %s measures, %s relationships.",
            len(index.tables), index.column_count, index.measure_count,
            len(index.relationships),
        )
        return 0

    if args.preload:
        try:
            _get_index()
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Metadata preload failed (%s); will retry on demand.", error)

    transport = args.transport or settings.transport
    LOGGER.info(
        "Starting SSAS MCP server (%s) for %s / %s",
        transport, settings.ssas_server, settings.ssas_database,
    )
    try:
        run_server(mcp, transport, settings.host, settings.port)
    finally:
        _EXECUTOR.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())