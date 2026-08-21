"""The shared Redis clients behind the chat relay and the UI event bus."""

from __future__ import annotations

import pytest

from core.redis import SOCKET_TIMEOUT_S, RedisConnections


class TestRedisConnections:
    def test_no_setting_at_all_is_a_bus_that_is_simply_absent(self, settings):
        """The test settings leave the Redis component out of the include list, which is
        a valid deployment shape — not a broken one."""
        del settings.DJANGO_REDIS_URL
        assert RedisConnections().configured is False

    def test_asking_for_a_client_without_a_url_gets_a_clear_error(self, settings):
        """Where the bus's publishers stay quiet, this is the side an operator sees when
        Redis is missing — it reaches the nav SSE handler's error frame."""
        del settings.DJANGO_REDIS_URL
        with pytest.raises(RuntimeError, match="requires Redis"):
            RedisConnections().async_client()

    def test_clients_are_built_once_and_kept(self, settings):
        settings.DJANGO_REDIS_URL = "redis://localhost:6379/0"
        connections = RedisConnections()
        assert connections.sync_client() is connections.sync_client()
        assert connections.async_client() is connections.async_client()
        assert connections.sync_client() is not connections.async_client()

    def test_the_sync_client_cannot_block_a_writer_forever(self, settings):
        """Publishing runs inline on request and worker threads, so a wedged server has to
        surface as an error the fire-and-forget ``except`` can swallow."""
        settings.DJANGO_REDIS_URL = "redis://localhost:6379/0"
        options = RedisConnections().sync_client().connection_pool.connection_kwargs
        assert options["socket_connect_timeout"] == SOCKET_TIMEOUT_S
        assert options["socket_timeout"] == SOCKET_TIMEOUT_S

    def test_the_async_client_keeps_no_read_deadline(self, settings):
        """Its readers block on purpose — ``PubSub.get_message(timeout=20)`` and the chat
        relay's ``xread(block=15000)`` both outlast any socket read deadline worth setting."""
        settings.DJANGO_REDIS_URL = "redis://localhost:6379/0"
        options = RedisConnections().async_client().connection_pool.connection_kwargs
        assert options.get("socket_timeout") is None
