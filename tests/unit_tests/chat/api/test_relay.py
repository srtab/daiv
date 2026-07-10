"""Tests for the chat run event relay (Redis Streams primitives).

The key format is a security contract: the thread id is embedded in the stream
key so the reader endpoint's visibility check on the thread is sufficient
authorization — guessing a run id from another thread yields a key that does
not exist. Assert it exactly.
"""

import pytest

from chat.api import relay


def test_run_events_key_embeds_thread_and_run_id():
    assert relay.run_events_key("t-1", "r-1") == "daiv:chat:run-events:t-1:r-1"


def test_cancel_key_embeds_thread_and_run_id():
    assert relay.cancel_key("t-1", "r-1") == "daiv:chat:run-cancel:t-1:r-1"


async def test_publish_event_appends_data_entry_and_refreshes_ttl(fake_redis):
    await relay.publish_event("t-1", "r-1", '{"type":"RUN_STARTED"}')
    await relay.publish_event("t-1", "r-1", '{"type":"RUN_FINISHED"}')

    key = relay.run_events_key("t-1", "r-1")
    entries = fake_redis.streams[key]
    assert [fields for _id, fields in entries] == [
        {"data": '{"type":"RUN_STARTED"}'},
        {"data": '{"type":"RUN_FINISHED"}'},
    ]
    assert fake_redis.ttls[key] == relay.RUN_EVENTS_TTL_S


async def test_publish_end_appends_sentinel_entry(fake_redis):
    await relay.publish_end("t-1", "r-1")

    entries = fake_redis.streams[relay.run_events_key("t-1", "r-1")]
    assert entries[-1][1] == {"end": "1"}


async def test_cancel_roundtrip(fake_redis):
    assert await relay.cancel_requested("t-1", "r-1") is False
    await relay.request_cancel("t-1", "r-1")
    assert await relay.cancel_requested("t-1", "r-1") is True
    # scoped per (thread, run)
    assert await relay.cancel_requested("t-1", "r-other") is False


def test_build_client_raises_without_configured_url(settings):
    # ``_build_client`` (not ``get_redis``) so the module-level singleton stays untouched.
    settings.DJANGO_REDIS_URL = None
    with pytest.raises(RuntimeError, match="DJANGO_REDIS_URL"):
        relay._build_client()
