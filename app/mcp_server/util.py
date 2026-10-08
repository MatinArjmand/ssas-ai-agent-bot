"""Small conversion helpers shared by the client and the metadata layer."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def clean_column_name(name: Any) -> str:
    """`[Name]` / `Table[Name]` / `Name` -> `Name`."""
    text = str(name)
    if "[" in text and text.endswith("]"):
        return text.rsplit("[", 1)[1][:-1]
    return text.strip("[]")


def dax_table_name(name: Any) -> str:
    """Quote a table name so it is safe inside a DAX expression."""
    return "'" + str(name).replace("'", "''") + "'"


def get_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes")


def norm_id(value: Any) -> Any:
    """Normalize TMSCHEMA IDs.

    ADOMD can hand back .NET Int64 / Guid / str for the same logical ID
    depending on the provider build, which silently breaks dict lookups
    between TABLES.ID and COLUMNS.TableID. Normalizing both sides fixes it.
    """
    if value is None:
        return None
    as_int = get_int(value)
    return as_int if as_int is not None else str(value)


def to_jsonable(value: Any) -> Any:
    """Convert a cell coming out of ADOMD into something json.dumps can eat."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value)  # System.DBNull and friends
    if text in ("", "DBNull", "System.DBNull"):
        return None
    return text


def truncate(text: Any, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def format_table(rows: list[dict[str, Any]]) -> str:
    """Render rows as a plain text table (handy for human-readable output)."""
    if not rows:
        return "(no rows)"
    headers = list(rows[0].keys())
    widths = {
        h: max(len(str(h)), *(len(str(r.get(h, ""))) for r in rows)) for h in headers
    }
    lines = [" | ".join(str(h).ljust(widths[h]) for h in headers)]
    lines.append("-+-".join("-" * widths[h] for h in headers))
    for row in rows:
        lines.append(" | ".join(str(row.get(h, "")).ljust(widths[h]) for h in headers))
    return "\n".join(lines)