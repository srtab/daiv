import pytest
from notifications.channels.base import NotificationChannel
from notifications.channels.registry import _registry, all_channels, get_channel, register_channel
from notifications.exceptions import UnknownChannelError


class _DummyChannel(NotificationChannel):
    channel_type = "dummy"
    display_name = "Dummy"

    def resolve_address(self, user):
        return "dummy"

    def send(self, notification, delivery):
        pass


@pytest.fixture(autouse=True)
def _preserve_registry():
    snapshot = dict(_registry)
    yield
    _registry.clear()
    _registry.update(snapshot)


class TestRegistry:
    def test_register_and_get(self):
        register_channel(_DummyChannel)
        channel = get_channel("dummy")
        assert isinstance(channel, _DummyChannel)

    def test_duplicate_registration_raises(self):
        register_channel(_DummyChannel)
        with pytest.raises(ValueError, match="already registered"):
            register_channel(_DummyChannel)

    def test_get_unknown_raises(self):
        with pytest.raises(UnknownChannelError) as exc_info:
            get_channel("does-not-exist")
        assert exc_info.value.channel_type == "does-not-exist"

    def test_all_channels_lists_registered(self):
        register_channel(_DummyChannel)
        assert _DummyChannel in all_channels()
