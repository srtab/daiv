from django.conf import settings  # NOQA
from decouple import config
from get_docker_secret import get_docker_secret

from appconf import AppConf


class CodebaseAppConf(AppConf):
    """
    Codebase pecific configurations.
    https://django-appconf.readthedocs.io/en/latest/
    """

    CLIENT = config("CODEBASE_CLIENT", default="gitlab")

    GITLAB_URL = config("GITLAB_URL", default=None)
    GITLAB_AUTH_TOKEN = get_docker_secret("GITLAB_AUTH_TOKEN")

    class Meta:
        proxy = True
        prefix = "CODEBASE"
