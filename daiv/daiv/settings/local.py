import warnings

from split_settings.tools import include

warnings.filterwarnings(
    "ignore", message=r'directory "/run/secrets" does not exist', module="pydantic_settings.sources.providers.secrets"
)

include(
    "components/common.py",
    "components/i18n.py",
    "components/database.py",
    "components/redis.py",
    "components/logs.py",
    "components/debug.py",
    "components/celery.py",
)
