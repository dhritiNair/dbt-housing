"""Validate model-written SQL before it reaches DuckDB.

The guard accepts a single read-only SELECT over the three marts and returns
the SQL regenerated from the validated parse tree, so what runs is exactly
what was checked. Every check fails closed: anything the guard cannot
positively classify as safe is rejected.

The DuckDB connection in db.py is a second layer; this module does not assume
it exists.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

ALLOWED_TABLES = frozenset({
    "mart_bay_area_price_trends",
    "mart_bay_area_scorecard",
    "mart_property_type_trends",
})

DEFAULT_MAX_ROWS = 1000

# Statement types that write, change config, or reach outside the database.
# The root check already requires a query, so this walk catches anything
# nested inside one.
_FORBIDDEN_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
        "Copy", "Attach", "Detach", "Install", "Pragma", "Set", "Use",
        "Transaction", "Commit", "Rollback", "Describe", "Summarize",
        "Command",
    )
    if hasattr(exp, name)
)

# Functions that read files, URLs, other databases, settings, or catalog
# metadata. Table-function use is already blocked by the table check; this
# list covers scalar use such as SELECT query('...') or getenv('HOME').
_FORBIDDEN_FUNC_PREFIXES = (
    "read_", "glob", "query", "sniff_", "parquet_", "duckdb_", "pragma_",
    "iceberg_", "delta_", "getenv", "current_setting", "which_secret",
    "load_", "checkpoint", "force_checkpoint",
)
_FORBIDDEN_FUNC_SUFFIXES = ("_scan",)


class SQLGuardError(ValueError):
    """Raised when SQL fails validation. The message is safe to show the
    user and to send back to the model for a retry."""


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str
    tables: frozenset[str]


def validate(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> ValidatedSQL:
    if not sql or not sql.strip():
        raise SQLGuardError("Empty query.")

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except SqlglotError as e:
        raise SQLGuardError(f"Could not parse SQL: {e}") from e

    if len(statements) != 1:
        raise SQLGuardError(f"Expected exactly one statement, got {len(statements)}.")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise SQLGuardError(f"Only SELECT queries are allowed, got {tree.key.upper()}.")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLGuardError(f"{node.key.upper()} is not allowed.")
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise SQLGuardError("SELECT ... INTO is not allowed.")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SQLGuardError("Recursive CTEs are not allowed.")
        if isinstance(node, exp.Func):
            name = _func_name(node)
            if name.startswith(_FORBIDDEN_FUNC_PREFIXES) or name.endswith(_FORBIDDEN_FUNC_SUFFIXES):
                raise SQLGuardError(f"Function {name}() is not allowed.")

    tables = _check_tables(tree)
    _apply_limit(tree, max_rows)
    # Comments are dropped so the executed SQL holds nothing the guard did not inspect.
    return ValidatedSQL(sql=tree.sql(dialect="duckdb", pretty=True, comments=False), tables=tables)


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    return node.sql_name().lower()


def _check_tables(tree: exp.Expression) -> frozenset[str]:
    """Every table reference must be a CTE defined in scope or an allowed mart.

    Scope resolution matters: in DuckDB, `WITH x AS (SELECT * FROM x)` reads
    the real table x, so a CTE name alone does not make a reference safe.
    """
    try:
        scopes = list(traverse_scope(tree))
    except SqlglotError as e:
        raise SQLGuardError(f"Could not resolve table references: {e}") from e

    resolved = set()
    used = set()
    for scope in scopes:
        for table in scope.tables:
            resolved.add(id(table))
            if not isinstance(table.this, exp.Identifier):
                raise SQLGuardError("Table functions are not allowed in FROM.")
            if table.args.get("db") or table.args.get("catalog"):
                raise SQLGuardError(f"Qualified table names are not allowed: {table.sql('duckdb')}.")
            name = table.name.lower()
            if name in scope.cte_sources:
                continue
            if name not in ALLOWED_TABLES:
                raise SQLGuardError(
                    f"Table {table.name!r} is not allowed. "
                    f"Use only: {', '.join(sorted(ALLOWED_TABLES))}."
                )
            used.add(name)

    # Fail closed on any table node scope analysis did not account for.
    for table in tree.find_all(exp.Table):
        if id(table) not in resolved:
            raise SQLGuardError(f"Unresolved table reference: {table.sql('duckdb')}.")

    return frozenset(used)


def _apply_limit(tree: exp.Expression, max_rows: int) -> None:
    """Add LIMIT max_rows when absent and lower a larger literal LIMIT.
    A non-literal LIMIT is left alone; db.py caps fetched rows regardless."""
    limit = tree.args.get("limit")
    if limit is None:
        tree.limit(max_rows, copy=False)
        return
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int and int(value.this) > max_rows:
        limit.set("expression", exp.Literal.number(max_rows))
