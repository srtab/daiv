from split_settings.tools import include

include(
    "components/common.py",
    "components/i18n.py",
    "components/database.py",
    "components/celery.py",
    "components/testing.py",
)
