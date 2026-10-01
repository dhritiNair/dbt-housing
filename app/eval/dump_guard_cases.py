"""Write every SQL case from tests/test_sql_guard.py to eval/guard_cases.txt
with the guard's verdict, for human review.

Run from app/:  python eval/dump_guard_cases.py
"""

import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

import tests.test_sql_guard as cases  # noqa: E402
from sql_guard import SQLGuardError, validate  # noqa: E402

OUT = APP / "eval" / "guard_cases.txt"


def collect() -> list[str]:
    seen: dict[str, None] = {}
    for name in dir(cases):
        for mark in getattr(getattr(cases, name), "pytestmark", []):
            if mark.name != "parametrize":
                continue
            argnames, values = mark.args[0], mark.args[1]
            if argnames == "table":
                values = [f"select * from {v}" for v in values]
            elif not argnames.startswith("sql"):
                continue
            for value in values:
                seen.setdefault(value[0] if isinstance(value, tuple) else value)
    return list(seen)


def main() -> None:
    accepted, rejected = [], []
    for sql in collect():
        try:
            validate(sql)
            accepted.append(sql)
        except SQLGuardError as e:
            rejected.append((sql, str(e).split(" Use only")[0]))

    one_line = lambda s: " ".join(s.split()) or repr(s)  # noqa: E731
    lines = [f"ACCEPTED ({len(accepted)})"]
    lines += [f"  ok   {one_line(s)}" for s in accepted]
    lines += ["", f"REJECTED ({len(rejected)})"]
    for sql, reason in rejected:
        lines += [f"  no   {one_line(sql)}", f"         -> {reason}"]
    OUT.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(accepted)} accepted and {len(rejected)} rejected cases to {OUT.relative_to(APP)}")


if __name__ == "__main__":
    main()
