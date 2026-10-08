"""Read-only validation for DAX that arrives from outside the server.

SECURITY NOTE
-------------
Text-level validation is *defence in depth only*. The primary control must be
the Windows account the server connects with: give it Read (and, if you need
TMSCHEMA discovery, Read Definition) on the database and nothing else. Never
run this server with an Analysis Services administrator account.
"""

from __future__ import annotations

import re

FORBIDDEN_TOKENS = {
    "CREATE", "ALTER", "DROP", "DELETE", "INSERT", "UPDATE", "MERGE",
    "REFRESH", "PROCESS", "BACKUP", "RESTORE", "ATTACH", "DETACH",
    "SYNCHRONIZE", "EXECUTE", "CALL", "SCOPE", "SUBCUBE",
}

IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class DaxValidationError(ValueError):
    """Raised when a DAX string is rejected before it ever reaches the server."""


def strip_literals(dax: str) -> str:
    """Blank out string literals, quoted table names, bracketed identifiers and
    comments, so the keyword scan only sees real DAX syntax.

    This is what stops `'Orders'[Drop Ship Flag]` or a measure named
    "Process Time" from being rejected as an unsafe command; a naive substring
    blacklist rejects both.
    """
    out: list[str] = []
    i = 0
    n = len(dax)
    while i < n:
        ch = dax[i]

        if ch == '"':  # string literal, "" is an escaped quote
            i += 1
            while i < n:
                if dax[i] == '"':
                    if i + 1 < n and dax[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")

        elif ch == "'":  # quoted table name, '' is an escaped quote
            i += 1
            while i < n:
                if dax[i] == "'":
                    if i + 1 < n and dax[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")

        elif ch == "[":  # column / measure reference
            i += 1
            while i < n and dax[i] != "]":
                i += 1
            i += 1
            out.append(" ")

        elif ch == "/" and i + 1 < n and dax[i + 1] == "/":
            while i < n and dax[i] != "\n":
                i += 1
            out.append(" ")

        elif ch == "-" and i + 1 < n and dax[i + 1] == "-":
            while i < n and dax[i] != "\n":
                i += 1
            out.append(" ")

        elif ch == "/" and i + 1 < n and dax[i + 1] == "*":
            i += 2
            while i + 1 < n and not (dax[i] == "*" and dax[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")

        else:
            out.append(ch)
            i += 1

    return "".join(out)


def validate_dax(dax: str, max_chars: int = 20_000) -> None:
    """Structural, token-based validation. Raises DaxValidationError."""
    if not dax or not dax.strip():
        raise DaxValidationError("Empty DAX query.")

    if len(dax) > max_chars:
        raise DaxValidationError(f"DAX query is too long ({len(dax)} characters).")

    stripped = dax.lstrip()
    if stripped[:1] in ("<", "{"):
        raise DaxValidationError("Query looks like XMLA/TMSL, not DAX. Rejected.")

    if "$SYSTEM" in dax.upper():
        raise DaxValidationError(
            "Direct DMV access is not allowed here. Use the metadata tools instead."
        )

    scan = strip_literals(dax)

    if ";" in scan:
        raise DaxValidationError("Statement separators are not allowed.")

    tokens = [t.upper() for t in IDENTIFIER_RE.findall(scan)]
    if not tokens:
        raise DaxValidationError("No DAX statement found.")

    if tokens[0] not in ("DEFINE", "EVALUATE"):
        raise DaxValidationError(
            f"A DAX query must start with DEFINE or EVALUATE (found {tokens[0]})."
        )

    if "EVALUATE" not in tokens:
        raise DaxValidationError("The query does not contain EVALUATE.")

    found = FORBIDDEN_TOKENS.intersection(tokens)
    if found:
        raise DaxValidationError("Unsafe command detected: " + ", ".join(sorted(found)))


DAX_RULES = """\
RULES FOR WRITING DAX AGAINST THIS MODEL

1.  Produce a DAX *query*, not a measure definition.
2.  It must start with DEFINE or EVALUATE and must contain EVALUATE.
3.  Never write CREATE, ALTER, DELETE, DROP, REFRESH, PROCESS, TMSL or XMLA:
    the server rejects them and the connection is read-only anyway.
4.  Use ONLY tables, columns and measures returned by the metadata tools.
    Call get_model_schema / describe_table / search_model_objects first;
    do not guess object names.
5.  Prefer existing measures over re-implementing their calculations.
6.  Prefer SUMMARIZECOLUMNS for grouped results.
7.  ALWAYS bound the result set: wrap it in TOPN(n, ...) unless the query is a
    single-row aggregate. The server also caps rows, but an unbounded query
    still costs the cube time and memory.
8.  Add an explicit ORDER BY when the question implies ranking.
9.  Prefer filtering on a date/period column so the engine can prune partitions.
10. Respect the relationships shown in the schema.
11. Do not invent dates, categories, products, columns, measures or values.
"""