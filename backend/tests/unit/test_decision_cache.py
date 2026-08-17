import asyncio

import pytest

from app.policy.decision_cache import AuthorizeDecision, FailMode, authorize_with_budget


class FakeCache:
    def __init__(self):
        self._store: dict[str, AuthorizeDecision] = {}

    async def get(self, username):
        return self._store.get(username)

    async def set(self, username, decision, ttl_s):
        self._store[username] = decision


FRESH = AuthorizeDecision(accepted=True, reply_attributes={"Mikrotik-Rate-Limit": "10M/10M"}, reason="ok")


async def fast_compute():
    return FRESH


async def slow_compute():
    await asyncio.sleep(1)
    return FRESH


async def raising_compute():
    raise RuntimeError("backend db unreachable")


@pytest.mark.asyncio
async def test_fast_path_returns_computed_decision_and_caches_it():
    cache = FakeCache()
    result = await authorize_with_budget(
        "alice", fast_compute, cache, timeout_s=0.05, fail_mode=FailMode.CLOSED, cache_ttl_s=30
    )
    assert result == FRESH
    assert await cache.get("alice") == FRESH


@pytest.mark.asyncio
async def test_timeout_with_no_cached_decision_and_fail_open_grants():
    cache = FakeCache()
    result = await authorize_with_budget(
        "bob", slow_compute, cache, timeout_s=0.01, fail_mode=FailMode.OPEN, cache_ttl_s=30
    )
    assert result.accepted is True
    assert result.reason == "fail_mode:open"


@pytest.mark.asyncio
async def test_timeout_with_no_cached_decision_and_fail_closed_denies():
    cache = FakeCache()
    result = await authorize_with_budget(
        "carol", slow_compute, cache, timeout_s=0.01, fail_mode=FailMode.CLOSED, cache_ttl_s=30
    )
    assert result.accepted is False
    assert result.reason == "fail_mode:closed"


@pytest.mark.asyncio
async def test_timeout_with_cached_decision_prefers_stale_cache_over_fail_mode():
    # a stale-but-real decision beats a blind fail-open/closed guess, regardless
    # of which fail_mode is configured
    cache = FakeCache()
    await cache.set("dave", FRESH, 30)
    result = await authorize_with_budget(
        "dave", slow_compute, cache, timeout_s=0.01, fail_mode=FailMode.CLOSED, cache_ttl_s=30
    )
    assert result.accepted is True
    assert result.reason == "cache:stale:ok"


@pytest.mark.asyncio
async def test_exception_in_compute_falls_back_same_as_timeout():
    cache = FakeCache()
    result = await authorize_with_budget(
        "erin", raising_compute, cache, timeout_s=1.0, fail_mode=FailMode.OPEN, cache_ttl_s=30
    )
    assert result.accepted is True
    assert result.reason == "fail_mode:open"


@pytest.mark.asyncio
async def test_budget_is_actually_enforced_not_just_advisory():
    cache = FakeCache()
    loop = asyncio.get_event_loop()
    start = loop.time()
    await authorize_with_budget(
        "frank", slow_compute, cache, timeout_s=0.01, fail_mode=FailMode.OPEN, cache_ttl_s=30
    )
    elapsed = loop.time() - start
    # must return near the timeout budget, not wait for slow_compute's full 1s sleep
    assert elapsed < 0.5
