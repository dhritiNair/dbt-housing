"""pipeline.py with a scripted writer and a small real MartDB. No network calls."""

import duckdb
import pytest

from config import Settings
from db import MartDB
from llm import Chart, Draft, LLMError
from pipeline import Pipeline
from sql_guard import ALLOWED_TABLES

SC = "mart_bay_area_scorecard"
NO_CHART = Chart("none", "", "", "")


class ScriptedWriter:
    """Returns the queued drafts (or raises queued errors) in order."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def draft(self, question, failed=None):
        self.calls.append((question, failed))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def answer(sql):
    return Draft("answer", sql, Chart("bar", "county", "n", ""), "One line.")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "housing.duckdb"
    con = duckdb.connect(str(path))
    for table in ALLOWED_TABLES:
        con.execute(f"create table {table} as select 'Marin County, CA' as county, 1 as n")
    con.execute("create table raw_redfin_county as select 1 as x")
    con.close()
    return MartDB(str(path))


def run(db, *results):
    writer = ScriptedWriter(*results)
    return Pipeline(Settings(api_key="unused"), db, writer).ask("question?"), writer


def test_answers_on_first_try(db):
    result, writer = run(db, answer(f"select county, n from {SC}"))
    assert result.status == "answered"
    assert result.df.to_dict("records") == [{"county": "Marin County, CA", "n": 1}]
    assert "LIMIT 1000" in result.sql  # the validated SQL is what ran and is shown
    assert len(writer.calls) == 1 and len(result.attempts) == 1


def test_retries_once_after_guard_rejection(db):
    result, writer = run(db, answer("select * from raw_redfin_county"), answer(f"select * from {SC}"))
    assert result.status == "answered"
    question, failed = writer.calls[1]
    assert failed[0] == "select * from raw_redfin_county" and "not allowed" in failed[1]
    assert [a.error is None for a in result.attempts] == [False, True]


def test_retries_once_after_query_error(db):
    result, writer = run(db, answer(f"select no_such_column from {SC}"), answer(f"select * from {SC}"))
    assert result.status == "answered"
    assert "no_such_column" in writer.calls[1][1][1]


def test_fails_after_second_bad_query(db):
    result, writer = run(db, answer("drop table x"), answer("select * from raw_redfin_county"))
    assert result.status == "failed"
    assert "after 2 attempts" in result.error and "raw_redfin_county" in result.error
    assert len(writer.calls) == 2


def test_decline(db):
    result, writer = run(db, Draft("decline", "", NO_CHART, "The data has no rents."))
    assert result.status == "declined" and result.explanation == "The data has no rents."
    assert result.sql is None


def test_llm_error_is_reported(db):
    result, _ = run(db, LLMError("Could not reach the Claude API."))
    assert result.status == "failed" and result.error == "Could not reach the Claude API."


def test_empty_question_skips_the_model(db):
    writer = ScriptedWriter()
    result = Pipeline(Settings(api_key="unused"), db, writer).ask("   ")
    assert result.status == "failed" and writer.calls == []


def test_attempts_record_the_failing_stage(db):
    result, _ = run(db, answer("select * from raw_redfin_county"), answer(f"select nope from {SC}"))
    assert [a.stage for a in result.attempts] == ["guard", "query"]
