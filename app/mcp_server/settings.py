"""Environment configuration for one database-scoped SSAS MCP subprocess."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

LOGGER = logging.getLogger("ssas_mcp")


@dataclass(frozen=True)
class Settings:
    ssas_server: str
    ssas_database: str
    adomd_path: str
    connection_string_override: str | None = None

    max_result_rows: int = 500
    default_result_rows: int = 100
    max_result_chars: int = 200_000
    max_metadata_rows: int = 200_000

    schema_char_budget: int = 120_000
    max_columns_per_table: int = 200
    max_measures_per_table: int = 200
    max_expression_chars: int = 400
    include_hidden: bool = False

    connect_timeout_seconds: int = 30
    query_timeout_seconds: int = 120
    request_memory_limit_kb: int = 0
    application_name: str = "SSAS-MCP-Server"
    max_dax_chars: int = 20_000

    schema_cache_path: str | None = None
    schema_cache_ttl_seconds: int = 24 * 3600

    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_settings() -> Settings:
    # Parent process loads .env and injects the selected database into this
    # subprocess. override=False ensures the injected values always win.
    try:
        from dotenv import load_dotenv
    except ImportError:
        LOGGER.debug("python-dotenv not installed; reading environment only.")
    else:
        load_dotenv(override=False)

    required = {
        "SSAS_SERVER": os.getenv("SSAS_SERVER"),
        "SSAS_DATABASE": os.getenv("SSAS_DATABASE"),
        "ADOMD_PATH": os.getenv("ADOMD_PATH"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing configuration: " + ", ".join(missing)
            + ". The parent application must inject them into the MCP subprocess."
        )

    return Settings(
        ssas_server=str(required["SSAS_SERVER"]),
        ssas_database=str(required["SSAS_DATABASE"]),
        adomd_path=str(required["ADOMD_PATH"]),
        connection_string_override=os.getenv("SSAS_CONNECTION_STRING") or None,
        max_result_rows=_env_int("MAX_RESULT_ROWS", 500),
        default_result_rows=_env_int("DEFAULT_RESULT_ROWS", 100),
        max_result_chars=_env_int("MAX_RESULT_CHARS", 200_000),
        max_metadata_rows=_env_int("MAX_METADATA_ROWS", 200_000),
        schema_char_budget=_env_int("SCHEMA_CHAR_BUDGET", 120_000),
        max_columns_per_table=_env_int("MAX_COLUMNS_PER_TABLE", 200),
        max_measures_per_table=_env_int("MAX_MEASURES_PER_TABLE", 200),
        max_expression_chars=_env_int("MAX_EXPRESSION_CHARS", 400),
        include_hidden=_env_bool("INCLUDE_HIDDEN", False),
        connect_timeout_seconds=_env_int("CONNECT_TIMEOUT_SECONDS", 30),
        query_timeout_seconds=_env_int("QUERY_TIMEOUT_SECONDS", 120),
        request_memory_limit_kb=_env_int("REQUEST_MEMORY_LIMIT_KB", 0),
        application_name=os.getenv("APPLICATION_NAME", "SSAS-MCP-Server"),
        max_dax_chars=_env_int("MAX_DAX_CHARS", 20_000),
        schema_cache_path=os.getenv("SCHEMA_CACHE_PATH") or None,
        schema_cache_ttl_seconds=_env_int("SCHEMA_CACHE_TTL_SECONDS", 24 * 3600),
        transport=os.getenv("MCP_TRANSPORT", "stdio"),
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=_env_int("MCP_PORT", 8000),
    )
