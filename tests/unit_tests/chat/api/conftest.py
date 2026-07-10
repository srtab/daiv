"""Shared fixtures for chat API tests.

``FakeAsyncRedis`` implements the tiny slice of the redis.asyncio API the chat
relay uses (streams + string get/set). Hand-rolled instead of fakeredis so the
test dependency surface stays zero and blocking semantics stay deterministic:
``xread`` never blocks — an empty result stands in for a block timeout — and it
always yields control once so concurrently-spawned writer tasks can progress.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


class FakeAsyncRedis:
    def __init__(self):
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self._seq = 0

    async def xadd(self, key, fields, maxlen=None, approximate=False):
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.streams.setdefault(key, []).append((entry_id, {k: str(v) for k, v in fields.items()}))
        return entry_id

    async def xread(self, streams, count=None, block=None):
        # Yield control so a concurrently running writer task can make progress
        # before we report "nothing new" (the stand-in for a block timeout).
        await asyncio.sleep(0)
        out = []
        for key, last_id in streams.items():
            entries = [e for e in self.streams.get(key, []) if self._after(e[0], last_id)]
            if count:
                entries = entries[:count]
            if entries:
                out.append((key, entries))
        return out

    @staticmethod
    def _after(entry_id: str, last_id: str) -> bool:
        def parse(i: str) -> tuple[int, int]:
            a, _, b = i.partition("-")
            return (int(a), int(b or 0))

        return parse(entry_id) > parse(last_id)

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def set(self, key, value, ex=None):
        self.kv[key] = str(value)
        return True

    async def get(self, key):
        return self.kv.get(key)

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    """Minimal async-pipeline stand-in: buffers commands and replays them against
    the parent fake on ``execute`` (matches how ``relay._append`` batches XADD +
    EXPIRE into one round-trip). Command methods return ``self`` for chaining and
    are not awaited, mirroring redis.asyncio's buffered pipeline."""

    def __init__(self, redis: FakeAsyncRedis):
        self._redis = redis
        self._ops: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def xadd(self, *args, **kwargs):
        self._ops.append(("xadd", args, kwargs))
        return self

    def expire(self, *args, **kwargs):
        self._ops.append(("expire", args, kwargs))
        return self

    async def execute(self):
        return [await getattr(self._redis, name)(*args, **kwargs) for name, args, kwargs in self._ops]


@pytest.fixture
def fake_redis():
    """Route every relay call through an in-memory fake."""
    fake = FakeAsyncRedis()
    with patch("chat.api.relay.get_redis", return_value=fake):
        yield fake


@pytest.fixture
def captured_runs():
    """Patch ``runner.spawn_run`` so view tests can deterministically await the
    spawned run: tasks are real (created on the test loop) and collected here —
    ``await asyncio.gather(*captured_runs)`` drives them to completion before
    DB/relay assertions.
    """
    from chat.api import runner

    tasks: list[asyncio.Task] = []

    def _spawn(streamer):
        task = asyncio.get_running_loop().create_task(runner.run_to_relay(streamer))
        tasks.append(task)
        return task

    with patch("chat.api.runner.spawn_run", side_effect=_spawn):
        yield tasks
