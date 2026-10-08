"""Read the Tabular model metadata, index it in O(n), and render it as text.

NOTE: the TMSCHEMA_* DMVs require elevated rights on the database
(Read Definition or admin). If the query account is read-only, build the cache
once from an account that can read definitions (SCHEMA_CACHE_PATH) and run
day-to-day queries with the least-privilege account.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .client import SsasClient
from .settings import Settings
from .util import dax_table_name, get_bool, get_int, norm_id, truncate

LOGGER = logging.getLogger("ssas_mcp.metadata")

DATA_TYPES = {
    1: "Automatic", 2: "String", 6: "Int64", 8: "Double", 9: "DateTime",
    10: "Decimal", 11: "Boolean", 17: "Binary", 19: "Unknown", 20: "Variant",
}
CARDINALITIES = {0: "None", 1: "One", 2: "Many"}
CROSS_FILTERING = {1: "OneDirection", 2: "BothDirections", 3: "Automatic"}
COLUMN_TYPE_ROW_NUMBER = 3

# Explicit column lists: SELECT * on TMSCHEMA_COLUMNS of a large model drags
# back dozens of unused wide columns (expressions, annotations, lineage tags).
DMV_SOURCES: dict[str, tuple[str, list[str]]] = {
    "tables": (
        "$SYSTEM.TMSCHEMA_TABLES",
        ["ID", "Name", "Description", "IsHidden", "IsPrivate", "SystemManaged"],
    ),
    "columns": (
        "$SYSTEM.TMSCHEMA_COLUMNS",
        ["ID", "TableID", "ExplicitName", "InferredName", "SourceColumn",
         "ExplicitDataType", "InferredDataType", "Type", "IsHidden",
         "IsKey", "IsUnique", "Description"],
    ),
    "measures": (
        "$SYSTEM.TMSCHEMA_MEASURES",
        ["ID", "TableID", "Name", "Description", "Expression",
         "DataType", "IsHidden", "FormatString"],
    ),
    "relationships": (
        "$SYSTEM.TMSCHEMA_RELATIONSHIPS",
        ["ID", "Name", "FromTableID", "FromColumnID", "ToTableID", "ToColumnID",
         "FromCardinality", "ToCardinality", "CrossFilteringBehavior", "IsActive"],
    ),
}


# =========================================================
# DISCOVERY
# =========================================================

def _dmv_columns(client: SsasClient, dmv: str) -> dict[str, str]:
    """Ask the server which columns this DMV actually has.

    Read with max_rows=1; the streaming reader stops after the first row, so
    this costs nothing even on a large DMV, and the column schema comes back
    regardless of how many rows exist.
    """
    result = client.run_query(f"SELECT * FROM {dmv}", max_rows=1)
    return {name.lower(): name for name in result.columns}


def _build_dmv_query(dmv: str, wanted: list[str], available: dict[str, str]) -> str:
    picked = [available[w.lower()] for w in wanted if w.lower() in available]
    missing = [w for w in wanted if w.lower() not in available]
    if missing:
        LOGGER.info(
            "%s does not expose %s on this server; continuing without it.",
            dmv, ", ".join(missing),
        )
    if not picked:
        return f"SELECT * FROM {dmv}"
    return "SELECT " + ", ".join(f"[{c}]" for c in picked) + f" FROM {dmv}"


def discover_model(client: SsasClient) -> dict[str, list[dict[str, Any]]]:
    LOGGER.info(
        "Reading metadata from %s / %s ...",
        client.settings.ssas_server, client.settings.ssas_database,
    )
    metadata: dict[str, list[dict[str, Any]]] = {}
    for key, (dmv, wanted) in DMV_SOURCES.items():
        try:
            available = _dmv_columns(client, dmv)
            query = _build_dmv_query(dmv, wanted, available)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Could not probe %s (%s); using SELECT *.", dmv, exc)
            query = f"SELECT * FROM {dmv}"
        metadata[key] = client.fetch_rowset(query)
    return metadata


# =========================================================
# FIELD READERS
# =========================================================

def get_metadata_column_name(column: dict[str, Any]) -> str:
    return (
        column.get("ExplicitName")
        or column.get("InferredName")
        or column.get("SourceColumn")
        or "(unnamed column)"
    )


def get_column_data_type(column: dict[str, Any]) -> str:
    explicit = get_int(column.get("ExplicitDataType"))
    inferred = get_int(column.get("InferredDataType"))
    value = inferred if explicit in (None, 1) else explicit
    if value is None:
        return "Unknown"
    return DATA_TYPES.get(value, f"DataType({value})")


def is_user_column(column: dict[str, Any]) -> bool:
    """ColumnType: 1=Data, 2=Calculated, 3=RowNumber, 4=CalculatedTableColumn.
    RowNumber columns are SSAS internals and must never reach a prompt.
    """
    return get_int(column.get("Type")) != COLUMN_TYPE_ROW_NUMBER


def select_user_tables(
    tables: Iterable[dict[str, Any]], include_hidden: bool
) -> list[dict[str, Any]]:
    selected = []
    for table in tables:
        if get_bool(table.get("IsPrivate")) or get_bool(table.get("SystemManaged")):
            continue
        if not table.get("Name"):
            continue
        if not include_hidden and get_bool(table.get("IsHidden")):
            continue
        selected.append(table)
    return selected


# =========================================================
# INDEX
# =========================================================

@dataclass
class ModelIndex:
    """Pre-computed lookups. Built once, in O(n)."""

    tables: list[dict[str, Any]]
    tables_by_id: dict[Any, dict[str, Any]]
    tables_by_name: dict[str, dict[str, Any]]
    columns_by_id: dict[Any, dict[str, Any]]
    columns_by_table: dict[Any, list[dict[str, Any]]]
    measures_by_table: dict[Any, list[dict[str, Any]]]
    relationships: list[dict[str, Any]]
    built_at: float = field(default_factory=time.time)

    @property
    def column_count(self) -> int:
        return sum(len(v) for v in self.columns_by_table.values())

    @property
    def measure_count(self) -> int:
        return sum(len(v) for v in self.measures_by_table.values())

    # -- lookups ---------------------------------------------------------

    def find_table(self, name: str) -> dict[str, Any] | None:
        return self.tables_by_name.get(str(name).strip().strip("'").lower())

    def columns_of(self, table: dict[str, Any]) -> list[dict[str, Any]]:
        return self.columns_by_table.get(norm_id(table.get("ID")), [])

    def measures_of(self, table: dict[str, Any]) -> list[dict[str, Any]]:
        return self.measures_by_table.get(norm_id(table.get("ID")), [])

    def relationships_of(self, table: dict[str, Any]) -> list[dict[str, Any]]:
        table_id = norm_id(table.get("ID"))
        return [
            r for r in self.relationships
            if norm_id(r.get("FromTableID")) == table_id
            or norm_id(r.get("ToTableID")) == table_id
        ]

    def describe_relationship(self, relationship: dict[str, Any]) -> str | None:
        from_table = self.tables_by_id.get(norm_id(relationship.get("FromTableID")))
        to_table = self.tables_by_id.get(norm_id(relationship.get("ToTableID")))
        from_column = self.columns_by_id.get(norm_id(relationship.get("FromColumnID")))
        to_column = self.columns_by_id.get(norm_id(relationship.get("ToColumnID")))
        if not (from_table and to_table and from_column and to_column):
            return None

        from_cardinality = CARDINALITIES.get(
            get_int(relationship.get("FromCardinality")),
            str(relationship.get("FromCardinality")),
        )
        to_cardinality = CARDINALITIES.get(
            get_int(relationship.get("ToCardinality")),
            str(relationship.get("ToCardinality")),
        )
        cross_filter = CROSS_FILTERING.get(
            get_int(relationship.get("CrossFilteringBehavior")),
            str(relationship.get("CrossFilteringBehavior")),
        )
        return (
            f"{relationship.get('Name') or '(unnamed relationship)'}: "
            f"{dax_table_name(from_table.get('Name'))}"
            f"[{get_metadata_column_name(from_column)}] ({from_cardinality}) -> "
            f"{dax_table_name(to_table.get('Name'))}"
            f"[{get_metadata_column_name(to_column)}] ({to_cardinality}); "
            f"active={get_bool(relationship.get('IsActive'))}; "
            f"filtering={cross_filter}"
        )

    def search(self, needle: str, limit: int = 50) -> list[dict[str, str]]:
        """Case-insensitive substring search over tables, columns and measures."""
        needle = needle.strip().lower()
        hits: list[dict[str, str]] = []
        if not needle:
            return hits

        for table in self.tables:
            table_name = str(table.get("Name"))
            if needle in table_name.lower():
                hits.append({
                    "kind": "table",
                    "table": table_name,
                    "name": table_name,
                    "reference": dax_table_name(table_name),
                    "detail": truncate(table.get("Description") or "", 160),
                })
            for column in self.columns_of(table):
                column_name = get_metadata_column_name(column)
                if needle in column_name.lower():
                    hits.append({
                        "kind": "column",
                        "table": table_name,
                        "name": column_name,
                        "reference": f"{dax_table_name(table_name)}[{column_name}]",
                        "detail": get_column_data_type(column),
                    })
            for measure in self.measures_of(table):
                measure_name = str(measure.get("Name"))
                if needle in measure_name.lower():
                    hits.append({
                        "kind": "measure",
                        "table": table_name,
                        "name": measure_name,
                        "reference": f"[{measure_name}]",
                        "detail": truncate(measure.get("Description") or "", 160),
                    })
            if len(hits) >= limit:
                break
        return hits[:limit]


def build_index(
    metadata: dict[str, list[dict[str, Any]]], settings: Settings
) -> ModelIndex:
    """One pass with a defaultdict: O(columns), not O(tables x columns)."""
    tables = select_user_tables(metadata["tables"], settings.include_hidden)
    tables_by_id = {norm_id(t.get("ID")): t for t in tables if t.get("ID") is not None}
    tables_by_name = {str(t.get("Name")).lower(): t for t in tables}

    columns_by_id: dict[Any, dict[str, Any]] = {}
    columns_by_table: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for column in metadata["columns"]:
        column_id = norm_id(column.get("ID"))
        if column_id is not None:
            columns_by_id[column_id] = column
        if not is_user_column(column):
            continue
        if not settings.include_hidden and get_bool(column.get("IsHidden")):
            continue
        table_id = norm_id(column.get("TableID"))
        if table_id in tables_by_id:
            columns_by_table[table_id].append(column)

    measures_by_table: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for measure in metadata["measures"]:
        if not measure.get("Name"):
            continue
        if not settings.include_hidden and get_bool(measure.get("IsHidden")):
            continue
        table_id = norm_id(measure.get("TableID"))
        if table_id in tables_by_id:
            measures_by_table[table_id].append(measure)

    relationships = [
        r for r in metadata["relationships"]
        if norm_id(r.get("FromTableID")) in tables_by_id
        and norm_id(r.get("ToTableID")) in tables_by_id
    ]

    return ModelIndex(
        tables=sorted(tables, key=lambda t: str(t.get("Name", ""))),
        tables_by_id=tables_by_id,
        tables_by_name=tables_by_name,
        columns_by_id=columns_by_id,
        columns_by_table=dict(columns_by_table),
        measures_by_table=dict(measures_by_table),
        relationships=relationships,
    )


# =========================================================
# SCHEMA TEXT (bounded)
# =========================================================

class BoundedLines:
    """Line accumulator that stops once the character budget is spent."""

    MARKER = (
        "... [schema truncated: character budget reached. Use describe_table / "
        "search_model_objects for the rest, or raise SCHEMA_CHAR_BUDGET.]"
    )

    def __init__(self, budget: int):
        # Reserve room for the marker so the final text never exceeds `budget`.
        self.budget = max(len(self.MARKER) + 1, budget) - (len(self.MARKER) + 1)
        self.size = 0
        self.lines: list[str] = []
        self.overflowed = False

    def add(self, line: str = "") -> bool:
        if self.overflowed:
            return False
        if self.size + len(line) + 1 > self.budget:
            self.overflowed = True
            self.lines.append(self.MARKER)
            return False
        self.lines.append(line)
        self.size += len(line) + 1
        return True

    def text(self) -> str:
        return "\n".join(self.lines)


def render_table(out: BoundedLines, index: ModelIndex, table: dict[str, Any],
                 settings: Settings) -> None:
    table_name = table.get("Name")
    quoted = dax_table_name(table_name)
    out.add(f"TABLE: {quoted}")

    if table.get("Description"):
        out.add(f"Description: {truncate(table['Description'], 300)}")
    if get_bool(table.get("IsHidden")):
        out.add("Table visibility: Hidden")

    table_columns = index.columns_of(table)
    out.add("Columns:")
    if not table_columns:
        out.add("- No columns discovered")

    shown = table_columns[: settings.max_columns_per_table]
    for column in shown:
        name = get_metadata_column_name(column)
        data_type = get_column_data_type(column)
        attributes = []
        if get_bool(column.get("IsKey")):
            attributes.append("key")
        if get_bool(column.get("IsHidden")):
            attributes.append("hidden")
        if get_bool(column.get("IsUnique")):
            attributes.append("unique")
        attribute_text = f" [{', '.join(attributes)}]" if attributes else ""
        out.add(f"- {quoted}[{name}] : {data_type}{attribute_text}")
        if column.get("Description"):
            out.add("  Description: " + truncate(column["Description"], 200))

    if len(table_columns) > len(shown):
        out.add(f"- ... and {len(table_columns) - len(shown)} more columns (not shown)")

    table_measures = index.measures_of(table)
    if table_measures:
        out.add("Measures:")
        for measure in table_measures[: settings.max_measures_per_table]:
            measure_type = get_int(measure.get("DataType"))
            type_name = (
                DATA_TYPES.get(measure_type, f"DataType({measure_type})")
                if measure_type is not None else "Unknown"
            )
            hidden_text = " [hidden]" if get_bool(measure.get("IsHidden")) else ""
            out.add(f"- [{measure.get('Name')}] : {type_name}{hidden_text}")
            if measure.get("Description"):
                out.add("  Description: " + truncate(measure["Description"], 200))
            if measure.get("Expression"):
                expression = " ".join(str(measure["Expression"]).split())
                out.add(
                    "  Expression: "
                    + truncate(expression, settings.max_expression_chars)
                )
            if measure.get("FormatString"):
                out.add("  Format: " + str(measure["FormatString"]))


def build_schema(index: ModelIndex, settings: Settings) -> str:
    out = BoundedLines(settings.schema_char_budget)
    out.add(f"DATABASE: {settings.ssas_database}")
    out.add()
    out.add("This schema was read live from the SSAS Tabular model metadata.")
    out.add()

    for table in index.tables:
        render_table(out, index, table, settings)
        out.add()

    out.add("RELATIONSHIPS:")
    written = 0
    for relationship in index.relationships:
        line = index.describe_relationship(relationship)
        if line:
            out.add("- " + line)
            written += 1
    if written == 0:
        out.add("- No relationships discovered")

    if out.overflowed:
        LOGGER.warning(
            "Schema text hit the %s character budget and was truncated. "
            "Consider hiding unused tables/columns in the model.",
            settings.schema_char_budget,
        )
    return out.text()


def build_table_detail(index: ModelIndex, table: dict[str, Any],
                       settings: Settings) -> str:
    out = BoundedLines(settings.schema_char_budget)
    render_table(out, index, table, settings)
    related = index.relationships_of(table)
    out.add()
    out.add("RELATIONSHIPS involving this table:")
    written = 0
    for relationship in related:
        line = index.describe_relationship(relationship)
        if line:
            out.add("- " + line)
            written += 1
    if written == 0:
        out.add("- none")
    return out.text()


# =========================================================
# CACHE
# =========================================================

def read_cache(settings: Settings) -> dict[str, Any] | None:
    path = settings.schema_cache_path
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read schema cache: %s", exc)
        return None

    if (cached.get("server") != settings.ssas_server
            or cached.get("database") != settings.ssas_database):
        return None
    age = time.time() - float(cached.get("generated_at", 0))
    if age > settings.schema_cache_ttl_seconds:
        LOGGER.info("Schema cache expired (%.0f minutes old).", age / 60)
        return None

    LOGGER.info("Using cached metadata (%.0f minutes old).", age / 60)
    return cached.get("metadata")


def write_cache(settings: Settings, metadata: dict[str, Any]) -> None:
    path = settings.schema_cache_path
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "generated_at": time.time(),
                    "server": settings.ssas_server,
                    "database": settings.ssas_database,
                    "metadata": metadata,
                },
                handle,
                ensure_ascii=False,
                default=str,
            )
    except OSError as exc:
        LOGGER.warning("Could not write schema cache: %s", exc)


def load_index(client: SsasClient, settings: Settings,
               use_cache: bool = True) -> ModelIndex:
    metadata = read_cache(settings) if use_cache else None
    if metadata is None:
        metadata = discover_model(client)
        write_cache(settings, metadata)

    index = build_index(metadata, settings)
    LOGGER.info(
        "Model indexed: %s tables, %s columns, %s measures, %s relationships.",
        len(index.tables), index.column_count, index.measure_count,
        len(index.relationships),
    )
    return index