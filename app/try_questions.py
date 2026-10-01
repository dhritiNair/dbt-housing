"""Run three questions through the full pipeline (calls the Claude API).

Run from app/:  python try_questions.py
"""

from config import load_settings
from pipeline import Pipeline

QUESTIONS = [
    "Which Bay Area county has the highest median sale price right now?",
    "How has San Francisco's 12-month average sale price changed since 2020?",
    "What is the average rent in Oakland?",  # should be declined
]

pipeline = Pipeline.from_settings(load_settings())
for question in QUESTIONS:
    answer = pipeline.ask(question)
    print(f"\nQ: {question}\n   status: {answer.status}  attempts: {len(answer.attempts)}")
    if answer.explanation:
        print(f"   {answer.explanation}")
    if answer.error:
        print(f"   error: {answer.error}")
    if answer.sql:
        print("   SQL:\n" + "\n".join("     " + line for line in answer.sql.splitlines()))
        print(f"   chart: {answer.chart}")
        print(answer.df.head(10).to_string(index=False))
