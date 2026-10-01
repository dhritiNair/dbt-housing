"""Ask Claude for one DuckDB query that answers a question, as structured output.

The API key comes only from Settings (environment or .env). It is never
logged, and every error message built from an API response is scrubbed of it.
"""

import json
import logging
from dataclasses import dataclass

import anthropic

from config import Settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You answer questions about the Bay Area housing market by writing one DuckDB SQL query \
over three dbt mart tables. The app validates and runs your query and shows the user \
the result table, an optional chart, and your explanation.

Query rules:
- Exactly one read-only SELECT (CTEs are fine), using only the three tables below.
- Use the exact county strings listed below. Map short names to counties: "SF" is \
'San Francisco County, CA', "Oakland" is in 'Alameda County, CA', "San Jose" is in \
'Santa Clara County, CA'. The data has no city-level detail; when the user names a city, \
use its county and say so in the explanation.
- "Latest" or "now" means the most recent month in the data, not today's date.
- Follow the grain and moving-average rules below.
- Give columns readable aliases, round ratios for display only when the user asks for \
percentages, and order rows meaningfully.

Decline (status "decline", empty sql) when the tables cannot answer the question, for \
example rents, mortgage rates, forecasts, places outside the nine counties, or anything \
not about this housing data. The explanation then says briefly what the data does cover.

chart: "line" for a value over time (x = the date column), "bar" for a value compared \
across categories, "none" when a table is clearer. x, y and color must be column names \
from your query's output; use an empty string for color when there is no series column, \
and empty strings for all three when type is "none".

explanation: one plain sentence describing what the result shows and any filter a \
reader should know about, such as a city mapped to its county.

{schema}
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["answer", "decline"]},
        "sql": {"type": "string"},
        "chart": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["line", "bar", "none"]},
                "x": {"type": "string"},
                "y": {"type": "string"},
                "color": {"type": "string"},
            },
            "required": ["type", "x", "y", "color"],
            "additionalProperties": False,
        },
        "explanation": {"type": "string"},
    },
    "required": ["status", "sql", "chart", "explanation"],
    "additionalProperties": False,
}


class LLMError(RuntimeError):
    """The model call failed. The message is safe to show the user."""


@dataclass(frozen=True)
class Chart:
    type: str
    x: str
    y: str
    color: str


@dataclass(frozen=True)
class Draft:
    status: str  # "answer" or "decline"
    sql: str
    chart: Chart
    explanation: str


class SQLWriter:
    def __init__(self, settings: Settings, schema_context: str, client: anthropic.Anthropic | None = None):
        self._settings = settings
        self._system = SYSTEM_PROMPT.format(schema=schema_context)
        self._client = client or anthropic.Anthropic(
            api_key=settings.api_key,  # explicit, so no other credential source is used
            timeout=settings.llm_timeout_s,
            max_retries=1,
        )

    def draft(self, question: str, failed: tuple[str, str] | None = None) -> Draft:
        """Return a draft for the question. `failed` is (sql, error) from a
        previous attempt; it is sent as context so Claude can correct it."""
        content = question
        if failed:
            sql, error = failed
            content = (
                f"{question}\n\nYour previous query failed.\n"
                f"Query:\n{sql}\n\nError:\n{error}\n\n"
                "Write a corrected query, or decline if the tables cannot answer the question."
            )

        try:
            response = self._client.messages.create(
                model=self._settings.model,
                max_tokens=16000,
                system=self._system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": self._settings.effort,
                    "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
                },
            )
        except anthropic.APITimeoutError as e:
            raise LLMError(f"Claude did not respond within {self._settings.llm_timeout_s:g}s. Try again.") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError("The Anthropic API key does not have access to this model.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Model {self._settings.model!r} was not found. Check CLAUDE_MODEL.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("The Claude API rate limit was reached. Wait a moment and try again.") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"The Claude API rejected the request: {self._scrub(e.message)}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"The Claude API returned an error ({e.status_code}). Try again shortly.") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Claude API. Check your network connection.") from e

        log.info("claude request_id=%s stop_reason=%s", response._request_id, response.stop_reason)

        if response.stop_reason == "refusal":
            return Draft("decline", "", Chart("none", "", "", ""), "Claude declined to answer this question.")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's response was cut off before it finished. Try a simpler question.")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
            chart = Chart(**data["chart"])
            explanation = " ".join(data["explanation"].split())  # one line
            return Draft(data["status"], data["sql"].strip(), chart, explanation)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise LLMError("Claude returned a response the app could not read. Try again.") from e

    def _scrub(self, message: str) -> str:
        key = self._settings.api_key
        return message.replace(key, "[redacted]") if key else message
