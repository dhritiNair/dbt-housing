"""db.py tested on its own: SQL goes straight to MartDB.run with no guard,
so every hostile query here must be stopped by the database layer alone."""

import os
import time

import duckdb
import pytest

from db import MartDB, QueryError, QueryTimeout
from sql_guard import ALLOWED_TABLES

SC = "mart_bay_area_scorecard"


@pytest.fixture
def source(tmp_path):
    """A small stand-in for housing.duckdb: the three marts plus non-mart
    tables and views that must never be reachable."""
    path = tmp_path / "housing.duckdb"
    con = duckdb.connect(str(path))
    for table in ALLOWED_TABLES:
        con.execute(f"create table {table} as select 'Marin County, CA' as county, 1 as n")
    con.execute("create table raw_redfin_county as select 'secret' as s")
    con.execute("create view stg_redfin__county_market as select * from raw_redfin_county")
    con.execute("create table bay_area_counties as select 'Marin County, CA' as county_region")
    con.close()
    (tmp_path / "secrets.csv").write_text("key\nsk-not-a-real-key\n")
    return path


@pytest.fixture
def db(source):
    return MartDB(str(source), max_rows=100, timeout_s=1.0, memory_limit="64MB", threads=1)


# --- normal use -------------------------------------------------------------

def test_reads_marts(db):
    result = db.run(f"select county, n from {SC}")
    assert result.df.to_dict("records") == [{"county": "Marin County, CA", "n": 1}]
    assert not result.truncated


def test_only_marts_exist(db):
    tables = set(db.run("select table_name from duckdb_tables()").df["table_name"])
    views = set(db.run("select view_name from duckdb_views() where not internal").df["view_name"])
    assert tables == ALLOWED_TABLES and views == set()


# --- hostile queries --------------------------------------------------------

@pytest.mark.parametrize("sql, error", [
    ("select * from raw_redfin_county", "does not exist"),
    ("select * from stg_redfin__county_market", "does not exist"),
    ("select * from bay_area_counties", "does not exist"),
    ("select * from query_table('raw_redfin_county')", "does not exist"),
    ("select getenv('HOME')", "getenv does not exist"),
])
def test_non_mart_objects_unreachable(db, sql, error):
    with pytest.raises(QueryError, match=error):
        db.run(sql)


def test_read_csv_blocked(db, source):
    secrets = source.parent / "secrets.csv"
    for sql in (
        f"select * from read_csv('{secrets}')",
        f"select * from '{secrets}'",
        f"select * from read_text('{secrets}')",
        f"select * from read_blob('{source}')",
    ):
        with pytest.raises(QueryError, match="disabled by configuration"):
            db.run(sql)


def test_attach_source_blocked(db, source):
    with pytest.raises(QueryError, match="disabled by configuration"):
        db.run(f"attach '{source}' as s2 (read_only)")


def test_copy_to_file_blocked(db, tmp_path):
    out = tmp_path / "out.csv"
    with pytest.raises(QueryError, match="disabled by configuration"):
        db.run(f"copy {SC} to '{out}'")
    assert not out.exists()


def test_url_blocked(db):
    with pytest.raises(QueryError, match="disabled by configuration"):
        db.run("select * from 'https://example.com/data.csv'")


@pytest.mark.parametrize("sql", ["install httpfs", "load httpfs"])
def test_extensions_blocked(db, sql):
    with pytest.raises(QueryError):
        db.run(sql)


@pytest.mark.parametrize("sql", [
    "set enable_external_access = true",
    "set lock_configuration = false",
    "set memory_limit = '64GB'",
    "set temp_directory = '/tmp'",
    "reset enable_external_access",
])
def test_configuration_locked(db, sql):
    with pytest.raises(QueryError, match="configuration has been locked"):
        db.run(sql)


def test_writes_do_not_reach_source(db, source):
    # The per-query database accepts writes, but the source file never sees them.
    db.run(f"drop table {SC}")
    con = duckdb.connect(str(source), read_only=True)
    assert con.execute(f"select count(*) from {SC}").fetchone() == (1,)
    con.close()


def test_each_query_gets_a_fresh_database(db):
    db.run(f"drop table {SC}")
    db.run("create table leftover as select 1")
    assert db.run(f"select count(*) as c from {SC}").df["c"][0] == 1
    with pytest.raises(QueryError, match="leftover does not exist"):
        db.run("select * from leftover")


# --- resource limits --------------------------------------------------------

@pytest.mark.slow
def test_timeout_interrupts_query(db):
    start = time.monotonic()
    with pytest.raises(QueryTimeout):
        db.run("select sum(a.range * b.range) from range(200000) a, range(200000) b")
    assert time.monotonic() - start < 3
    assert db.run(f"select count(*) as c from {SC}").df["c"][0] == 1  # still usable


@pytest.mark.slow
def test_memory_limit(db):
    with pytest.raises(QueryError, match="Out of Memory"):
        db.run("select count(*) from (select * from range(50000000) order by random())")


def test_row_cap(db):
    capped = db.run("select * from range(5000)")
    assert len(capped.df) == 100 and capped.truncated
    exact = db.run("select * from range(100)")
    assert len(exact.df) == 100 and not exact.truncated


# --- refresh on mtime -------------------------------------------------------

def _bump_mtime(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_refreshes_snapshot_when_source_changes(db, source):
    assert len(db.run(f"select * from {SC}").df) == 1
    con = duckdb.connect(str(source))
    con.execute(f"insert into {SC} values ('Napa County, CA', 2)")
    con.close()
    _bump_mtime(source)
    assert len(db.run(f"select * from {SC}").df) == 2


def test_no_recopy_when_source_unchanged(db):
    db.run("select 1")
    first = db._snapshot
    db.run("select 1")
    assert db._snapshot is first


def test_keeps_old_snapshot_when_reload_fails(db, source, monkeypatch):
    db.run("select 1")
    first = db._snapshot
    _bump_mtime(source)
    monkeypatch.setattr(db, "_read_marts", lambda: (_ for _ in ()).throw(duckdb.IOException("locked")))
    assert len(db.run(f"select * from {SC}").df) == 1
    assert db._snapshot is first


def test_missing_source(tmp_path):
    with pytest.raises(QueryError, match="dbt build"):
        MartDB(str(tmp_path / "nope.duckdb")).run("select 1")
