from plugins.conf import settings


def test_defaults():
    assert settings.UPDATE_CRON == "0 * * * *"
    assert settings.DATA_PATH.name == "plugins"
