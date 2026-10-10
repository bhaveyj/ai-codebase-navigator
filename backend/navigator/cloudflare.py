"""Cloudflare Workers AI embeddings for repository source and search queries."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from .contracts import DomainError


CODE_RETRIEVAL_INSTRUCTION = "Given a question about a codebase, retrieve source code that answers the question."


def _retry_after_seconds(header: str | None) -> float:
    """Honor Cloudflare's cooldown without trusting provider response bodies."""
    if header:
        if re.fullmatch(r"\d+(?:\.\d+)?", header.strip()):
            return min(86400, max(1, float(header.strip())))
        try:
            retry_at = parsedate_to_datetime(header)
            if retry_at.tzinfo is not None:
                return min(86400, max(1, (retry_at - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    return 60


def _utc_reset_seconds() -> float:
    now = datetime.now(timezone.utc)
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(1, (reset - now).total_seconds())


def _daily_allocation_exhausted(response: httpx.Response) -> bool:
    # Cloudflare documents internal error 3036 for the daily free allocation.
    # Inspect only the known numeric code; never show provider error text.
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
        return False
    return any(isinstance(item, dict) and str(item.get("code")) == "3036" for item in payload["errors"])


def _vectors_from_response(payload: object, count: int, dimensions: int) -> list[list[float]]:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned an invalid embedding response.", 502)
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned an invalid embedding response.", 502)
    data = result.get("data")
    if not isinstance(data, list) or len(data) != count:
        raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned an unexpected number of embeddings.", 502)
    shape = result.get("shape")
    if shape is not None and shape != [count, dimensions]:
        raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned unexpected embedding dimensions.", 502)

    vectors: list[list[float]] = []
    for values in data:
        if not isinstance(values, list) or len(values) != dimensions:
            raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned unexpected embedding dimensions.", 502)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned invalid embedding values.", 502)
        norm = math.hypot(*values)
        if not math.isfinite(norm) or norm == 0:
            raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned an invalid embedding vector.", 502)
        vectors.append([float(value / norm) for value in values])
    return vectors


def embed_cloudflare(texts: list[str], settings, query: bool = False) -> list[list[float]]:
    """Embed a batch of documents or queries with the same Cloudflare Qwen model.

    Qwen recommends task instructions for queries and plain text for documents.
    Cloudflare's REST endpoint accepts an array of texts and returns one vector
    for each input, preserving order.
    """
    if not texts:
        return []
    if any(not isinstance(item, str) or not item.strip() for item in texts):
        raise ValueError("Embedding inputs must be nonempty strings")
    if not settings.cloudflare_api_token or not settings.cloudflare_account_id:
        raise DomainError(
            "CLOUDFLARE_UNAVAILABLE",
            "Configure CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to enable Cloudflare embeddings.",
            503,
            retryable=False,
        )

    inputs = (
        [f"Instruct: {CODE_RETRIEVAL_INSTRUCTION}\nQuery: {item}" for item in texts]
        if query else texts
    )
    account_id = quote(settings.cloudflare_account_id, safe="")
    model = quote(settings.cloudflare_embedding_model, safe="@/")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    try:
        with httpx.Client(timeout=settings.cloudflare_timeout_seconds, follow_redirects=False) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {settings.cloudflare_api_token}", "Content-Type": "application/json"},
                json={"text": inputs},
            )
        if response.status_code == 429:
            if _daily_allocation_exhausted(response):
                raise DomainError(
                    "CLOUDFLARE_QUOTA_EXHAUSTED",
                    "Cloudflare's daily free embedding allocation is exhausted. Saved embeddings are retained; indexing will resume after the UTC reset.",
                    429,
                    retry_after_seconds=max(_utc_reset_seconds(), _retry_after_seconds(response.headers.get("Retry-After"))),
                )
            raise DomainError(
                "CLOUDFLARE_RATE_LIMITED",
                "Cloudflare embedding capacity is temporarily unavailable. Saved embeddings are retained; indexing will resume after a cooldown.",
                429,
                retry_after_seconds=_retry_after_seconds(response.headers.get("Retry-After")),
            )
        if response.status_code in (401, 403):
            raise DomainError(
                "CLOUDFLARE_ACCESS_DENIED",
                "Cloudflare rejected the Workers AI credentials or model access.",
                503,
                retryable=False,
            )
        if response.status_code >= 400:
            raise DomainError(
                "CLOUDFLARE_UNAVAILABLE",
                "Cloudflare could not complete the embedding request. Check model access and service availability.",
                503,
                retryable=response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise DomainError("CLOUDFLARE_BAD_RESPONSE", "Cloudflare returned an invalid embedding response.", 502) from error
        return _vectors_from_response(payload, len(texts), settings.effective_embedding_dimensions)
    except httpx.HTTPError as error:
        raise DomainError(
            "CLOUDFLARE_UNAVAILABLE",
            "Cloudflare embeddings are unreachable. Saved embeddings are retained; retry when the service is available.",
            503,
        ) from error
