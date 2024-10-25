from split_settings.tools import include

include(
    "components/common.py",
    "components/i18n.py",
    "components/database.py",
    "components/redis.py",
    "components/logs.py",
    "components/debug.py",
    "components/celery.py",
)


CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
