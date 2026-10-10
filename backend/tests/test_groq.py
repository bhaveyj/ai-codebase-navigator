import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from navigator import groq
from navigator.contracts import DomainError


def settings(**overrides):
    values = {
        "groq_api_key": "test-secret",
        "groq_model": "openai/gpt-oss-20b",
        "groq_timeout_seconds": 30,
        "groq_max_completion_tokens": 3072,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_generate_groq_sends_grounding_prompt_and_returns_json(monkeypatch):
    answer = {"blocks": [{"text": "A fact", "kind": "fact", "citationIds": ["S1"], "edgeIds": []}]}
    post = Mock(return_value=httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}))
    monkeypatch.setattr(groq.httpx, "post", post)

    assert groq.generate_groq("Cite supplied evidence", {"sources": [{"citationId": "S1"}]}, settings()) == answer
    args, kwargs = post.call_args
    assert args == (groq.GROQ_CHAT_URL,)
    assert kwargs["headers"]["Authorization"] == "Bearer test-secret"
    assert kwargs["timeout"] == 30
    assert kwargs["json"]["model"] == "openai/gpt-oss-20b"
    assert kwargs["json"]["max_completion_tokens"] == 3072
    answer_format = kwargs["json"]["response_format"]
    assert answer_format["type"] == "json_schema"
    assert answer_format["json_schema"]["strict"] is True
    assert answer_format["json_schema"]["schema"]["required"] == ["blocks"]
    assert answer_format["json_schema"]["schema"]["properties"]["blocks"]["items"]["required"] == ["text", "kind", "citationIds", "edgeIds"]
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "Cite supplied evidence"}
    assert json.loads(kwargs["json"]["messages"][1]["content"]) == {"sources": [{"citationId": "S1"}]}


def test_generate_groq_requires_api_key_before_request(monkeypatch):
    post = Mock()
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(DomainError) as caught:
        groq.generate_groq("system", {}, settings(groq_api_key=""))
    assert caught.value.code == "GROQ_UNAVAILABLE"
    assert not caught.value.retryable
    post.assert_not_called()


@pytest.mark.parametrize(
    "status, code, retryable",
    [(401, "GROQ_ACCESS_DENIED", False), (400, "GROQ_REQUEST_INVALID", False), (503, "GROQ_UNAVAILABLE", True)],
)
def test_generate_groq_classifies_provider_errors_without_leaking_body(monkeypatch, status, code, retryable):
    post = Mock(return_value=httpx.Response(status, json={"error": {"message": "secret-provider-details"}}))
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(DomainError) as caught:
        groq.generate_groq("system", {}, settings())
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "secret-provider-details" not in caught.value.message


@pytest.mark.parametrize("header, expected", [("17.5", 17.5), ("oops", 60), ("999999", 86400)])
def test_generate_groq_preserves_rate_limit_cooldown(monkeypatch, header, expected):
    post = Mock(return_value=httpx.Response(429, headers={"retry-after": header}, text="private quota details"))
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(DomainError) as caught:
        groq.generate_groq("system", {}, settings())
    assert caught.value.code == "GROQ_RATE_LIMITED"
    assert caught.value.status == 429
    assert caught.value.retry_after_seconds == expected
    assert "private quota details" not in caught.value.message


def test_generate_groq_identifies_single_request_token_budget(monkeypatch):
    post = Mock(return_value=httpx.Response(413, json={"error": {
        "type": "tokens", "code": "rate_limit_exceeded",
        "message": "secret-provider-details Limit 8000 Requested 8531",
    }}))
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(DomainError) as caught:
        groq.generate_groq("system", {}, settings())
    assert caught.value.code == "GROQ_TOKEN_BUDGET"
    assert not caught.value.retryable
    assert "token-per-minute" in caught.value.message
    assert "secret-provider-details" not in caught.value.message


def test_generate_groq_classifies_connection_failure(monkeypatch):
    post = Mock(side_effect=httpx.TimeoutException("timeout with secret details"))
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(DomainError) as caught:
        groq.generate_groq("system", {}, settings())
    assert caught.value.code == "GROQ_UNAVAILABLE"
    assert "secret details" not in caught.value.message


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "stop", "message": {"content": "not json"}},
        {"finish_reason": "stop", "message": {"content": "[]"}},
    ],
)
def test_generate_groq_rejects_truncated_or_invalid_output(monkeypatch, choice):
    post = Mock(return_value=httpx.Response(200, json={"choices": [choice]}))
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(ValueError):
        groq.generate_groq("system", {}, settings())


def test_generate_groq_reports_truncated_answer(monkeypatch):
    post = Mock(return_value=httpx.Response(200, json={"choices": [{
        "finish_reason": "length", "message": {"content": "{}"},
    }]}))
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(DomainError) as caught:
        groq.generate_groq("system", {}, settings())
    assert caught.value.code == "GROQ_ANSWER_TRUNCATED"
    assert not caught.value.retryable


def test_generate_groq_rejects_malformed_response_envelope(monkeypatch):
    post = Mock(return_value=httpx.Response(200, json={"choices": [None]}))
    monkeypatch.setattr(groq.httpx, "post", post)
    with pytest.raises(ValueError, match="valid JSON answer"):
        groq.generate_groq("system", {}, settings())
