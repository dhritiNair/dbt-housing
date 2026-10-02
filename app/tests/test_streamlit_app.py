"""Render the Streamlit page with a fake pipeline. No network calls."""

from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
import pipeline
from llm import Chart
from pipeline import Answer, Attempt

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")
SQL = "SELECT\n  county\nFROM mart_bay_area_scorecard\nLIMIT 1000"


class FakePipeline:
    def __init__(self, answer):
        self.answer = answer
        self.questions = []

    def ask(self, question):
        self.questions.append(question)
        return self.answer


@pytest.fixture
def app(monkeypatch):
    def start(answer=None, settings_error=None):
        fake = FakePipeline(answer)

        def load_settings():
            if settings_error:
                raise settings_error
            return config.Settings(api_key="unused")

        monkeypatch.setattr(config, "load_settings", load_settings)
        monkeypatch.setattr(pipeline.Pipeline, "from_settings", classmethod(lambda cls, s: fake))
        st.cache_resource.clear()
        at = AppTest.from_file(APP).run()
        return at, fake

    return start


def ask(at, question):
    at.text_input[0].input(question)
    at.button[0].click()
    return at.run()


def test_answered(app):
    answer = Answer(
        "q", "answered", explanation="San Mateo has the highest median price.",
        sql=SQL, df=pd.DataFrame({"county": ["San Mateo County, CA"]}),
        chart=Chart("none", "", "", ""), attempts=[Attempt(SQL, None)],
    )
    at, fake = app(answer)
    at = ask(at, "Which county is priciest?")
    assert fake.questions == ["Which county is priciest?"]
    assert "San Mateo has the highest median price." in at.markdown[-1].value
    assert at.expander[0].label == "SQL"
    assert at.code[0].value == SQL
    assert at.dataframe[0].value["county"].tolist() == ["San Mateo County, CA"]
    assert not at.exception and not at.error


def test_corrected_query_is_noted(app):
    answer = Answer(
        "q", "answered", explanation="x", sql=SQL, df=pd.DataFrame({"n": [1]}),
        attempts=[Attempt("select * from raw", "Table 'raw' is not allowed."), Attempt(SQL, None)],
    )
    at = ask(app(answer)[0], "q")
    assert "first query failed" in at.caption[-1].value


def test_truncated_and_empty(app):
    truncated = Answer("q", "answered", explanation="x", sql=SQL, df=pd.DataFrame({"n": range(5)}), truncated=True)
    assert "first 5 rows" in ask(app(truncated)[0], "q").caption[-1].value
    empty = Answer("q", "answered", explanation="x", sql=SQL, df=pd.DataFrame({"n": []}))
    assert "no rows" in ask(app(empty)[0], "q").info[0].value


def test_declined(app):
    at = ask(app(Answer("q", "declined", explanation="The data has no rents."))[0], "rent?")
    assert at.info[0].value == "The data has no rents."
    assert not at.code and not at.dataframe


def test_failed(app):
    answer = Answer("q", "failed", sql="select * from raw", error="No working query after 2 attempts.")
    at = ask(app(answer)[0], "q")
    assert at.error[0].value == "No working query after 2 attempts."
    assert at.expander[0].label == "Last SQL tried"


def test_missing_key_message(app):
    at, _ = app(settings_error=config.ConfigError("ANTHROPIC_API_KEY is not set."))
    assert at.error[0].value == "ANTHROPIC_API_KEY is not set."
    assert not at.text_input
