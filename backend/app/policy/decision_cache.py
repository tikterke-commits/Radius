"""Authorize decision cache + fail-open/fail-closed policy (A1): p99 < 50ms
for authorize, with an explicit, configurable answer for what happens when
the real decision computation can't finish in time.

Deliberately written against a narrow Protocol instead of the concrete Redis
client, so the timeout/fallback logic is unit-testable with a fake cache and
a slow fake compute function — no real Redis needed to verify the behavior
that actually matters here (what happens on timeout).
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class FailMode(str, Enum):
    OPEN = "open"    # grant on failure/timeout — never take the network offline for a backend hiccup
    CLOSED = "closed"  # deny on failure/timeout — never silently bypass policy


@dataclass(frozen=True)
class AuthorizeDecision:
    accepted: bool
    reply_attributes: dict
    reason: str  # e.g. "ok", "gate:traffic", "cache:stale", "fail_mode:open", "fail_mode:closed"


class DecisionCache(Protocol):
    async def get(self, username: str) -> AuthorizeDecision | None: ...
    async def set(self, username: str, decision: AuthorizeDecision, ttl_s: int) -> None: ...


async def authorize_with_budget(
    username: str,
    compute: Callable[[], Awaitable[AuthorizeDecision]],
    cache: DecisionCache,
    timeout_s: float,
    fail_mode: FailMode,
    cache_ttl_s: int,
) -> AuthorizeDecision:
    """Try to compute a fresh decision within `timeout_s`. On success, cache
    and return it. On timeout or exception: fall back to the last cached
    decision for this subscriber if one exists (a stale-but-recent real
    decision beats a blind fail-open/closed guess); if there's no cached
    decision at all, apply `fail_mode`.
    """
    try:
        decision = await asyncio.wait_for(compute(), timeout=timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("authorize timeout for %s, falling back", username)
        return await _fallback(username, cache, fail_mode)
    except Exception:
        logger.exception("authorize compute failed for %s, falling back", username)
        return await _fallback(username, cache, fail_mode)

    await cache.set(username, decision, cache_ttl_s)
    return decision


async def _fallback(username: str, cache: DecisionCache, fail_mode: FailMode) -> AuthorizeDecision:
    cached = await cache.get(username)
    if cached is not None:
        return AuthorizeDecision(
            accepted=cached.accepted,
            reply_attributes=cached.reply_attributes,
            reason=f"cache:stale:{cached.reason}",
        )

    if fail_mode == FailMode.OPEN:
        return AuthorizeDecision(accepted=True, reply_attributes={}, reason="fail_mode:open")
    return AuthorizeDecision(accepted=False, reply_attributes={}, reason="fail_mode:closed")


class RedisDecisionCache:
    """Concrete DecisionCache backed by app.redis_client. Kept out of the
    Protocol-satisfying core above so the timeout/fallback unit tests never
    need a real Redis connection.
    """

    def __init__(self, redis_client, key_prefix: str = "authz_cache"):
        self._redis = redis_client
        self._key_prefix = key_prefix

    def _key(self, username: str) -> str:
        return f"{self._key_prefix}:{username}"

    async def get(self, username: str) -> AuthorizeDecision | None:
        raw = await self._redis.get(self._key(username))
        if raw is None:
            return None
        return AuthorizeDecision(**json.loads(raw))

    async def set(self, username: str, decision: AuthorizeDecision, ttl_s: int) -> None:
        await self._redis.set(self._key(username), json.dumps(asdict(decision)), ex=ttl_s)
