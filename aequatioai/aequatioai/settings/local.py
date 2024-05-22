from split_settings.tools import include

include(
    "components/common.py",
    "components/i18n.py",
    "components/database.py",
    "components/redis.py",
    "components/logging.py",
    "components/debug.py",
    "components/celery.py",
)
