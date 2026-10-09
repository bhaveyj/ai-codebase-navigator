from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from navigator.config import Settings
from navigator.contracts import DomainError
from navigator.quota import QuotaGate, estimate_tokens, pacific_reset_seconds


def test_conservative_embedding_token_estimate_and_dst_reset():
    assert estimate_tokens("é") == 130
    from datetime import datetime, timezone
    # Pacific daylight-saving transition is 23 hours from one midnight to the next.
    at = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
    assert pacific_reset_seconds(at) == 23 * 3600


def test_gate_uses_source_input_count_and_prefix_tokens():
    calls = []
    client = SimpleNamespace(eval=lambda *args: (calls.append(args) or [1, 0, b"ok"]), close=lambda: None)
    settings = Settings(_env_file=None, GEMINI_EMBEDDING_RPM=100, GEMINI_EMBEDDING_TPM=30000)
    gate = QuotaGate(settings, client)
    gate.embedding(["title: Repository source | text: abc", "title: Repository source | text: xyz"])
    argv = calls[0]
    assert argv[7] == 2  # Redis script arguments follow the four keys and timestamp.
    assert argv[8] == sum(estimate_tokens(s) for s in ["title: Repository source | text: abc", "title: Repository source | text: xyz"])
    assert argv[9:11] == (70, 21000)
    assert argv[12:14] == (60, 18000)


def test_gate_returns_retry_without_sending_provider_request():
    client = SimpleNamespace(eval=lambda *_: [0, 61000, b"minute"], close=lambda: None)
    gate = QuotaGate(Settings(_env_file=None), client)
    with pytest.raises(DomainError) as caught:
        gate.embedding(["abc"])
    assert caught.value.code == "GEMINI_CAPACITY_WAIT"
    assert caught.value.retry_after_seconds == 61


def test_single_request_larger_than_safe_token_budget_is_rejected_before_redis():
    client = SimpleNamespace(eval=lambda *_: pytest.fail("Oversized input reached Redis"), close=lambda: None)
    settings = Settings(_env_file=None, GEMINI_EMBEDDING_TPM=1000, GEMINI_QUERY_TPM_RESERVE=0)
    with pytest.raises(DomainError) as caught:
        QuotaGate(settings, client).embedding(["x" * 700])
    assert caught.value.code == "QUOTA_INPUT_TOO_LARGE"


def test_redis_gate_is_atomic_across_workers():
    import os
    import redis
    url = os.getenv("REDIS_TEST_URL", "redis://localhost:6379/15")
    client = redis.Redis.from_url(url, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis integration service unavailable")
    settings = Settings(_env_file=None, REDIS_URL=url, GEMINI_QUOTA_PROJECT=f"test-{uuid4().hex}",
                        GEMINI_EMBEDDING_RPM=10, GEMINI_EMBEDDING_TPM=100000,
                        GEMINI_QUERY_RPM_RESERVE=0, GEMINI_QUERY_TPM_RESERVE=0,
                        GEMINI_EMBEDDING_DAILY_BUDGET=50)
    gates = [QuotaGate(settings), QuotaGate(settings)]
    def attempt(index):
        try:
            gates[index % 2].embedding(["x"])
            return True
        except DomainError as error:
            assert error.code == "GEMINI_CAPACITY_WAIT"
            return False
    with ThreadPoolExecutor(max_workers=12) as executor:
        assert sum(executor.map(attempt, range(24))) == 7
    for gate in gates:
        gate.close()


def test_redis_gate_enforces_tpm_and_daily_budget():
    import os
    import redis
    url = os.getenv("REDIS_TEST_URL", "redis://localhost:6379/15")
    client = redis.Redis.from_url(url, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis integration service unavailable")
    settings = Settings(_env_file=None, REDIS_URL=url, GEMINI_QUOTA_PROJECT=f"test-{uuid4().hex}",
                        GEMINI_EMBEDDING_RPM=20, GEMINI_EMBEDDING_TPM=2000,
                        GEMINI_QUERY_RPM_RESERVE=0, GEMINI_QUERY_TPM_RESERVE=0,
                        GEMINI_EMBEDDING_DAILY_BUDGET=3, GEMINI_QUERY_DAILY_RESERVE=0)
    gate = QuotaGate(settings)
    reservation = gate.embedding(["x" * 900])  # 1028 reserved tokens; a second exceeds 1400 TPM.
    with pytest.raises(DomainError) as minute:
        gate.embedding(["x" * 900])
    assert "minute" in minute.value.message
    gate.record_usage(reservation, 100)  # Provider's actual count releases excess reservation.
    gate.embedding(["y"])
    gate.embedding(["z"])
    with pytest.raises(DomainError) as daily:
        gate.embedding(["last"])
    assert "daily" in daily.value.message
    gate.close()


def test_indexing_cannot_consume_question_reserve_or_per_ip_allowance():
    import os
    import redis
    url = os.getenv("REDIS_TEST_URL", "redis://localhost:6379/15")
    client = redis.Redis.from_url(url, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis integration service unavailable")
    settings = Settings(_env_file=None, REDIS_URL=url, GEMINI_QUOTA_PROJECT=f"test-{uuid4().hex}",
                        GEMINI_EMBEDDING_RPM=10, GEMINI_EMBEDDING_TPM=100000,
                        GEMINI_QUERY_RPM_RESERVE=3, GEMINI_QUERY_TPM_RESERVE=0,
                        GEMINI_EMBEDDING_DAILY_BUDGET=50, REPOSITORY_SUBMISSIONS_PER_IP_DAY=2)
    gate = QuotaGate(settings)
    for _ in range(4):
        gate.embedding(["document"])
    with pytest.raises(DomainError):
        gate.embedding(["document"])
    for _ in range(3):
        gate.embedding(["query"], query=True)
    with pytest.raises(DomainError):
        gate.embedding(["query"], query=True)
    gate.submission("203.0.113.7")
    gate.submission("203.0.113.7")
    with pytest.raises(DomainError) as limit:
        gate.submission("203.0.113.7")
    assert limit.value.code == "SUBMISSION_LIMIT"
    gate.submission("203.0.113.8")
    gate.close()
