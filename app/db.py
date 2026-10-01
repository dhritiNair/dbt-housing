"""Run validated SQL against a fresh in-memory copy of the three marts.

This is the second safety layer behind sql_guard. Queries never touch
housing.duckdb. The marts are read once into a cached Arrow snapshot, and
every query gets its own new in-memory database built from that snapshot,
with file, network and extension access turned off and the configuration
locked. A query that slipped past the guard would find no other tables, no
files, no way to change settings, and nothing it changed would outlive it.

The snapshot is refreshed when housing.duckdb's mtime changes, e.g. after
dbt build. Holding no connection to the file means the app never blocks dbt.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass

import duckdb
import pandas as pd
import pyarrow as pa

from sql_guard import ALLOWED_TABLES

log = logging.getLogger(__name__)


class QueryError(RuntimeError):
    """A query failed. The message is safe to show and to send back to the model."""


class QueryTimeout(QueryError):
    pass


@dataclass(frozen=True)
class QueryResult:
    df: pd.DataFrame
    truncated: bool
    elapsed_s: float


@dataclass(frozen=True)
class _Snapshot:
    tables: dict[str, pa.Table]
    mtime_ns: int


class MartDB:
    def __init__(
        self,
        source_path: str,
        *,
        max_rows: int = 1000,
        timeout_s: float = 10.0,
        memory_limit: str = "256MB",
        threads: int = 2,
    ):
        self.source_path = source_path
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self.memory_limit = memory_limit
        self.threads = threads
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None

    def run(self, sql: str) -> QueryResult:
        start = time.monotonic()
        conn = self._fresh_database()
        timer = threading.Timer(self.timeout_s, conn.interrupt)
        timer.daemon = True
        timer.start()
        try:
            conn.execute(sql)
            rows = conn.fetchmany(self.max_rows + 1)
            columns = [d[0] for d in conn.description or []]
        except duckdb.InterruptException as e:
            raise QueryTimeout(f"Query exceeded {self.timeout_s:g}s and was cancelled.") from e
        except duckdb.Error as e:
            raise QueryError(str(e)) from e
        finally:
            timer.cancel()
            conn.close()

        truncated = len(rows) > self.max_rows
        df = pd.DataFrame(rows[: self.max_rows], columns=columns)
        return QueryResult(df=df, truncated=truncated, elapsed_s=time.monotonic() - start)

    def _fresh_database(self) -> duckdb.DuckDBPyConnection:
        snapshot = self._current_snapshot()
        conn = duckdb.connect(":memory:")
        try:
            for name, table in snapshot.tables.items():
                conn.register("_snapshot", table)
                conn.execute(f"CREATE TABLE main.{name} AS SELECT * FROM _snapshot")
                conn.unregister("_snapshot")

            # Order matters. Tables are loaded first, external access is
            # disabled before any query runs, and the lock comes last so
            # nothing above can be undone by a query.
            memory_limit = self.memory_limit.replace("'", "''")
            for statement in (
                f"SET memory_limit = '{memory_limit}'",
                f"SET threads = {int(self.threads)}",
                "SET temp_directory = ''",  # no spilling to disk; hitting the limit is an error
                "SET autoinstall_known_extensions = false",
                "SET autoload_known_extensions = false",
                "SET enable_external_access = false",
                "SET lock_configuration = true",
            ):
                conn.execute(statement)
        except BaseException:
            conn.close()
            raise
        return conn

    def _current_snapshot(self) -> _Snapshot:
        with self._lock:
            try:
                mtime_ns = os.stat(self.source_path).st_mtime_ns
            except OSError as e:
                if self._snapshot is None:
                    raise QueryError(f"Database not found at {self.source_path}. Run dbt build first.") from e
                return self._snapshot

            if self._snapshot is not None and mtime_ns == self._snapshot.mtime_ns:
                return self._snapshot

            try:
                tables = self._read_marts()
            except duckdb.Error as e:
                # Typically dbt build holding the write lock. Serve the previous
                # snapshot if there is one; its stale mtime makes the next query retry.
                if self._snapshot is None:
                    raise QueryError(f"Could not load marts from {self.source_path}: {e}") from e
                log.warning("Keeping previous mart snapshot, reload failed: %s", e)
                return self._snapshot

            self._snapshot = _Snapshot(tables=tables, mtime_ns=mtime_ns)
            return self._snapshot

    def _read_marts(self) -> dict[str, pa.Table]:
        conn = duckdb.connect(self.source_path, read_only=True)
        try:
            return {
                name: conn.execute(f"SELECT * FROM main.{name}").to_arrow_table()
                for name in sorted(ALLOWED_TABLES)
            }
        finally:
            conn.close()
