from split_settings.tools import include

include(
    "components/common.py",
    "components/i18n.py",
    "components/database.py",
    "components/redis.py",
    "components/logs.py",
    "components/celery.py",
    "components/sentry.py",
)
