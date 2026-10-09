"""Project-wide Gemini admission controls shared by the API and workers.

The Redis script reserves a whole request before it is sent. A failure to reach
Redis fails closed; an in-process counter would silently oversubscribe quotas
when another API or worker replica starts.
"""

import hashlib
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import redis

from .contracts import DomainError

PACIFIC = ZoneInfo("America/Los_Angeles")
logger = logging.getLogger(__name__)

# Every member carries its request and token weights. The same atomic script
# checks project-wide minute/day windows and the smaller indexing allocation.
ADMIT_SCRIPT = """
local now = tonumber(ARGV[1])
local requests = tonumber(ARGV[2])
local tokens = tonumber(ARGV[3])
local rpm = tonumber(ARGV[4])
local tpm = tonumber(ARGV[5])
local rpd = tonumber(ARGV[6])
local index_rpm = tonumber(ARGV[7])
local index_tpm = tonumber(ARGV[8])
local index_rpd = tonumber(ARGV[9])
local member = ARGV[10]
local day_wait = tonumber(ARGV[11])
local indexing = tonumber(ARGV[12])
local counts = {}
local weights = {}
for i = 1, 4 do
  if i <= 2 then redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now - 60000) end
  local members = redis.call('ZRANGE', KEYS[i], 0, -1)
  local count = 0
  local weight = 0
  for _, value in ipairs(members) do
    local c, t = string.match(value, '|(%d+)|(%d+)$')
    count = count + tonumber(c)
    weight = weight + tonumber(t)
  end
  counts[i] = count
  weights[i] = weight
end
local function wait_minute(key)
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  if oldest[2] then return math.max(1000, tonumber(oldest[2]) + 60001 - now) end
  return 60000
end
if counts[1] + requests > rpm or weights[1] + tokens > tpm then
  return {0, wait_minute(KEYS[1]), 'minute'}
end
if indexing == 1 and (counts[2] + requests > index_rpm or weights[2] + tokens > index_tpm) then
  return {0, wait_minute(KEYS[2]), 'minute'}
end
if rpd > 0 and counts[3] + requests > rpd then return {0, day_wait, 'day'} end
if indexing == 1 and index_rpd > 0 and counts[4] + requests > index_rpd then
  return {0, day_wait, 'day'}
end
redis.call('ZADD', KEYS[1], now, member)
redis.call('EXPIRE', KEYS[1], 120)
redis.call('ZADD', KEYS[3], now, member)
redis.call('EXPIRE', KEYS[3], 172800)
if indexing == 1 then
  redis.call('ZADD', KEYS[2], now, member)
  redis.call('EXPIRE', KEYS[2], 120)
  redis.call('ZADD', KEYS[4], now, member)
  redis.call('EXPIRE', KEYS[4], 172800)
end
return {1, 0, 'ok'}
"""

SUBMISSION_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if current > tonumber(ARGV[2]) then return redis.call('TTL', KEYS[1]) end
return 0
"""

RECONCILE_SCRIPT = """
for i = 1, 4 do
  local score = redis.call('ZSCORE', KEYS[i], ARGV[1])
  if score then
    redis.call('ZREM', KEYS[i], ARGV[1])
    redis.call('ZADD', KEYS[i], score, ARGV[2])
  end
end
return 1
"""


def estimate_tokens(text: str) -> int:
    """Pessimistic text-token reservation; source is code with variable density."""
    return len(text.encode("utf-8")) + 128


def pacific_reset_seconds(at: datetime | None = None) -> int:
    current = (at or datetime.now(timezone.utc)).astimezone(PACIFIC)
    next_day = current.date() + timedelta(days=1)
    reset = datetime(next_day.year, next_day.month, next_day.day, tzinfo=PACIFIC)
    return max(1, math.ceil((reset.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()))


class QuotaGate:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=3)
        # API keys are never put into Redis names. The configured project ID lets
        # multiple deployments sharing a project coordinate when using one Redis.
        self.project = hashlib.sha256(settings.gemini_quota_project.encode()).hexdigest()[:16]

    def close(self):
        self.client.close()

    def _admit(self, model, requests, tokens, rpm, tpm, rpd, index=False, index_rpm=0, index_tpm=0, index_rpd=0):
        now = datetime.now(timezone.utc)
        pacific_day = now.astimezone(PACIFIC).date().isoformat()
        prefix = f"navigator:gemini:{self.project}:{model}"
        keys = [f"{prefix}:minute", f"{prefix}:index-minute", f"{prefix}:day:{pacific_day}", f"{prefix}:index-day:{pacific_day}"]
        if requests > (index_rpm if index else rpm) or tokens > (index_tpm if index else tpm):
            raise DomainError("QUOTA_INPUT_TOO_LARGE", "This request exceeds the configured Gemini minute budget.", 413, retryable=False)
        member = f"{uuid.uuid4().hex}|{requests}|{tokens}"
        try:
            admitted, wait_ms, reason = self.client.eval(
                ADMIT_SCRIPT, len(keys), *keys, int(now.timestamp() * 1000), requests, tokens,
                rpm, tpm, rpd, index_rpm, index_tpm, index_rpd, member,
                pacific_reset_seconds(now) * 1000, int(index),
            )
        except redis.RedisError as error:
            raise DomainError("QUOTA_GATE_UNAVAILABLE", "Gemini capacity checks are unavailable. Retry shortly.", 503) from error
        if not admitted:
            seconds = max(1, math.ceil(int(wait_ms) / 1000))
            label = "daily" if reason in {b"day", "day"} else "minute"
            raise DomainError("GEMINI_CAPACITY_WAIT", f"Gemini {label} budget is full. Saved embeddings are retained; indexing resumes automatically.", 429, retry_after_seconds=seconds)
        return keys, member, tokens

    def record_usage(self, reservation, actual_tokens):
        """Release excess pessimistic reservation only after provider usage arrives."""
        if not reservation or not isinstance(actual_tokens, int) or actual_tokens <= 0:
            return
        keys, member, estimated = reservation
        revised = math.ceil(actual_tokens * 1.25)
        if revised == estimated:
            return
        count = member.split("|")[-2]
        replacement = f"{uuid.uuid4().hex}|{count}|{revised}"
        try:
            self.client.eval(RECONCILE_SCRIPT, len(keys), *keys, member, replacement)
        except redis.RedisError:
            logger.warning("Gemini token usage reconciliation unavailable")

    def embedding(self, texts, *, query=False):
        s = self.settings
        tokens = sum(estimate_tokens(text) for text in texts)
        rpm = max(1, math.floor(s.gemini_embedding_rpm * s.gemini_quota_fraction))
        tpm = max(1, math.floor(s.gemini_embedding_tpm * s.gemini_quota_fraction))
        index_rpm = max(1, rpm - s.gemini_query_rpm_reserve)
        index_tpm = max(1, tpm - s.gemini_query_tpm_reserve)
        daily = min(s.gemini_embedding_daily_budget,
                    max(1, math.floor(s.gemini_embedding_rpd * s.gemini_quota_fraction)) if s.gemini_embedding_rpd else s.gemini_embedding_daily_budget)
        try:
            return self._admit(s.embedding_model, len(texts), tokens, rpm, tpm,
                               daily, not query, index_rpm, index_tpm,
                               max(1, daily - s.gemini_query_daily_reserve))
        except DomainError as error:
            if query and error.code == "GEMINI_CAPACITY_WAIT":
                raise DomainError(error.code, "Gemini search capacity is full. Retry this question shortly.", 429, retry_after_seconds=error.retry_after_seconds) from error
            raise

    def generation(self, text):
        s = self.settings
        try:
            return self._admit(s.gemini_model, 1, estimate_tokens(text),
                               max(1, math.floor(s.gemini_answer_rpm * s.gemini_quota_fraction)),
                               max(1, math.floor(s.gemini_answer_tpm * s.gemini_quota_fraction)),
                               min(s.gemini_answer_daily_budget,
                                   max(1, math.floor(s.gemini_answer_rpd * s.gemini_quota_fraction)) if s.gemini_answer_rpd else s.gemini_answer_daily_budget))
        except DomainError as error:
            if error.code == "GEMINI_CAPACITY_WAIT":
                raise DomainError(error.code, "Gemini answer capacity is full. Retry this question shortly.", 429, retry_after_seconds=error.retry_after_seconds) from error
            raise

    def submission(self, ip):
        s = self.settings
        key = f"navigator:submissions:{self.project}:{hashlib.sha256(ip.encode()).hexdigest()[:24]}"
        try:
            wait = self.client.eval(SUBMISSION_SCRIPT, 1, key, 86400, s.repository_submissions_per_ip_day)
        except redis.RedisError as error:
            raise DomainError("ADMISSION_UNAVAILABLE", "Repository admission is unavailable. Retry shortly.", 503) from error
        if wait:
            raise DomainError("SUBMISSION_LIMIT", "Daily repository submission allowance reached.", 429, retry_after_seconds=max(1, int(wait)))
