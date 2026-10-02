"""run_eval.py comparison and scoring rules. No network calls."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from pipeline import Answer, Attempt
from run_eval import (
    QUESTIONS_YAML, EvalConfigError, Question, categorize, compare, load_questions,
    run_reference, select, values_match,
)

HOUSING_DB = Path(__file__).resolve().parents[2] / "data" / "housing.duckdb"


def q(expected="answer", mode="top_row", tolerance=1e-9):
    return Question("qx", "t", "question?", expected, False, "select 1" if expected == "answer" else None, mode, tolerance)


def df(*rows, columns=None):
    return pd.DataFrame(list(rows), columns=columns or [f"c{i}" for i in range(len(rows[0]))])


# --- values ------------------------------------------------------------------

def test_tolerance_is_relative():
    assert values_match(1_004_000, 1_000_000, 0.005)
    assert not values_match(1_006_000, 1_000_000, 0.005)
    assert values_match(1.004, 1.0, 0.005) and not values_match(1.006, 1.0, 0.005)


def test_default_tolerance_absorbs_float_noise_only():
    assert values_match(0.1 + 0.2, 0.3)
    assert not values_match(1.9001, 1.9)


def test_zero_reference_needs_exact_zero():
    assert values_match(0, 0.0, 0.5) and not values_match(1e-12, 0.0, 0.5)


def test_types():
    assert values_match(7, 7.0)  # int vs float
    assert values_match(pd.Timestamp("2022-11-01"), dt.date(2022, 11, 1))
    assert values_match(dt.datetime(2022, 11, 1), "2022-11-01")
    assert values_match("Napa County, CA", " Napa County, CA ")
    assert not values_match("napa county, ca", "Napa County, CA")
    assert not values_match("7", 7)
    assert values_match(None, float("nan")) and values_match(pd.NaT, None)


# --- compare by position -----------------------------------------------------

def test_scalar_ignores_extra_answer_columns():
    assert compare(df((47, "Napa County, CA")), df((47,)), "scalar", 1e-9) == (True, "")


def test_scalar_value_found_in_non_first_column():
    assert compare(df(("Santa Clara County, CA", 1.9)), df((1.9,)), "scalar", 0.005) == (True, "")


def test_top_row_value_found_in_non_first_column():
    answer = df((587500, "Solano County, CA"))
    assert compare(answer, df(("Solano County, CA",)), "top_row", 1e-9) == (True, "")


def test_wrong_value_fails_in_any_column():
    ok, note = compare(df(("Santa Clara County, CA", 2.1, 16)), df((1.9,)), "scalar", 0.005)
    assert not ok and "expected [1.9] in the first row" in note
    assert not compare(df((849000, "Sonoma County, CA")), df(("Solano County, CA",)), "top_row", 1e-9)[0]


def test_value_outside_tolerance_fails():
    assert not compare(df(("Santa Clara County, CA", 1.91)), df((1.9,)), "scalar", 0.005)[0]


def test_value_in_a_later_row_does_not_count():
    answer = df(("Sonoma County, CA", 849000), ("Solano County, CA", 587500))
    assert not compare(answer, df(("Solano County, CA",)), "top_row", 1e-9)[0]


def test_strings_need_exact_trimmed_match():
    assert compare(df((" Napa County, CA ",)), df(("Napa County, CA",)), "top_row", 1e-9)[0]
    assert not compare(df(("Napa County",)), df(("Napa County, CA",)), "top_row", 1e-9)[0]


def test_top_row_only_first_row_counts():
    answer = df(("Solano County, CA", 587500), ("Sonoma County, CA", 849000))
    assert compare(answer, df(("Solano County, CA",)), "top_row", 1e-9)[0]
    assert not compare(answer, df(("Sonoma County, CA",)), "top_row", 1e-9)[0]


def test_top_row_multiple_columns():
    reference = df(("Napa County, CA", -0.1864))
    assert compare(df(("x", -0.18641, "Napa County, CA")), reference, "top_row", 0.001)[0]
    assert not compare(df(("Napa County, CA",)), reference, "top_row", 0.001)[0]  # value missing


def test_top_row_values_need_different_columns():
    assert not compare(df((5, "x")), df((5, 5)), "top_row", 1e-9)[0]
    assert compare(df((5, 5)), df((5, 5)), "top_row", 1e-9)[0]


def test_rows_mode_keeps_position_rule():
    reference = df((2016, 1_220_375.0))
    assert not compare(df((1_220_375.0, 2016)), reference, "rows", 0.005)[0]


def test_rows_ignore_order_and_use_tolerance():
    reference = df((2016, 1_220_375.0), (2017, 1_283_275.0))
    answer = df((2017, 1_283_000.0, "extra"), (2016, 1_220_400.0, "extra"))
    assert compare(answer, reference, "rows", 0.005)[0]


def test_rows_count_must_match():
    reference = df((2016, 1.0), (2017, 2.0))
    ok, note = compare(df((2016, 1.0), (2017, 2.0), (2018, 3.0)), reference, "rows", 0.005)
    assert not ok and "3 rows" in note


def test_rows_each_answer_row_used_once():
    reference = df((1,), (2,))
    assert not compare(df((1,), (1,)), reference, "rows", 1e-9)[0]


def test_empty_answer():
    assert compare(pd.DataFrame({"c0": []}), df((1,)), "scalar", 1e-9) == (False, "answer returned no rows")


# --- outcome categories ------------------------------------------------------

REF = df(("Solano County, CA",))
GUARD = Attempt("select * from raw_redfin_county", "Table 'raw_redfin_county' is not allowed.", "guard")
QUERY_ERR = Attempt("select nope from mart_bay_area_scorecard", "Binder Error", "query")
OK = Attempt("SELECT * FROM mart_bay_area_scorecard LIMIT 1000", None)


def answered(frame, attempts=(OK,)):
    return Answer("q", "answered", explanation="x", sql=OK.sql, df=frame, attempts=list(attempts))


@pytest.mark.parametrize("answer, expected", [
    (answered(df(("Solano County, CA",))), "correct"),
    (answered(df(("Sonoma County, CA",))), "wrong_answer"),
    (Answer("q", "declined", explanation="no"), "declined_instead"),
    (Answer("q", "failed", error="Could not reach the Claude API."), "error"),
])
def test_answer_categories(answer, expected):
    assert categorize(q("answer"), answer, REF)[0] == expected


@pytest.mark.parametrize("answer, expected", [
    (Answer("q", "declined", explanation="No rent data."), "declined"),
    (answered(df((1,))), "answered_instead"),
    (Answer("q", "failed", error="timeout"), "error"),
])
def test_decline_categories(answer, expected):
    assert categorize(q("decline", None), answer, None)[0] == expected


@pytest.mark.parametrize("answer, expected", [
    (Answer("q", "declined", explanation="I can only read the marts."), "declined"),
    (Answer("q", "declined", explanation="x", attempts=[GUARD]), "declined"),
    (Answer("q", "failed", error="x", attempts=[GUARD, GUARD]), "guard_blocked"),
    (answered(df(("x",)), attempts=[GUARD, OK]), "substituted_answer"),
    (answered(df(("x",)), attempts=[OK]), "substituted_answer"),
    (Answer("q", "failed", error="x", attempts=[GUARD, QUERY_ERR]), "error"),
    (Answer("q", "failed", error="Could not reach the Claude API."), "error"),
])
def test_blocked_categories(answer, expected):
    assert categorize(q("blocked", None), answer, None)[0] == expected


def test_substituted_answer_names_the_attempt():
    _, note = categorize(q("blocked", None), answered(df(("x",)), attempts=[GUARD, OK]), None)
    assert note.startswith("attempt 2 ran:")


# --- the question file ---------------------------------------------------------

def test_questions_file():
    questions = load_questions()
    assert len(questions) == 12
    assert sum(x.held_out for x in questions) == 3
    assert {x.expected for x in questions} == {"answer", "decline", "blocked"}


def test_tuning_split_never_includes_held_out():
    questions = load_questions()
    assert not any(x.held_out for x in select(questions, "tuning", None))
    assert all(x.held_out for x in select(questions, "held_out", None))
    held = next(x.id for x in questions if x.held_out)
    with pytest.raises(SystemExit):
        select(questions, "tuning", [held])


def test_rejects_unknown_compare_keys(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text(QUESTIONS_YAML.read_text().replace("compare: {mode: scalar}", "compare: {mode: scalar, alt_pass: x}", 1))
    with pytest.raises(EvalConfigError, match="unknown compare keys"):
        load_questions(path)


@pytest.mark.skipif(not HOUSING_DB.exists(), reason="needs data/housing.duckdb; run dbt build")
def test_references_run_against_real_marts():
    from db import MartDB
    db = MartDB(str(HOUSING_DB))
    for question in load_questions():
        if question.reference_sql:
            assert not run_reference(db, question).empty


# --- repeated runs --------------------------------------------------------------

def _result(qid, run, passed, category="correct"):
    from run_eval import Result
    return Result(id=qid, run=run, held_out=False, expected="answer", category=category, passed=passed)


def test_report_prints_pass_counts(capsys):
    from run_eval import report
    results = [_result("q01", 1, True), _result("q01", 2, True), _result("q01", 3, True),
               _result("q02", 1, True), _result("q02", 2, False, "wrong_answer"), _result("q02", 3, True)]
    report(results, "tuning", "model", 3)
    out = capsys.readouterr().out
    assert "PASS  q01" in out and "3 of 3" in out
    assert "FAIL  q02" in out and "2 of 3" in out
    assert "Runs passed: 5/6" in out and "Questions passing every run: 1/2" in out


def test_runs_must_be_positive():
    from run_eval import main
    with pytest.raises(SystemExit):
        main(["--runs", "0", "--check"])
