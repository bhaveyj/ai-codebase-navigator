import json
import math
from types import SimpleNamespace

import httpx
import pytest

from navigator import cloudflare
from navigator.contracts import DomainError


ORIGINAL_HTTPX_CLIENT = httpx.Client


def settings(**overrides):
    values = {
        "cloudflare_api_token": "test-token",
        "cloudflare_account_id": "test-account",
        "cloudflare_embedding_model": "@cf/qwen/qwen3-embedding-0.6b",
        "cloudflare_timeout_seconds": 12,
        "effective_embedding_dimensions": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def mock_cloudflare(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cloudflare.httpx,
        "Client",
        lambda **kwargs: ORIGINAL_HTTPX_CLIENT(transport=transport, **kwargs),
    )


def test_batch_documents_use_one_request_and_preserve_order(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.path.endswith("/ai/run/@cf/qwen/qwen3-embedding-0.6b")
        assert request.content == b'{"text":["first source","second source"]}'
        return httpx.Response(
            200,
            json={"success": True, "result": {"shape": [2, 3], "data": [[3, 4, 0], [0, 0, 5]]}},
        )

    mock_cloudflare(monkeypatch, respond)
    vectors = cloudflare.embed_cloudflare(["first source", "second source"], settings())
    assert len(seen) == 1
    assert vectors == [[0.6, 0.8, 0.0], [0.0, 0.0, 1.0]]


def test_query_uses_qwen_retrieval_instruction(monkeypatch):
    def respond(request):
        text = request.read().decode()
        assert "Instruct: Given a question about a codebase" in text
        assert "Query: Where is auth checked?" in text
        return httpx.Response(200, json={"result": {"data": [[1, 0, 0]], "shape": [1, 3]}})

    mock_cloudflare(monkeypatch, respond)
    assert cloudflare.embed_cloudflare(["Where is auth checked?"], settings(), query=True) == [[1.0, 0.0, 0.0]]


def test_rate_limit_preserves_retry_after_without_exposing_body(monkeypatch):
    mock_cloudflare(
        monkeypatch,
        lambda request: httpx.Response(429, headers={"Retry-After": "37"}, json={"errors": ["secret error"]}),
    )
    with pytest.raises(DomainError) as caught:
        cloudflare.embed_cloudflare(["source"], settings())
    assert caught.value.code == "CLOUDFLARE_RATE_LIMITED"
    assert caught.value.status == 429
    assert caught.value.retry_after_seconds == 37
    assert "secret error" not in caught.value.message


def test_daily_free_allocation_pauses_until_utc_reset(monkeypatch):
    monkeypatch.setattr(cloudflare, "_utc_reset_seconds", lambda: 3600)
    mock_cloudflare(
        monkeypatch,
        lambda request: httpx.Response(429, json={"errors": [{"code": 3036, "message": "private provider detail"}]}),
    )
    with pytest.raises(DomainError) as caught:
        cloudflare.embed_cloudflare(["source"], settings())
    assert caught.value.code == "CLOUDFLARE_QUOTA_EXHAUSTED"
    assert caught.value.retry_after_seconds == 3600
    assert "private provider detail" not in caught.value.message


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"success": False, "result": {"data": [[1, 0, 0]]}}, "failure"),
        ({"result": {"shape": [2, 3], "data": [[1, 0, 0]]}}, "count"),
        ({"result": {"shape": [1, 2], "data": [[1, 0, 0]]}}, "shape"),
        ({"result": {"data": [[0, 0, 0]]}}, "zero"),
        ({"result": {"data": [[1, math.inf, 0]]}}, "nonfinite"),
        ({"result": {"data": [[True, 0, 0]]}}, "nonnumeric"),
    ],
)
def test_invalid_provider_vectors_are_rejected(monkeypatch, payload, reason):
    mock_cloudflare(monkeypatch, lambda request: httpx.Response(200, content=json.dumps(payload).encode()))
    with pytest.raises(DomainError) as caught:
        cloudflare.embed_cloudflare(["source"], settings())
    assert caught.value.code == "CLOUDFLARE_BAD_RESPONSE", reason


@pytest.mark.parametrize("status", [401, 403])
def test_access_errors_are_not_retried(monkeypatch, status):
    mock_cloudflare(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(DomainError) as caught:
        cloudflare.embed_cloudflare(["source"], settings())
    assert caught.value.code == "CLOUDFLARE_ACCESS_DENIED"
    assert not caught.value.retryable


def test_missing_credentials_fail_before_request():
    with pytest.raises(DomainError) as caught:
        cloudflare.embed_cloudflare(["source"], settings(cloudflare_api_token=""))
    assert caught.value.code == "CLOUDFLARE_UNAVAILABLE"
    assert not caught.value.retryable


def test_transport_failure_is_retryable_without_leaking_exception(monkeypatch):
    def fail(request):
        raise httpx.ConnectError("private transport detail", request=request)

    mock_cloudflare(monkeypatch, fail)
    with pytest.raises(DomainError) as caught:
        cloudflare.embed_cloudflare(["source"], settings())
    assert caught.value.code == "CLOUDFLARE_UNAVAILABLE"
    assert caught.value.retryable
    assert "private transport detail" not in caught.value.message


def test_server_error_is_retryable_and_bad_json_is_rejected(monkeypatch):
    mock_cloudflare(monkeypatch, lambda request: httpx.Response(503, text="private provider detail"))
    with pytest.raises(DomainError) as unavailable:
        cloudflare.embed_cloudflare(["source"], settings())
    assert unavailable.value.code == "CLOUDFLARE_UNAVAILABLE"
    assert unavailable.value.retryable
    assert "private provider detail" not in unavailable.value.message

    mock_cloudflare(monkeypatch, lambda request: httpx.Response(200, text="invalid json"))
    with pytest.raises(DomainError) as malformed:
        cloudflare.embed_cloudflare(["source"], settings())
    assert malformed.value.code == "CLOUDFLARE_BAD_RESPONSE"
