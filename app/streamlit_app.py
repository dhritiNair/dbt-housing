"""Ask a question about Bay Area housing; get the answer table and the SQL used.

Run from the project root:  streamlit run app/streamlit_app.py
"""

import streamlit as st

from config import ConfigError, load_settings
from db import QueryError
from pipeline import Answer, Pipeline

st.set_page_config(page_title="Bay Area Housing Q&A", layout="wide")


@st.cache_resource
def get_pipeline() -> Pipeline:
    # Shared across sessions: MartDB gives every query its own database,
    # and the Anthropic client is safe to share between threads.
    return Pipeline.from_settings(load_settings())


def render(answer: Answer) -> None:
    if answer.status == "declined":
        st.info(answer.explanation or "The housing data can't answer that question.")
        return

    if answer.status == "failed":
        st.error(answer.error)
        if answer.sql:
            with st.expander("Last SQL tried", expanded=False):
                st.code(answer.sql, language="sql")
        return

    st.write(answer.explanation)
    with st.expander("SQL", expanded=False):
        st.code(answer.sql, language="sql")
        if len(answer.attempts) > 1:
            st.caption(f"The first query failed and was corrected. Error: {answer.attempts[0].error}")

    if answer.df.empty:
        st.info("The query ran but returned no rows.")
    else:
        st.dataframe(answer.df, hide_index=True, width="stretch")
        if answer.truncated:
            st.caption(f"Showing the first {len(answer.df):,} rows.")


st.title("Bay Area Housing Q&A")
st.caption(
    "Ask about prices, sales and supply in the nine Bay Area counties. "
    "Answers come from Redfin county data modeled in dbt."
)

try:
    pipeline = get_pipeline()
except (ConfigError, QueryError) as e:
    st.error(str(e))
    st.stop()

with st.form("ask"):
    question = st.text_input(
        "Question",
        placeholder="Which county has the highest median sale price right now?",
    )
    submitted = st.form_submit_button("Ask")

if submitted:
    with st.spinner("Writing and running SQL..."):
        st.session_state["answer"] = pipeline.ask(question)

if "answer" in st.session_state:
    render(st.session_state["answer"])
