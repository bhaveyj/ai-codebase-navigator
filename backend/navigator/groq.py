"""Groq chat completion adapter for grounded repository answers."""

import json
import math

import httpx

from .contracts import DomainError


GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
STRICT_OUTPUT_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}


def _answer_format(model):
    if model not in STRICT_OUTPUT_MODELS:
        return {"type": "json_object"}
    block = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": ["fact", "inference", "unknown"]},
            "citationIds": {"type": "array", "items": {"type": "string"}},
            "edgeIds": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text", "kind", "citationIds", "edgeIds"],
        "additionalProperties": False,
    }
    return {"type": "json_schema", "json_schema": {
        "name": "grounded_repository_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"blocks": {"type": "array", "items": block}},
            "required": ["blocks"],
            "additionalProperties": False,
        },
    }}


def _retry_after_seconds(headers):
    """Groq documents retry-after as seconds on a 429 response."""
    try:
        seconds = float(headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return 60
    if not math.isfinite(seconds):
        return 60
    return min(86400, max(1, seconds))


def _status_error(response):
    # Do not expose the response body: it can include account or request details.
    status = response.status_code
    if status == 429:
        return DomainError(
            "GROQ_RATE_LIMITED",
            "Groq's answer rate limit was reached. Try this question after the cooldown.",
            429,
            retry_after_seconds=_retry_after_seconds(response.headers),
        )
    if status in {401, 403}:
        return DomainError(
            "GROQ_ACCESS_DENIED",
            "Groq rejected the server API key or model access. Check GROQ_API_KEY and the selected model.",
            503,
            retryable=False,
        )
    if status == 413:
        try:
            error = response.json().get("error", {})
        except (ValueError, AttributeError):
            error = {}
        if isinstance(error, dict) and error.get("type") == "tokens" and error.get("code") == "rate_limit_exceeded":
            return DomainError(
                "GROQ_TOKEN_BUDGET",
                "This answer request exceeds Groq's token-per-minute allowance. Reduce the evidence or completion-token limits and retry.",
                413,
                retryable=False,
            )
    if status in {400, 404, 413, 422}:
        return DomainError(
            "GROQ_REQUEST_INVALID",
            "Groq rejected the answer request. Check the selected model and reduce the evidence size if needed.",
            503,
            retryable=False,
        )
    return DomainError(
        "GROQ_UNAVAILABLE",
        "Groq could not generate an answer. Check service availability and connectivity.",
        503,
    )


def generate_groq(system: str, payload: dict, settings) -> dict:
    """Generate a JSON answer; the caller validates citations against its registry."""
    if not settings.groq_api_key:
        raise DomainError(
            "GROQ_UNAVAILABLE",
            "Configure GROQ_API_KEY to enable Groq repository answers.",
            503,
            retryable=False,
        )

    request = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        ],
        "temperature": 0.1,
        "max_completion_tokens": settings.groq_max_completion_tokens,
        "response_format": _answer_format(settings.groq_model),
    }
    try:
        response = httpx.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json=request,
            timeout=settings.groq_timeout_seconds,
        )
    except httpx.RequestError as error:
        raise DomainError(
            "GROQ_UNAVAILABLE",
            "Groq could not generate an answer. Check service availability and connectivity.",
            503,
        ) from error

    if response.status_code != 200:
        raise _status_error(response)

    try:
        completion = response.json()
        choice = completion["choices"][0]
        if choice.get("finish_reason") == "length":
            raise DomainError(
                "GROQ_ANSWER_TRUNCATED",
                "Groq stopped before finishing the answer. Ask a narrower question or increase GROQ_MAX_COMPLETION_TOKENS within your rate limit.",
                502,
                retryable=False,
            )
        content = choice["message"]["content"]
        answer = json.loads(content)
        if not isinstance(answer, dict):
            raise ValueError("Groq answer must be a JSON object")
        return answer
    except (AttributeError, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Groq did not return a valid JSON answer") from error
