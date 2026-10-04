"""Distributed sliding-window-log rate limiter on Redis (atomic via Lua).

State lives in Redis so every API replica shares one view. Clock: the caller's wall clock in ms;
skew between replicas is bounded by NTP and only shifts window edges slightly.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from app.core.errors import RateLimited, ServiceUnavailable
from app.observability.metrics import RATE_LIMITED

_LUA = """
local key, now, window, limit, member = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, window)
  return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry = tonumber(oldest[2]) + window - now
if retry < 1 then retry = 1 end
return {0, 0, retry}
"""


class RedisLike(Protocol):
    def register_script(self, script: str): ...


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int


class RateLimiter:
    def __init__(self, redis: RedisLike, *, fail_open: bool = False, clock: Callable[[], float] = time.time,
                 prefix: str = "rl"):
        self._script = redis.register_script(_LUA)
        self._fail_open = fail_open
        self._clock = clock
        self._prefix = prefix

    def hit(self, scope: str, identifier: str, *, limit: int, window_s: int = 60) -> LimitResult:
        key = f"{self._prefix}:{scope}:{identifier}"
        now_ms = int(self._clock() * 1000)
        try:
            allowed, remaining, retry_ms = self._script(
                keys=[key], args=[now_ms, window_s * 1000, limit, f"{now_ms}-{uuid.uuid4().hex[:8]}"])
        except Exception as exc:  # redis down / timeout
            if self._fail_open:
                return LimitResult(True, limit, limit, 0)
            raise ServiceUnavailable("Rate limiter unavailable", code="rate_limiter_unavailable") from exc
        return LimitResult(bool(allowed), limit, int(remaining), -(-int(retry_ms) // 1000))

    def enforce(self, scope: str, identifier: str, *, limit: int, window_s: int = 60,
                message: str | None = None) -> LimitResult:
        res = self.hit(scope, identifier, limit=limit, window_s=window_s)
        if not res.allowed:
            RATE_LIMITED.labels(scope=scope).inc()
            raise RateLimited(message or f"Rate limit exceeded ({scope})", retry_after=res.retry_after_s,
                              code=f"rate_limited_{scope}",
                              headers={"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"})
        return res
