from unittest.mock import AsyncMock, Mock, patch

from core.sandbox.session_store import SANDBOX_SESSION_TTL_SECONDS, SandboxSessionStore


def _patch_cache(fake_cache: Mock):
    return patch("core.sandbox.session_store.cache", fake_cache)


class TestAget:
    async def test_returns_session_id_when_mapping_present(self):
        fake_cache = Mock(aget=AsyncMock(return_value={"session_id": "s1"}))
        with _patch_cache(fake_cache):
            result = await SandboxSessionStore().aget("t1")
        assert result == "s1"
        fake_cache.aget.assert_awaited_once_with("sandbox_session:t1")

    async def test_returns_none_when_no_mapping(self):
        fake_cache = Mock(aget=AsyncMock(return_value=None))
        with _patch_cache(fake_cache):
            assert await SandboxSessionStore().aget("t1") is None

    async def test_returns_none_when_cached_value_missing_session_id(self):
        fake_cache = Mock(aget=AsyncMock(return_value={"other": "x"}))
        with _patch_cache(fake_cache):
            assert await SandboxSessionStore().aget("t1") is None

    async def test_returns_none_when_cached_value_not_a_dict(self):
        fake_cache = Mock(aget=AsyncMock(return_value="garbage"))
        with _patch_cache(fake_cache):
            assert await SandboxSessionStore().aget("t1") is None

    async def test_returns_none_on_cache_read_failure(self):
        """A cache outage degrades to "no warm session" (cold create) rather than crashing the run."""
        fake_cache = Mock(aget=AsyncMock(side_effect=RuntimeError("redis down")))
        with _patch_cache(fake_cache):
            assert await SandboxSessionStore().aget("t1") is None


class TestRemember:
    async def test_writes_mapping_with_default_ttl(self):
        fake_cache = Mock(aset=AsyncMock(return_value=None))
        with _patch_cache(fake_cache):
            await SandboxSessionStore().remember("t1", "s9")
        fake_cache.aset.assert_awaited_once_with(
            "sandbox_session:t1", {"session_id": "s9"}, timeout=SANDBOX_SESSION_TTL_SECONDS
        )

    async def test_respects_custom_ttl(self):
        fake_cache = Mock(aset=AsyncMock(return_value=None))
        with _patch_cache(fake_cache):
            await SandboxSessionStore(ttl=5).remember("t1", "s9")
        fake_cache.aset.assert_awaited_once_with("sandbox_session:t1", {"session_id": "s9"}, timeout=5)

    async def test_survives_cache_write_failure(self):
        """A write failure must not propagate into the run's teardown logic."""
        fake_cache = Mock(aset=AsyncMock(side_effect=RuntimeError("redis down")))
        with _patch_cache(fake_cache):
            await SandboxSessionStore().remember("t1", "s9")  # must not raise


class TestForget:
    async def test_deletes_mapping(self):
        fake_cache = Mock(adelete=AsyncMock(return_value=None))
        with _patch_cache(fake_cache):
            await SandboxSessionStore().forget("t1")
        fake_cache.adelete.assert_awaited_once_with("sandbox_session:t1")

    async def test_survives_cache_delete_failure(self):
        fake_cache = Mock(adelete=AsyncMock(side_effect=RuntimeError("redis down")))
        with _patch_cache(fake_cache):
            await SandboxSessionStore().forget("t1")  # must not raise
