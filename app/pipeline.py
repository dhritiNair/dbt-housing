"""Question in, answer out: draft SQL with Claude, validate it, run it.

Shared by the Streamlit UI and the eval so both exercise the same path.
"""

from dataclasses import dataclass, field

import pandas as pd

from config import Settings
from db import MartDB, QueryError
from llm import Chart, LLMError, SQLWriter
from schema_context import build_schema_context
from sql_guard import SQLGuardError, validate

MAX_ATTEMPTS = 2  # the first try plus one retry with the error message


@dataclass(frozen=True)
class Attempt:
    sql: str
    error: str | None
    stage: str | None = None  # where it failed: "guard" or "query"; None if it ran


@dataclass
class Answer:
    question: str
    status: str  # "answered", "declined" or "failed"
    explanation: str = ""
    sql: str | None = None  # the validated SQL that ran
    df: pd.DataFrame | None = None
    truncated: bool = False
    chart: Chart | None = None
    error: str | None = None
    attempts: list[Attempt] = field(default_factory=list)


class Pipeline:
    def __init__(self, settings: Settings, db: MartDB, writer: SQLWriter):
        self.settings = settings
        self.db = db
        self.writer = writer

    @classmethod
    def from_settings(cls, settings: Settings) -> "Pipeline":
        db = MartDB(
            settings.db_path,
            max_rows=settings.max_rows,
            timeout_s=settings.query_timeout_s,
            memory_limit=settings.memory_limit,
        )
        return cls(settings, db, SQLWriter(settings, build_schema_context(db)))

    def ask(self, question: str) -> Answer:
        question = question.strip()
        if not question:
            return Answer(question, "failed", error="Ask a question about Bay Area housing.")

        attempts: list[Attempt] = []
        failed: tuple[str, str] | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                draft = self.writer.draft(question, failed)
            except LLMError as e:
                return Answer(question, "failed", error=str(e), attempts=attempts)

            if draft.status == "decline":
                return Answer(question, "declined", explanation=draft.explanation, attempts=attempts)

            try:
                validated = validate(draft.sql, max_rows=self.settings.max_rows)
                result = self.db.run(validated.sql)
            except (SQLGuardError, QueryError) as e:
                stage = "guard" if isinstance(e, SQLGuardError) else "query"
                attempts.append(Attempt(draft.sql, str(e), stage))
                failed = (draft.sql, str(e))
                continue

            attempts.append(Attempt(validated.sql, None))
            return Answer(
                question,
                "answered",
                explanation=draft.explanation,
                sql=validated.sql,
                df=result.df,
                truncated=result.truncated,
                chart=draft.chart,
                attempts=attempts,
            )

        return Answer(
            question,
            "failed",
            sql=failed[0],
            error=f"No working query after {MAX_ATTEMPTS} attempts. Last error: {failed[1]}",
            attempts=attempts,
        )
