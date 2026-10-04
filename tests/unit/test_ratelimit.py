import fakeredis
import pytest

from app.core.errors import RateLimited, ServiceUnavailable
from app.security.ratelimit import RateLimiter


class Clock:
    t = 1_000_000.0
    def __call__(self): return self.t


def test_sliding_window_blocks_then_recovers():
    clk = Clock(); rl = RateLimiter(fakeredis.FakeRedis(), clock=clk)
    for _ in range(3):
        assert rl.hit("k", "a", limit=3).allowed
    r = rl.hit("k", "a", limit=3)
    assert not r.allowed and 1 <= r.retry_after_s <= 60
    clk.t += 61
    assert rl.hit("k", "a", limit=3).allowed


def test_identifiers_are_isolated():
    rl = RateLimiter(fakeredis.FakeRedis(), clock=Clock())
    for _ in range(2): rl.hit("k", "a", limit=2)
    assert not rl.hit("k", "a", limit=2).allowed
    assert rl.hit("k", "b", limit=2).allowed


def test_enforce_raises_429_with_retry_after():
    rl = RateLimiter(fakeredis.FakeRedis(), clock=Clock())
    rl.enforce("k", "a", limit=1)
    with pytest.raises(RateLimited) as e:
        rl.enforce("k", "a", limit=1)
    assert e.value.status_code == 429 and int(e.value.headers["Retry-After"]) >= 1


def test_shared_state_across_instances():  # two "API replicas" on one Redis
    r = fakeredis.FakeRedis(); clk = Clock()
    a, b = RateLimiter(r, clock=clk), RateLimiter(r, clock=clk)
    a.hit("k", "x", limit=2); b.hit("k", "x", limit=2)
    assert not a.hit("k", "x", limit=2).allowed


def test_redis_down_fail_closed_vs_open():
    class Boom:
        def register_script(self, s):
            def call(**kw): raise ConnectionError()
            return call
    with pytest.raises(ServiceUnavailable):
        RateLimiter(Boom()).hit("k", "a", limit=1)
    assert RateLimiter(Boom(), fail_open=True).hit("k", "a", limit=1).allowed
