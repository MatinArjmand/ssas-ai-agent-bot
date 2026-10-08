"""Synchronous ADOMD.NET wrapper used only inside the MCP worker thread."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from .settings import Settings
from .util import clean_column_name, to_jsonable

LOGGER = logging.getLogger("ssas_mcp.client")


def load_adomd(adomd_path: str):
    if not os.path.isdir(adomd_path):
        raise RuntimeError(f"ADOMD_PATH does not exist or is not a directory: {adomd_path}")
    if adomd_path not in sys.path:
        sys.path.append(adomd_path)
    try:
        from pyadomd import Pyadomd  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "Could not import pyadomd. Check pythonnet and Microsoft.AnalysisServices.AdomdClient.dll "
            f"in {adomd_path}."
        ) from exc
    return Pyadomd


def _adomd_command_class():
    try:
        from Microsoft.AnalysisServices.AdomdClient import AdomdCommand  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return AdomdCommand


class QueryResult:
    __slots__ = ("columns", "rows", "truncated")

    def __init__(self, columns: list[str], rows: list[tuple], truncated: bool):
        self.columns = columns
        self.rows = rows
        self.truncated = truncated

    def as_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


def _connection_value(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class SsasClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pyadomd = load_adomd(settings.adomd_path)
        self.connection_string = self._build_connection_string(settings)

    @staticmethod
    def _build_connection_string(settings: Settings) -> str:
        if settings.connection_string_override:
            value = settings.connection_string_override.strip()
            return value if value.endswith(";") else value + ";"
        parts = [
            "Provider=MSOLAP",
            f"Data Source={_connection_value(settings.ssas_server)}",
            f"Catalog={_connection_value(settings.ssas_database)}",
            f"Connect Timeout={settings.connect_timeout_seconds}",
            f"Application Name={settings.application_name}",
        ]
        if settings.request_memory_limit_kb > 0:
            parts.append(f"DbpropMsmdRequestMemoryLimit={settings.request_memory_limit_kb}")
        return ";".join(parts) + ";"

    def run_query(self, query: str, max_rows: int | None = None) -> QueryResult:
        with self._pyadomd(self.connection_string) as connection:
            fast_path = self._run_with_adomd_command(connection, query, max_rows)
            if fast_path is not None:
                return fast_path
            return self._run_with_pyadomd_cursor(connection, query, max_rows)

    def ping(self) -> bool:
        self.run_query('EVALUATE ROW("ok", 1)', max_rows=1)
        return True

    def _run_with_adomd_command(self, connection: Any, query: str, max_rows: int | None) -> QueryResult | None:
        command_class = _adomd_command_class()
        raw_connection = getattr(connection, "conn", None)
        if command_class is None or raw_connection is None:
            return None
        reader = None
        try:
            command = command_class(query, raw_connection)
            command.CommandTimeout = self.settings.query_timeout_seconds
            reader = command.ExecuteReader()
            field_count = int(reader.FieldCount)
            columns = [clean_column_name(reader.GetName(i)) for i in range(field_count)]
            rows: list[tuple] = []
            truncated = False
            while reader.Read():
                if max_rows is not None and len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(tuple(
                    None if reader.IsDBNull(i) else to_jsonable(reader.GetValue(i))
                    for i in range(field_count)
                ))
            return QueryResult(columns, rows, truncated)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (AttributeError, TypeError)):
                LOGGER.debug("AdomdCommand fast path unavailable: %s", exc)
                return None
            raise
        finally:
            if reader is not None:
                try:
                    reader.Close()
                except Exception:  # noqa: BLE001
                    pass

    def _run_with_pyadomd_cursor(self, connection: Any, query: str, max_rows: int | None) -> QueryResult:
        with connection.cursor().execute(query) as cursor:
            columns = [clean_column_name(c.name) for c in cursor.description]
            rows: list[tuple] = []
            truncated = False
            batch_size = 1_000
            if hasattr(cursor, "fetchmany"):
                while max_rows is None or len(rows) < max_rows:
                    want = batch_size if max_rows is None else min(batch_size, max_rows - len(rows))
                    batch = cursor.fetchmany(want)
                    if not batch:
                        break
                    rows.extend(batch)
                    if len(batch) < want:
                        break
                if max_rows is not None and len(rows) >= max_rows:
                    truncated = bool(cursor.fetchmany(1))
            else:
                LOGGER.warning("pyadomd cursor has no fetchmany(); falling back to fetchall().")
                all_rows = cursor.fetchall()
                if max_rows is not None and len(all_rows) > max_rows:
                    truncated = True
                    all_rows = all_rows[:max_rows]
                rows = list(all_rows)
            rows = [tuple(to_jsonable(v) for v in row) for row in rows]
            return QueryResult(columns, rows, truncated)

    def fetch_rowset(self, query: str) -> list[dict[str, Any]]:
        result = self.run_query(query, max_rows=self.settings.max_metadata_rows)
        if result.truncated:
            LOGGER.warning("Metadata rowset truncated at %s rows; schema may be incomplete.", self.settings.max_metadata_rows)
        return result.as_records()
