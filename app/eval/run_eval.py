"""Score the question-to-SQL pipeline against eval/questions.yaml.

Run from app/:
  python eval/run_eval.py                    # tuning questions; held-out ones are skipped
  python eval/run_eval.py --split held_out   # held-out questions only, for the final check
  python eval/run_eval.py --check            # run the reference SQL only; no API calls
  python eval/run_eval.py --only q04_san_jose_supply --json report.json
  python eval/run_eval.py --runs 1           # one pass per question instead of three

Reference queries run at eval time against the same mart snapshot the pipeline
reads, so expected answers follow the data. Each question runs --runs times
(default 3), and each run calls the Claude API once or twice.
"""

import argparse
import datetime as dt
import json
import math
import numbers
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd
import yaml

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from config import load_settings  # noqa: E402
from db import MartDB  # noqa: E402
from pipeline import Answer, Pipeline  # noqa: E402
from sql_guard import validate  # noqa: E402

QUESTIONS_YAML = APP / "eval" / "questions.yaml"
EXPECTED = {"answer", "decline", "blocked"}
MODES = {"scalar", "top_row", "rows"}
DEFAULT_TOLERANCE = 1e-9
PASSING = {"correct", "declined", "guard_blocked"}


class EvalConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    question: str
    expected: str
    held_out: bool
    reference_sql: str | None
    mode: str | None = None
    tolerance: float = DEFAULT_TOLERANCE


@dataclass
class Result:
    id: str
    run: int
    held_out: bool
    expected: str
    category: str
    passed: bool
    note: str = ""
    status: str = ""
    attempts: int = 0
    seconds: float = 0.0
    explanation: str = ""
    sql: str | None = None
    attempt_log: list[dict] = field(default_factory=list)


# --- loading ----------------------------------------------------------------

def load_questions(path: Path = QUESTIONS_YAML) -> list[Question]:
    doc = yaml.safe_load(path.read_text())
    questions, seen = [], set()
    for raw in doc["questions"]:
        qid = raw["id"]
        if qid in seen:
            raise EvalConfigError(f"Duplicate id {qid}")
        seen.add(qid)
        if raw["expected"] not in EXPECTED:
            raise EvalConfigError(f"{qid}: expected must be one of {sorted(EXPECTED)}")
        if not isinstance(raw.get("held_out"), bool):
            raise EvalConfigError(f"{qid}: held_out must be true or false")
        compare = raw.get("compare") or {}
        if raw["expected"] == "answer":
            if not raw.get("reference_sql") or compare.get("mode") not in MODES:
                raise EvalConfigError(f"{qid}: answer questions need reference_sql and compare.mode")
            if set(compare) - {"mode", "tolerance"}:
                raise EvalConfigError(f"{qid}: unknown compare keys {sorted(set(compare) - {'mode', 'tolerance'})}")
        elif raw.get("reference_sql"):
            raise EvalConfigError(f"{qid}: only answer questions have reference_sql")
        questions.append(Question(
            id=qid,
            type=raw["type"],
            question=raw["question"],
            expected=raw["expected"],
            held_out=raw["held_out"],
            reference_sql=raw.get("reference_sql"),
            mode=compare.get("mode"),
            tolerance=float(compare.get("tolerance", DEFAULT_TOLERANCE)),
        ))
    return questions


def run_reference(db: MartDB, q: Question) -> pd.DataFrame:
    """Run the reference through the same guard and database as the pipeline."""
    df = db.run(validate(q.reference_sql).sql).df
    if df.empty:
        raise EvalConfigError(f"{q.id}: reference returned no rows")
    if q.mode in {"scalar", "top_row"} and len(df) != 1:
        raise EvalConfigError(f"{q.id}: {q.mode} reference must return one row, got {len(df)}")
    if q.mode == "scalar" and df.shape[1] != 1:
        raise EvalConfigError(f"{q.id}: scalar reference must return one column, got {df.shape[1]}")
    return df


# --- comparing values -------------------------------------------------------

def normalize(value):
    """Numbers become float, dates become ISO dates, strings are stripped."""
    if value is None or value is pd.NA or value is pd.NaT:  # NaT is a datetime subclass
        return None
    if isinstance(value, (pd.Timestamp, dt.datetime)):
        if value.hour == value.minute == value.second == value.microsecond == 0:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, numbers.Number):
        number = float(value)
        return None if math.isnan(number) else number
    return str(value).strip()


def values_match(answer, reference, tolerance: float = DEFAULT_TOLERANCE) -> bool:
    a, r = normalize(answer), normalize(reference)
    if isinstance(a, float) and isinstance(r, float):
        return abs(a - r) <= tolerance * abs(r)  # relative; a zero reference needs an exact zero
    return a == r


def _row_matches(answer_row, reference_row, tolerance) -> bool:
    return all(values_match(a, r, tolerance) for a, r in zip(answer_row, reference_row))


def compare(answer: pd.DataFrame, reference: pd.DataFrame, mode: str, tolerance: float) -> tuple[bool, str]:
    """scalar and top_row: each reference value must appear in some column of the
    answer's first row. rows: columns are compared by position. Answer column
    names are ignored, and so are answer columns no reference value needs."""
    if answer.empty:
        return False, "answer returned no rows"
    if mode in {"scalar", "top_row"}:
        return _first_row_contains(list(answer.iloc[0]), list(reference.iloc[0]), tolerance)

    width = reference.shape[1]
    if answer.shape[1] < width:
        return False, f"answer has {answer.shape[1]} columns, reference needs {width}"
    ref_rows = [list(r) for r in reference.itertuples(index=False)]
    ans_rows = [list(r)[:width] for r in answer.itertuples(index=False)]
    if len(ans_rows) != len(ref_rows):
        return False, f"answer has {len(ans_rows)} rows, reference has {len(ref_rows)}"
    unused = list(range(len(ans_rows)))
    for ref_row in ref_rows:
        match = next((i for i in unused if _row_matches(ans_rows[i], ref_row, tolerance)), None)
        if match is None:
            return False, f"no answer row matches reference row {[normalize(v) for v in ref_row]}"
        unused.remove(match)
    return True, ""


def _first_row_contains(answer_row, reference_row, tolerance) -> tuple[bool, str]:
    """Each reference value must match a different column of the answer row."""
    unused = list(range(len(answer_row)))
    for ref in reference_row:
        match = next((j for j in unused if values_match(answer_row[j], ref, tolerance)), None)
        if match is None:
            got = [normalize(v) for v in answer_row]
            return False, f"expected {[normalize(v) for v in reference_row]} in the first row, got {got}"
        unused.remove(match)
    return True, ""


# --- scoring an answer ------------------------------------------------------

def categorize(q: Question, answer: Answer, reference: pd.DataFrame | None) -> tuple[str, str]:
    """Return (category, note)."""
    if q.expected == "answer":
        if answer.status == "answered":
            ok, note = compare(answer.df, reference, q.mode, q.tolerance)
            return ("correct" if ok else "wrong_answer"), note
        if answer.status == "declined":
            return "declined_instead", answer.explanation
        return "error", answer.error or ""

    if q.expected == "decline":
        if answer.status == "declined":
            return "declined", ""
        if answer.status == "answered":
            return "answered_instead", f"ran: {_one_line(answer.sql)}"
        return "error", answer.error or ""

    # blocked
    if answer.status == "declined":
        guarded = sum(a.stage == "guard" for a in answer.attempts)
        return "declined", f"after {guarded} guard rejection(s)" if guarded else ""
    if answer.status == "answered":
        return "substituted_answer", f"attempt {len(answer.attempts)} ran: {_one_line(answer.sql)}"
    if answer.attempts and all(a.stage == "guard" for a in answer.attempts):
        return "guard_blocked", answer.attempts[-1].error or ""
    return "error", answer.error or ""


def _one_line(sql: str | None) -> str:
    return " ".join((sql or "").split())


def score(q: Question, pipeline: Pipeline, reference: pd.DataFrame | None, run: int = 1) -> Result:
    start = time.monotonic()
    answer = pipeline.ask(q.question)
    seconds = time.monotonic() - start
    category, note = categorize(q, answer, reference)
    return Result(
        id=q.id,
        run=run,
        held_out=q.held_out,
        expected=q.expected,
        category=category,
        passed=category in PASSING,
        note=note,
        status=answer.status,
        attempts=len(answer.attempts),
        seconds=round(seconds, 2),
        explanation=answer.explanation or answer.error or "",
        sql=answer.sql,
        attempt_log=[asdict(a) for a in answer.attempts],
    )


# --- command line -----------------------------------------------------------

def select(questions: list[Question], split: str, only: list[str] | None) -> list[Question]:
    if only:
        unknown = set(only) - {q.id for q in questions}
        if unknown:
            raise SystemExit(f"Unknown question id(s): {', '.join(sorted(unknown))}")
        chosen = [q for q in questions if q.id in only]
        if split == "tuning" and any(q.held_out for q in chosen):
            raise SystemExit("Held-out questions only run with --split held_out or --split all.")
        return chosen
    if split == "tuning":
        return [q for q in questions if not q.held_out]
    if split == "held_out":
        return [q for q in questions if q.held_out]
    return questions


def check(questions: list[Question], db: MartDB) -> int:
    """Run every reference query and show its result. No API calls."""
    for q in questions:
        if q.reference_sql is None:
            print(f"  {q.id:32} expected {q.expected}, no reference")
            continue
        df = run_reference(db, q)
        values = [normalize(v) for v in df.iloc[0]]
        more = f" (+{len(df) - 1} more rows)" if len(df) > 1 else ""
        print(f"  {q.id:32} {q.mode:8} first row {values}{more}")
    print(f"All {len(questions)} questions are well formed and their references run.")
    return 0


def by_question(results: list[Result]) -> dict[str, list[Result]]:
    grouped: dict[str, list[Result]] = {}
    for r in results:
        grouped.setdefault(r.id, []).append(r)
    return grouped


def _counts(results: list[Result]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.category] = counts.get(r.category, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))


def report(results: list[Result], split: str, model: str, runs: int) -> None:
    grouped = by_question(results)
    for qid, rs in grouped.items():
        passed = sum(r.passed for r in rs)
        mark = "PASS" if passed == len(rs) else "FAIL"
        held = " [held out]" if rs[0].held_out else ""
        seconds = sum(r.seconds for r in rs) / len(rs)
        print(f"{mark}  {qid:32} {passed} of {len(rs)}  {_counts(rs):34} avg {seconds:.1f}s{held}")
        for r in rs:
            if r.note and not r.passed:
                print(f"      run {r.run}: {r.note}")

    passed_runs = sum(r.passed for r in results)
    always = sum(all(r.passed for r in rs) for rs in grouped.values())
    print(f"\nModel {model}, split {split}, {runs} run(s) per question")
    print(f"Runs passed: {passed_runs}/{len(results)} ({passed_runs / len(results):.0%})")
    print(f"Questions passing every run: {always}/{len(grouped)}")
    print("Outcomes: " + _counts(results))

    substituted = [r for r in results if r.category == "substituted_answer"]
    if substituted:
        print("\nSubstituted answers (mart data returned for an unsafe request; not passes):")
        for r in substituted:
            print(f"  {r.id} run {r.run}: {r.note}")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["tuning", "held_out", "all"], default="tuning")
    parser.add_argument("--only", nargs="+", metavar="ID", help="run only these question ids")
    parser.add_argument("--check", action="store_true", help="run reference SQL only; no API calls")
    parser.add_argument("--runs", type=positive_int, default=3, metavar="N",
                        help="times to run each question (default 3)")
    parser.add_argument("--json", type=Path, metavar="PATH", help="write a JSON report")
    args = parser.parse_args(argv)

    questions = select(load_questions(), args.split, args.only)
    settings = load_settings(require_key=not args.check)
    db = MartDB(settings.db_path, max_rows=settings.max_rows,
                timeout_s=settings.query_timeout_s, memory_limit=settings.memory_limit)

    if args.check:
        return check(questions, db)

    references = {q.id: run_reference(db, q) for q in questions if q.reference_sql}
    pipeline = Pipeline.from_settings(settings)
    results = []
    for q in questions:
        for run in range(1, args.runs + 1):
            results.append(score(q, pipeline, references.get(q.id), run))
            print(f"  ran {q.id} ({run} of {args.runs})", file=sys.stderr)

    report(results, args.split, settings.model, args.runs)
    if args.json:
        args.json.write_text(json.dumps({
            "run_at": dt.datetime.now().isoformat(timespec="seconds"),
            "model": settings.model,
            "effort": settings.effort,
            "split": args.split,
            "runs": args.runs,
            "passed_runs": sum(r.passed for r in results),
            "total_runs": len(results),
            "questions": [
                {"id": qid, "passed_runs": sum(r.passed for r in rs), "runs": len(rs)}
                for qid, rs in by_question(results).items()
            ],
            "results": [asdict(r) for r in results],
        }, indent=2, default=str))
        print(f"\nWrote {args.json}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
