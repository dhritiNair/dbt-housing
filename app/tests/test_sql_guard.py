import pytest

from sql_guard import SQLGuardError, validate

PT = "mart_bay_area_price_trends"
SC = "mart_bay_area_scorecard"
PROP = "mart_property_type_trends"


# --- accepted -------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    f"select * from {SC}",
    f"select county, median_sale_price from {SC} order by 2 desc;",
    f"from {SC}",
    f"select count(*) from {SC} where market_tilt = 'seller'",
    f"select * from {PT} where period_begin = (select max(period_begin) from {PT})",
    f"with latest as (select max(period_begin) as m from {PT}) "
    f"select p.* from {PT} p join latest on p.period_begin = latest.m",
    f"with a as (select * from {PROP}), b as (select * from a where homes_sold > 0) select * from b",
    f"select county from {SC} union all select county from {PT}",
    f"select * from {PROP} where county in (select county from {SC} where market_tilt = 'buyer')",
    f"select county, year(period_begin) y, avg(median_sale_price) from {PT} group by all",
    f'select * from "{SC}"',
    f"select * from {SC.upper()}",
    f"-- comment\nselect * from {SC} /* inline */",
])
def test_accepts_read_only_selects(sql):
    validate(sql)


def test_reports_tables_used():
    result = validate(f"with a as (select * from {PT}) select * from a join {SC} using (county)")
    assert result.tables == {PT, SC}


# --- rejected: statement shape ----------------------------------------------

@pytest.mark.parametrize("sql", ["", "   ", ";"])
def test_rejects_empty(sql):
    with pytest.raises(SQLGuardError):
        validate(sql)


def test_rejects_unparseable():
    with pytest.raises(SQLGuardError, match="parse"):
        validate("select from where")


@pytest.mark.parametrize("sql", [
    f"select * from {SC}; drop table {SC}",
    f"select * from {SC}; select * from {PT}",
])
def test_rejects_multiple_statements(sql):
    with pytest.raises(SQLGuardError, match="exactly one"):
        validate(sql)


@pytest.mark.parametrize("sql", [
    f"drop table {SC}",
    f"delete from {SC}",
    f"update {SC} set county = 'x'",
    f"insert into {SC} select * from {SC}",
    f"create table t as select * from {SC}",
    f"alter table {SC} rename to t",
    "attach 'other.duckdb' as o",
    "detach o",
    f"copy {SC} to 'out.csv'",
    f"copy (select * from {SC}) to 'out.csv'",
    "install httpfs",
    "load httpfs",
    "set enable_external_access = true",
    "pragma database_list",
    f"describe {SC}",
    f"summarize {SC}",
    f"explain select * from {SC}",
    "checkpoint",
    "begin transaction",
])
def test_rejects_non_select_statements(sql):
    with pytest.raises(SQLGuardError):
        validate(sql)


def test_rejects_select_into():
    with pytest.raises(SQLGuardError, match="INTO"):
        validate(f"select * into t from {SC}")


def test_rejects_recursive_cte():
    with pytest.raises(SQLGuardError, match="Recursive"):
        validate(f"with recursive r as (select 1 as n union all select n + 1 from r) select * from r")


# --- rejected: tables -------------------------------------------------------

@pytest.mark.parametrize("table", [
    "raw_redfin_county",
    "stg_redfin__county_market",
    "int_county_monthly",
    "bay_area_counties",
    "information_schema.tables",
    "duckdb_tables",
])
def test_rejects_non_mart_tables(table):
    with pytest.raises(SQLGuardError):
        validate(f"select * from {table}")


def test_rejects_non_mart_table_in_join_and_subquery():
    with pytest.raises(SQLGuardError, match="raw_redfin_county"):
        validate(f"select * from {SC} join raw_redfin_county using (county)")
    with pytest.raises(SQLGuardError, match="int_county_monthly"):
        validate(f"select * from {SC} where county in (select county from int_county_monthly)")


def test_rejects_cte_that_reads_real_table_of_same_name():
    # DuckDB resolves the inner reference to the real table, not the CTE.
    with pytest.raises(SQLGuardError, match="raw_redfin_county"):
        validate("with raw_redfin_county as (select * from raw_redfin_county) select * from raw_redfin_county")


def test_rejects_cte_forward_reference_to_real_table():
    # b is defined after a, so inside a the name b is the real table.
    with pytest.raises(SQLGuardError, match="'b'"):
        validate(f"with a as (select * from b), b as (select * from {SC}) select * from a")


@pytest.mark.parametrize("sql", [
    f"select * from main.{SC}",
    f"select * from housing.main.{SC}",
    f"select * from o.{SC}",
])
def test_rejects_qualified_names(sql):
    with pytest.raises(SQLGuardError, match="Qualified"):
        validate(sql)


@pytest.mark.parametrize("sql", [
    "select * from 'data/raw/county_market_tracker.tsv000.gz'",
    "select * from 'https://example.com/x.parquet'",
])
def test_rejects_file_and_url_scans(sql):
    with pytest.raises(SQLGuardError):
        validate(sql)


# --- rejected: functions ----------------------------------------------------

@pytest.mark.parametrize("sql", [
    "select * from read_csv('x.csv')",
    "select * from read_parquet('x.parquet')",
    "select * from read_text('/etc/passwd')",
    "select * from query_table('raw_redfin_county')",
    "select * from query('select * from raw_redfin_county')",
    "select * from duckdb_tables()",
    "select * from range(10)",
    "select * from glob('*')",
])
def test_rejects_table_functions(sql):
    with pytest.raises(SQLGuardError):
        validate(sql)


@pytest.mark.parametrize("sql", [
    f"select getenv('ANTHROPIC_API_KEY') from {SC}",
    f"select current_setting('enable_external_access') from {SC}",
    f"select county, (select count(*) from duckdb_tables()) from {SC}",
])
def test_rejects_scalar_escape_functions(sql):
    with pytest.raises(SQLGuardError):
        validate(sql)


# --- LIMIT handling and output ----------------------------------------------

def test_adds_limit_when_missing():
    assert validate(f"select * from {SC}", max_rows=50).sql.rstrip().endswith("LIMIT 50")


def test_caps_large_literal_limit():
    assert "LIMIT 50" in validate(f"select * from {SC} limit 5000", max_rows=50).sql


def test_keeps_smaller_limit():
    assert "LIMIT 3" in validate(f"select * from {SC} limit 3", max_rows=50).sql


def test_limit_applies_to_whole_union():
    out = validate(f"select county from {SC} union all select county from {PT}", max_rows=10).sql
    assert out.count("LIMIT") == 1 and out.rstrip().endswith("LIMIT 10")


def test_output_is_regenerated_from_parse_tree():
    # Comments are stripped, so nothing uninspected reaches execution.
    out = validate(f"select * from {SC} -- ; drop table x").sql
    assert "drop" not in out.lower() and "--" not in out and "/*" not in out


# --- round 2: adversarial ---------------------------------------------------

@pytest.mark.parametrize("sql", [
    # case and quoting: DuckDB matches identifiers case-insensitively, quoted or not
    f'select * from "{SC.upper()}"',
    "SeLeCt * FrOm Mart_Bay_Area_Scorecard",
    # comma join between marts
    f"select * from {SC}, {PT}",
    # FROM-first syntax
    f"from {SC} select county, market_tilt",
    f"from {SC} where market_tilt = 'seller'",
    # nested CTEs that legitimately shadow a forbidden name with mart data
    f"with m as (select * from {SC}) "
    f"select * from (with raw_redfin_county as (select * from m) select * from raw_redfin_county)",
    f"with x as (select * from {SC}) select * from (with x as (select * from x) select * from x)",
    f"with raw_redfin_county as (select * from {SC}) "
    f"select * from (with y as (select * from raw_redfin_county) select * from y)",
    # an alias is not a table reference
    f"select * from {SC} as raw_redfin_county",
    # PIVOT over a mart
    f"select * from (pivot {PROP} on property_type using sum(homes_sold) group by county)",
])
def test_round2_accepts(sql):
    validate(sql)


@pytest.mark.parametrize("sql", [
    # case and quoting
    'SELECT * FROM "Raw_Redfin_County"',
    "SELECT * FROM RAW_REDFIN_COUNTY",
    f'select * from "main.{SC}"',
    f'select * from "{SC} "',
    'select * from "main"."raw_redfin_county"',
    f"DrOp TaBlE {SC}",
    # scalar subqueries on non-mart tables, in every clause
    f"select county, (select max(median_sale_price) from stg_redfin__county_market) from {SC}",
    f"select * from {SC} where exists (select 1 from raw_redfin_county)",
    f"select * from {SC} order by (select count(*) from int_county_monthly)",
    f"select county, case when (select 1 from bay_area_counties limit 1) = 1 then 1 end from {SC}",
    f"select county, count(*) from {PT} group by county "
    f"having count(*) > (select count(*) from raw_redfin_county)",
    # comma, lateral, natural and positional joins to non-mart tables
    f"select * from {SC}, raw_redfin_county",
    f"select * from {SC}, lateral (select * from raw_redfin_county) r",
    f"select * from {SC} natural join stg_redfin__county_market",
    f"select * from {SC} positional join raw_redfin_county",
    # FROM-first syntax
    "from raw_redfin_county",
    "from raw_redfin_county select county",
    # nested CTE shadowing: an inner CTE is not visible outside its subquery
    "with a as (with raw_redfin_county as (select 1) select * from raw_redfin_county) "
    "select * from raw_redfin_county",
    "with a as (select * from (with b as (select 1) select * from raw_redfin_county)) select * from a",
    # semicolon outside a string
    "select 'x'; drop table y",
    # other statement forms
    "table raw_redfin_county",
    "pivot raw_redfin_county on property_type using sum(homes_sold)",
    "select * from (pivot raw_redfin_county on property_type using sum(homes_sold))",
    "values (1), (2)",
])
def test_round2_rejects(sql):
    with pytest.raises(SQLGuardError):
        validate(sql)


@pytest.mark.parametrize("sql, literal", [
    (f"select * from {SC} where county = 'a; drop table x'", "'a; drop table x'"),
    (f"select * from {SC} where county = 'it''s; drop table x'", "'it''s; drop table x'"),
    (f"select $$; drop table x$$ as s from {SC}", "'; drop table x'"),
])
def test_semicolon_inside_string_stays_in_literal(sql, literal):
    out = validate(sql).sql
    assert literal in out and out.count(";") == 1
