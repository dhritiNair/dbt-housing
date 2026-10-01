"""llm.py with a fake client: response parsing, declines, and error messages.
No network calls."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from config import ConfigError, Settings, load_settings
from llm import LLMError, SQLWriter

FAKE_KEY = "sk-ant-test-0000000000000000"
SETTINGS = Settings(api_key=FAKE_KEY, llm_timeout_s=5)
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(payload=None, stop_reason="end_turn", text=None):
    text = json.dumps(payload) if text is None else text
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        _request_id="req_test",
    )


class FakeClient:
    def __init__(self, result):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._result = result

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _writer(result):
    client = FakeClient(result)
    return SQLWriter(SETTINGS, "SCHEMA CONTEXT", client=client), client


ANSWER = {
    "status": "answer",
    "sql": "  select county from mart_bay_area_scorecard  ",
    "chart": {"type": "bar", "x": "county", "y": "n", "color": ""},
    "explanation": "Counties\nin the  scorecard.",
}


def test_parses_answer():
    writer, client = _writer(_response(ANSWER))
    draft = writer.draft("which counties?")
    assert draft.status == "answer"
    assert draft.sql == "select county from mart_bay_area_scorecard"
    assert draft.chart.type == "bar" and draft.chart.x == "county"
    assert draft.explanation == "Counties in the scorecard."  # collapsed to one line


def test_request_shape():
    writer, client = _writer(_response(ANSWER))
    writer.draft("q")
    call = client.calls[0]
    assert call["model"] == SETTINGS.model
    assert "SCHEMA CONTEXT" in call["system"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == SETTINGS.effort
    assert call["messages"] == [{"role": "user", "content": "q"}]


def test_retry_includes_failed_sql_and_error():
    writer, client = _writer(_response(ANSWER))
    writer.draft("q", failed=("select * from nope", "Table 'nope' is not allowed."))
    content = client.calls[0]["messages"][0]["content"]
    assert "select * from nope" in content and "is not allowed" in content


def test_decline_passes_through():
    writer, _ = _writer(_response({**ANSWER, "status": "decline", "sql": ""}))
    assert writer.draft("what's the rent?").status == "decline"


def test_refusal_becomes_decline():
    writer, _ = _writer(_response(text="", stop_reason="refusal"))
    assert writer.draft("q").status == "decline"


def test_truncated_response():
    writer, _ = _writer(_response(text='{"status": "ans', stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="cut off"):
        writer.draft("q")


def test_unreadable_response():
    writer, _ = _writer(_response(text="not json"))
    with pytest.raises(LLMError, match="could not read"):
        writer.draft("q")


def _status_error(cls, status, message):
    response = httpx2.Response(status, request=REQUEST)
    return cls(message, response=response, body=None)


@pytest.mark.parametrize("error, expected", [
    (anthropic.APITimeoutError(request=REQUEST), "did not respond within 5s"),
    (anthropic.APIConnectionError(request=REQUEST), "Could not reach"),
    (_status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), "key was rejected"),
    (_status_error(anthropic.PermissionDeniedError, 403, "denied"), "does not have access"),
    (_status_error(anthropic.NotFoundError, 404, "model: nope"), "CLAUDE_MODEL"),
    (_status_error(anthropic.RateLimitError, 429, "slow down"), "rate limit"),
    (_status_error(anthropic.InternalServerError, 500, "boom"), "error (500)"),
    (_status_error(anthropic.BadRequestError, 400, "bad schema"), "rejected the request: bad schema"),
])
def test_api_errors_have_clear_messages(error, expected):
    writer, _ = _writer(error)
    with pytest.raises(LLMError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        writer.draft("q")


def test_key_never_in_error_message():
    error = _status_error(anthropic.BadRequestError, 400, f"bad header {FAKE_KEY}")
    writer, _ = _writer(error)
    with pytest.raises(LLMError) as info:
        writer.draft("q")
    assert FAKE_KEY not in str(info.value) and "[redacted]" in str(info.value)


def test_key_not_in_settings_repr():
    assert FAKE_KEY not in repr(SETTINGS)


def test_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("config.PROJECT_ROOT", tmp_path)  # no .env there
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        load_settings()


def test_model_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setattr("config.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    assert load_settings().model == "claude-sonnet-5-5"
