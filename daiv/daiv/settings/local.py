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
    "components/tasks.py",
    "components/allauth.py",
    "components/oauth2.py",
)

# Serve static files directly from app directories without collectstatic
STORAGES = {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]  # type: ignore[name-defined]  # noqa: F821
