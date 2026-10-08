import os
import sys
import json
from pathlib import Path

from dotenv import load_dotenv
from agent_knowledge import format_knowledge_base
from ai_provider import AIClient, load_ai_settings


# =========================================================
# LOAD CONFIGURATION
# =========================================================

load_dotenv(Path(__file__).resolve().parent / ".env")
AI_SETTINGS = load_ai_settings()

SSAS_SERVER = os.getenv("SSAS_SERVER")
SSAS_DATABASE = os.getenv("SSAS_DATABASE")
ADOMD_PATH = os.getenv("ADOMD_PATH")


required_settings = {
    "ADOMD_PATH": ADOMD_PATH,
}

missing_settings = [
    name
    for name, value in required_settings.items()
    if not value
]

if missing_settings:
    raise RuntimeError(
        "Missing settings in .env: "
        + ", ".join(missing_settings)
    )


# =========================================================
# LOAD ADOMD.NET
#
# This MUST happen before importing pyadomd.
# =========================================================

sys.path.append(ADOMD_PATH)

from pyadomd import Pyadomd


# =========================================================
# SELECTED AI PROVIDER
# =========================================================

ai_client = AIClient(AI_SETTINGS)


# =========================================================
# SSAS CONNECTION
# =========================================================

CONNECTION_STRING = (
    "Provider=MSOLAP;"
    f"Data Source={SSAS_SERVER};"
    f"Catalog={SSAS_DATABASE};"
) if SSAS_SERVER and SSAS_DATABASE else None


def resolve_connection_string(connection_string=None):
    """Explicit per-request targets take precedence; CLI keeps its .env default."""
    resolved = connection_string if connection_string is not None else CONNECTION_STRING
    if not resolved:
        raise RuntimeError(
            "Supply a database connection or set SSAS_SERVER and SSAS_DATABASE in .env."
        )
    return resolved


# =========================================================
# SSAS METADATA ENUMS
#
# These are SSAS metadata values, not TestModel-specific
# hard-coding.
# =========================================================

DATA_TYPES = {
    1: "Automatic",
    2: "String",
    6: "Int64",
    8: "Double",
    9: "DateTime",
    10: "Decimal",
    11: "Boolean",
    17: "Binary",
    19: "Unknown",
    20: "Variant",
}

CARDINALITIES = {
    0: "None",
    1: "One",
    2: "Many",
}

CROSS_FILTERING = {
    1: "OneDirection",
    2: "BothDirections",
    3: "Automatic",
}


# =========================================================
# GENERAL HELPERS
# =========================================================

def clean_column_name(name):
    """
    Normalize metadata column names returned by ADOMD.

    Examples:
        [Name]       -> Name
        Table[Name]  -> Name
        Name         -> Name
    """

    name = str(name)

    if "[" in name and name.endswith("]"):
        return name.rsplit("[", 1)[1][:-1]

    return name.strip("[]")


def dax_table_name(name):
    """
    Safely quote a table name for DAX.
    """

    escaped = str(name).replace("'", "''")
    return f"'{escaped}'"


def get_int(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_bool(value):
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    return str(value).lower() in (
        "true",
        "1",
        "yes",
    )


def is_user_column(column):
    """
    Exclude SSAS internal RowNumber columns.

    Column Type:
        1 = Data
        2 = Calculated
        3 = RowNumber
        4 = CalculatedTableColumn
    """

    column_type = get_int(
        column.get("Type")
    )

    return column_type != 3


# =========================================================
# EXECUTE SSAS METADATA ROWSET QUERY
# =========================================================

def fetch_rowset(connection, query):

    with connection.cursor().execute(query) as cursor:

        column_names = [
            clean_column_name(column.name)
            for column in cursor.description
        ]

        rows = cursor.fetchall()

    return [
        dict(zip(column_names, row))
        for row in rows
    ]


# =========================================================
# DISCOVER MODEL METADATA
#
# No model schema is hard-coded.
# =========================================================

def discover_model(connection_string=None, database_name=None):

    print(
        f"Reading metadata from "
        f"{database_name or SSAS_DATABASE or 'selected database'} ..."
    )

    with Pyadomd(resolve_connection_string(connection_string)) as connection:

        tables = fetch_rowset(
            connection,
            """
            SELECT *
            FROM $SYSTEM.TMSCHEMA_TABLES
            """
        )

        columns = fetch_rowset(
            connection,
            """
            SELECT *
            FROM $SYSTEM.TMSCHEMA_COLUMNS
            """
        )

        measures = fetch_rowset(
            connection,
            """
            SELECT *
            FROM $SYSTEM.TMSCHEMA_MEASURES
            """
        )

        relationships = fetch_rowset(
            connection,
            """
            SELECT *
            FROM $SYSTEM.TMSCHEMA_RELATIONSHIPS
            """
        )

    return {
        "tables": tables,
        "columns": columns,
        "measures": measures,
        "relationships": relationships,
    }


# =========================================================
# COLUMN METADATA HELPERS
# =========================================================

def get_metadata_column_name(column):

    explicit = column.get("ExplicitName")
    inferred = column.get("InferredName")
    source = column.get("SourceColumn")
    name = column.get("Name")

    return (
        explicit
        or inferred
        or name
        or source
        or "(unnamed column)"
    )


def get_column_data_type(column):

    explicit = get_int(
        column.get("ExplicitDataType")
    )

    inferred = get_int(
        column.get("InferredDataType")
    )

    data_type = get_int(
        column.get("DataType")
    )

    if explicit not in (None, 1):
        value = explicit

    elif inferred not in (None, 1):
        value = inferred

    else:
        value = data_type

    if value is None:
        return "Unknown"

    return DATA_TYPES.get(
        value,
        f"DataType({value})"
    )


# =========================================================
# GET USER TABLES
# =========================================================

def get_user_tables(metadata):

    user_tables = []

    for table in metadata["tables"]:

        if get_bool(
            table.get("IsPrivate")
        ):
            continue

        if get_bool(
            table.get("SystemManaged")
        ):
            continue

        if not table.get("Name"):
            continue

        user_tables.append(table)

    return user_tables


# =========================================================
# BUILD A TEXT REPRESENTATION OF THE LIVE MODEL
#
# This is the schema sent to the selected AI provider.
# =========================================================

def build_schema(metadata, database_name=None):

    tables = metadata["tables"]
    columns = metadata["columns"]
    measures = metadata["measures"]
    relationships = metadata["relationships"]

    user_tables = get_user_tables(metadata)

    tables_by_id = {
        table.get("ID"): table
        for table in user_tables
        if table.get("ID") is not None
    }

    columns_by_id = {
        column.get("ID"): column
        for column in columns
        if column.get("ID") is not None
    }

    lines = []

    lines.append(
        f"DATABASE: {database_name or SSAS_DATABASE or 'selected database'}"
    )

    lines.append("")

    lines.append(
        "This schema was discovered live from "
        "the SSAS Tabular model."
    )

    lines.append("")

    # -----------------------------------------------------
    # TABLES
    # -----------------------------------------------------

    for table in sorted(
        user_tables,
        key=lambda item: str(
            item.get("Name", "")
        )
    ):

        table_id = table.get("ID")
        table_name = table.get("Name")

        lines.append(
            f"TABLE: {dax_table_name(table_name)}"
        )

        description = table.get(
            "Description"
        )

        if description:
            lines.append(
                f"Description: {description}"
            )

        if get_bool(
            table.get("IsHidden")
        ):
            lines.append(
                "Visibility: Hidden"
            )

        # -------------------------------------------------
        # COLUMNS
        # -------------------------------------------------

        lines.append("Columns:")

        table_columns = [
            column
            for column in columns
            if (
                column.get("TableID")
                == table_id
                and is_user_column(column)
            )
        ]

        if not table_columns:
            lines.append(
                "- No user columns discovered"
            )

        for column in table_columns:

            name = get_metadata_column_name(
                column
            )

            data_type = get_column_data_type(
                column
            )

            attributes = []

            if get_bool(
                column.get("IsKey")
            ):
                attributes.append("key")

            if get_bool(
                column.get("IsHidden")
            ):
                attributes.append("hidden")

            if get_bool(
                column.get("IsUnique")
            ):
                attributes.append("unique")

            attribute_text = ""

            if attributes:
                attribute_text = (
                    " ["
                    + ", ".join(attributes)
                    + "]"
                )

            lines.append(
                f"- "
                f"{dax_table_name(table_name)}"
                f"[{name}]"
                f" : {data_type}"
                f"{attribute_text}"
            )

            column_description = (
                column.get("Description")
            )

            if column_description:
                lines.append(
                    "  Description: "
                    + str(column_description)
                )

        # -------------------------------------------------
        # MEASURES
        # -------------------------------------------------

        table_measures = [
            measure
            for measure in measures
            if measure.get("TableID")
            == table_id
        ]

        if table_measures:

            lines.append("Measures:")

            for measure in table_measures:

                measure_name = (
                    measure.get("Name")
                )

                if not measure_name:
                    continue

                measure_type = get_int(
                    measure.get("DataType")
                )

                measure_type_name = (
                    DATA_TYPES.get(
                        measure_type,
                        f"DataType({measure_type})"
                    )
                    if measure_type is not None
                    else "Unknown"
                )

                attributes = []

                if get_bool(
                    measure.get("IsHidden")
                ):
                    attributes.append(
                        "hidden"
                    )

                attribute_text = ""

                if attributes:
                    attribute_text = (
                        " ["
                        + ", ".join(attributes)
                        + "]"
                    )

                lines.append(
                    f"- [{measure_name}]"
                    f" : {measure_type_name}"
                    f"{attribute_text}"
                )

                measure_description = (
                    measure.get("Description")
                )

                if measure_description:
                    lines.append(
                        "  Description: "
                        + str(
                            measure_description
                        )
                    )

                expression = measure.get(
                    "Expression"
                )

                if expression:
                    lines.append(
                        "  Expression: "
                        + str(expression)
                    )

                format_string = measure.get(
                    "FormatString"
                )

                if format_string:
                    lines.append(
                        "  Format: "
                        + str(format_string)
                    )

        lines.append("")

    # -----------------------------------------------------
    # RELATIONSHIPS
    # -----------------------------------------------------

    lines.append("RELATIONSHIPS:")

    relationship_count = 0

    for relationship in relationships:

        from_table = tables_by_id.get(
            relationship.get(
                "FromTableID"
            )
        )

        to_table = tables_by_id.get(
            relationship.get(
                "ToTableID"
            )
        )

        from_column = columns_by_id.get(
            relationship.get(
                "FromColumnID"
            )
        )

        to_column = columns_by_id.get(
            relationship.get(
                "ToColumnID"
            )
        )

        if not from_table or not to_table:
            continue

        if not from_column or not to_column:
            continue

        from_table_name = (
            from_table.get("Name")
        )

        to_table_name = (
            to_table.get("Name")
        )

        from_column_name = (
            get_metadata_column_name(
                from_column
            )
        )

        to_column_name = (
            get_metadata_column_name(
                to_column
            )
        )

        from_cardinality_value = get_int(
            relationship.get(
                "FromCardinality"
            )
        )

        to_cardinality_value = get_int(
            relationship.get(
                "ToCardinality"
            )
        )

        cross_filter_value = get_int(
            relationship.get(
                "CrossFilteringBehavior"
            )
        )

        from_cardinality = (
            CARDINALITIES.get(
                from_cardinality_value,
                str(
                    from_cardinality_value
                )
            )
        )

        to_cardinality = (
            CARDINALITIES.get(
                to_cardinality_value,
                str(
                    to_cardinality_value
                )
            )
        )

        cross_filter = (
            CROSS_FILTERING.get(
                cross_filter_value,
                str(cross_filter_value)
            )
        )

        active = get_bool(
            relationship.get(
                "IsActive"
            )
        )

        relationship_name = (
            relationship.get("Name")
            or "(unnamed relationship)"
        )

        lines.append(
            f"- {relationship_name}: "
            f"{dax_table_name(from_table_name)}"
            f"[{from_column_name}] "
            f"({from_cardinality}) -> "
            f"{dax_table_name(to_table_name)}"
            f"[{to_column_name}] "
            f"({to_cardinality}); "
            f"active={active}; "
            f"filtering={cross_filter}"
        )

        relationship_count += 1

    if relationship_count == 0:
        lines.append(
            "- No relationships discovered"
        )

    return "\n".join(lines)


# =========================================================
# MODEL STATISTICS
# =========================================================

def metadata_statistics(metadata):

    tables = get_user_tables(
        metadata
    )

    table_ids = {
        table.get("ID")
        for table in tables
    }

    columns = [
        column
        for column in metadata["columns"]
        if (
            column.get("TableID")
            in table_ids
            and is_user_column(column)
        )
    ]

    measures = [
        measure
        for measure in metadata["measures"]
        if measure.get("TableID")
        in table_ids
    ]

    relationships = [
        relationship
        for relationship
        in metadata["relationships"]
        if (
            relationship.get(
                "FromTableID"
            )
            in table_ids
            and relationship.get(
                "ToTableID"
            )
            in table_ids
        )
    ]

    return (
        len(tables),
        len(columns),
        len(measures),
        len(relationships),
    )


# =========================================================
# AI API CALL WITH BOUNDED SDK RETRIES
# =========================================================


def call_ai(prompt, json_mode=False, max_attempts=5):
    return ai_client.complete(prompt, json_mode=json_mode, max_attempts=max_attempts)


# Retain the former entry point for scripts that imported it. It now uses the
# provider selected by AI_PROVIDER, just like every other agent stage.
call_qwen = call_ai


# =========================================================
# GENERATE DAX
# =========================================================

def generate_dax(
    question,
    model_schema,
    knowledge_base=(),
):

    prompt = f"""
You are an expert Microsoft SSAS Tabular and DAX query
generator.

The following semantic model schema was discovered LIVE
from the SSAS server.

It is authoritative.

Do not invent any table, column, measure, relationship,
or value that does not appear in this schema.

================ MODEL SCHEMA ================

{model_schema}

==============================================

Convert the user's question into a READ-ONLY DAX QUERY.

{format_knowledge_base(knowledge_base)}

RULES:

1. Return a DAX query, not a measure definition.

2. Every query must contain EVALUATE.

3. DEFINE may be used if required.

4. Never generate model-changing commands such as:
   CREATE
   ALTER
   DELETE
   DROP
   REFRESH
   PROCESS
   TMSL
   XMLA

5. Never modify the semantic model.

6. Use ONLY objects contained in the discovered schema.

7. Prefer existing measures rather than reproducing their
   calculation manually.

8. Prefer SUMMARIZECOLUMNS for grouped results.

9. Limit potentially large result sets to at most 100 rows.

10. When the question implies sorting, ranking, highest,
    lowest, top, bottom, ascending, or descending output,
    use an appropriate explicit ORDER BY where useful.

11. Respect the relationships defined in the schema.

12. When answering questions such as "most", "highest",
    or "lowest", use TOPN where appropriate.

13. Do not add markdown fences around the DAX.

14. Return ONLY a JSON object.

Required JSON format:

{{
    "dax": "the DAX query"
}}

15. UNION is a function. Never write:

    ROW(...)
    UNION
    ROW(...)

    Write:

    UNION(
        ROW(...),
        ROW(...)
    )

16. In SUMMARIZECOLUMNS, measures must always be supplied
    as named expression pairs:

    "Output Name", [Measure]

    Never place a measure where a group-by column is expected.

17. Every SUMMARIZECOLUMNS group-by argument must be a
    fully-qualified physical column reference such as:

    'Table'[Column]

18. Before returning the query, internally check its DAX
    syntax, especially:
    - parentheses
    - UNION
    - ROW
    - SUMMARIZECOLUMNS
    - TOPN
    - FILTER
    - VAR / RETURN
    - ORDER BY

USER QUESTION:

{question}
"""

    response_text = call_ai(
        prompt=prompt,
        json_mode=True,
    )

    try:

        result = json.loads(
            response_text
        )

    except json.JSONDecodeError as error:

        raise RuntimeError(
            f"{AI_SETTINGS.provider} returned invalid JSON:\n"
            + response_text
        ) from error

    if not isinstance(result, dict):
        raise RuntimeError(f"{AI_SETTINGS.provider} must return a JSON object with a dax field.")

    dax = result.get("dax")

    if not isinstance(dax, str) or not dax.strip():
        raise RuntimeError(
            f"{AI_SETTINGS.provider} returned JSON but "
            "did not include a 'dax' field."
        )

    return dax.strip()


# =========================================================
# DAX SAFETY CHECK
# =========================================================

def validate_dax(dax):

    upper = dax.upper()

    forbidden = [
        "CREATE ",
        "ALTER ",
        "DELETE ",
        "DROP ",
        "REFRESH",
        "PROCESS",
        "TMSL",
        "XMLA",
    ]

    for command in forbidden:

        if command in upper:

            raise ValueError(
                "Unsafe command detected: "
                + command.strip()
            )

    if "EVALUATE" not in upper:

        raise ValueError(
            "Generated query does not "
            "contain EVALUATE."
        )


# =========================================================
# EXECUTE DAX AGAINST SSAS
# =========================================================

def execute_dax(dax, connection_string=None):

    validate_dax(dax)

    with Pyadomd(
        resolve_connection_string(connection_string)
    ) as connection:

        with connection.cursor().execute(
            dax
        ) as cursor:

            columns = [
                column.name
                for column
                in cursor.description
            ]

            rows = cursor.fetchall()

    results = []

    # Safety limit before results are sent
    # back to the LLM.
    for row in rows[:100]:

        results.append(
            dict(
                zip(
                    columns,
                    row,
                )
            )
        )

    return results


# =========================================================
# EXPLAIN QUERY RESULTS
# =========================================================

def explain_results(
    question,
    dax,
    results,
):

    prompt = f"""
You are a business intelligence assistant.

A user asked this question about an SSAS Tabular semantic
model:

{question}

The following DAX query was successfully executed against
SSAS:

{dax}

The database returned the following result:

{json.dumps(
    results,
    indent=2,
    default=str
)}

Answer the user's original question using ONLY the database
result above.

RULES:

- Do not invent values.
- Do not estimate values that are not returned by SSAS.
- Format numbers clearly.
- Be concise.
- Mention useful comparisons when appropriate.
- If there are no rows, say that no matching data was found.
- Do not discuss DAX, SSAS, Python, or implementation details
  unless the user specifically asks about them.
"""

    return call_ai(
        prompt=prompt,
        json_mode=False,
    )


# =========================================================
# ASK ONE QUESTION
# =========================================================

def ask(
    question,
    model_schema,
    connection_string=None,
    knowledge_base=(),
):

    print(
        "\nGenerating DAX...\n"
    )

    dax, results = (
        generate_and_execute_dax(
            question=question,
            model_schema=model_schema,
            max_repairs=2,
            connection_string=connection_string,
            knowledge_base=knowledge_base,
        )
    )

    print("Database result:")

    print(
        json.dumps(
            results,
            indent=2,
            default=str,
        )
    )

    print(
        "\nGenerating answer...\n"
    )

    answer = explain_results(
        question,
        dax,
        results,
    )

    return answer


# =========================================================
# LOAD / RELOAD LIVE SSAS SCHEMA
# =========================================================

def load_schema(connection_string=None, database_name=None):

    metadata = discover_model(connection_string, database_name)

    schema = build_schema(
        metadata,
        database_name=database_name,
    )

    (
        table_count,
        column_count,
        measure_count,
        relationship_count,
    ) = metadata_statistics(
        metadata
    )

    print(
        "\nModel discovered successfully:"
    )

    print(
        f"  Tables:        {table_count}"
    )

    print(
        f"  Columns:       {column_count}"
    )

    print(
        f"  Measures:      {measure_count}"
    )

    print(
        f"  Relationships: {relationship_count}"
    )

    return schema


# =========================================================
# REPAIR INVALID DAX
# =========================================================

def repair_dax(
    question,
    model_schema,
    bad_dax,
    ssas_error,
    knowledge_base=(),
):

    prompt = f"""
You are an expert Microsoft SSAS Tabular DAX query debugger.

A previous DAX query was generated for a user's question,
but SSAS rejected it.

Your task is to FIX the query.

{format_knowledge_base(knowledge_base)}

================ MODEL SCHEMA ================

{model_schema}

==============================================

USER QUESTION:

{question}


INVALID DAX:

{bad_dax}


SSAS ERROR:

{ssas_error}


RULES:

1. Return a complete corrected DAX QUERY.

2. The query must contain EVALUATE.

3. Use ONLY tables, columns, measures, and relationships
   present in the supplied schema.

4. Preserve the user's original analytical intent.

5. Fix the actual error reported by SSAS.

6. Never generate:
   CREATE
   ALTER
   DELETE
   DROP
   REFRESH
   PROCESS
   TMSL
   XMLA

7. UNION is a function and must use this syntax:

   UNION(
       table_expression_1,
       table_expression_2
   )

8. ROW syntax is:

   ROW(
       "Column Name", scalar_expression,
       "Another Column", scalar_expression
   )

9. SUMMARIZECOLUMNS syntax for measures is:

   SUMMARIZECOLUMNS(
       'Table'[GroupColumn],
       "Measure Output Name", [Measure],
       "Another Output Name", [Another Measure]
   )

   Measures must NOT be supplied as unnamed group-by
   arguments.

10. Group-by arguments to SUMMARIZECOLUMNS must be fully
    qualified physical column references.

11. If using variables after EVALUATE, use:

    EVALUATE
    VAR x = ...
    VAR y = ...
    RETURN
    <table expression>

12. Return ONLY JSON.

Required JSON format:

{{
    "dax": "corrected DAX query"
}}
"""

    response_text = call_ai(
        prompt=prompt,
        json_mode=True,
    )

    try:
        result = json.loads(
            response_text
        )

    except json.JSONDecodeError as error:

        raise RuntimeError(
            f"{AI_SETTINGS.provider} returned invalid JSON while repairing DAX:\n"
            + response_text
        ) from error

    if not isinstance(result, dict):
        raise RuntimeError(f"{AI_SETTINGS.provider} must return a JSON object with a dax field.")

    dax = result.get("dax")

    if not isinstance(dax, str) or not dax.strip():
        raise RuntimeError(
            f"{AI_SETTINGS.provider} did not return a repaired 'dax' field."
        )

    return dax.strip()


# =========================================================
# GENERATE + EXECUTE + AUTO-REPAIR DAX
# =========================================================

def generate_and_execute_dax(
    question,
    model_schema,
    max_repairs=2,
    connection_string=None,
    knowledge_base=(),
):

    dax = generate_dax(
        question,
        model_schema,
        knowledge_base=knowledge_base,
    )

    for attempt in range(
        max_repairs + 1
    ):

        print("Generated DAX:")

        print("=" * 70)
        print(dax)
        print("=" * 70)

        print(
            "\nExecuting against SSAS...\n"
        )

        try:

            results = execute_dax(
                dax,
                connection_string=connection_string,
            )

            return dax, results

        except Exception as error:

            if attempt >= max_repairs:
                raise

            ssas_error = str(error)

            print("SSAS rejected the DAX.")
            print()

            print(
                f"Attempting automatic repair "
                f"({attempt + 1}/{max_repairs})..."
            )

            print()

            dax = repair_dax(
                question=question,
                model_schema=model_schema,
                bad_dax=dax,
                ssas_error=ssas_error,
                knowledge_base=knowledge_base,
            )

            print(
                f"{AI_SETTINGS.provider} produced a repaired query.\n"
            )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    print("=" * 70)
    print(f"SSAS AI Agent - {AI_SETTINGS.provider}")
    print("=" * 70)

    print(
        f"\nServer:    {SSAS_SERVER}"
    )

    print(
        f"Database:  {SSAS_DATABASE}"
    )

    print(
        f"AI model:  {AI_SETTINGS.model}"
    )

    print(
        f"Provider:  {AI_SETTINGS.provider}"
    )

    print()

    # -----------------------------------------------------
    # DISCOVER SCHEMA
    # -----------------------------------------------------

    try:

        model_schema = load_schema()

    except Exception as error:

        print(
            "\nCould not read SSAS metadata."
        )

        print(error)

        sys.exit(1)

    # -----------------------------------------------------
    # CHAT LOOP
    # -----------------------------------------------------

    print(
        "\nAsk questions about the model."
    )

    print(
        "Commands: schema, reload, exit\n"
    )

    while True:

        question = input(
            "You: "
        ).strip()

        if not question:
            continue

        command = question.lower()

        # -------------------------------------------------
        # EXIT
        # -------------------------------------------------

        if command in (
            "exit",
            "quit",
            "q",
        ):
            break

        # -------------------------------------------------
        # SHOW DISCOVERED SCHEMA
        # -------------------------------------------------

        if command == "schema":

            print(
                "\n"
                + "=" * 70
            )

            print(
                model_schema
            )

            print(
                "=" * 70
                + "\n"
            )

            continue

        # -------------------------------------------------
        # RELOAD SCHEMA FROM SSAS
        # -------------------------------------------------

        if command == "reload":

            try:

                model_schema = (
                    load_schema()
                )

                print(
                    "\nSchema reloaded.\n"
                )

            except Exception as error:

                print(
                    "\nCould not reload schema:"
                )

                print(error)
                print()

            continue

        # -------------------------------------------------
        # ASK THE SELECTED AI PROVIDER
        # -------------------------------------------------

        try:

            answer = ask(
                question,
                model_schema,
            )

            print("\nAI:")
            print(answer)
            print()

        except Exception as error:

            print("\nERROR:")
            print(error)
            print()
