"""Fixed-window rate limiting in Redis. Keys hold counters only (never content)."""

from __future__ import annotations

import time

import redis.asyncio as aioredis

from khugjil.crypto import sha256_hex


class RateLimiter:
    def __init__(self, redis: aioredis.Redis, prefix: str = "khj:rl") -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, key: str, window_s: int) -> str:
        bucket = int(time.time()) // window_s
        # Hash the caller key so identifiers (IPs, logins) are not stored in clear text.
        return f"{self._prefix}:{sha256_hex(key)[:32]}:{window_s}:{bucket}"

    async def allow(self, key: str, limit: int, window_s: int) -> bool:
        k = self._key(key, window_s)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(k)
            pipe.expire(k, window_s + 1)
            count, _ = await pipe.execute()
        return int(count) <= limit

    async def count(self, key: str, window_s: int) -> int:
        value = await self._redis.get(self._key(key, window_s))
        return int(value or 0)

    async def reset(self, key: str, window_s: int) -> None:
        await self._redis.delete(self._key(key, window_s))
