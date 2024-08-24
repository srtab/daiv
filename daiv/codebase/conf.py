from django.conf import settings  # NOQA
from decouple import config
from get_docker_secret import get_docker_secret

from appconf import AppConf


class CodebaseAppConf(AppConf):
    """
    Codebase pecific configurations.
    https://django-appconf.readthedocs.io/en/latest/
    """

    COLLECTION_NAME = "codebase"
    CLIENT = config("CODEBASE_CLIENT", default="gitlab")

    GITLAB_URL = config("GITLAB_URL")
    GITLAB_AUTH_TOKEN = get_docker_secret("GITLAB_AUTH_TOKEN")

    CHROMA_HOST = config("CHROMA_HOST")
    CHROMA_PORT = config("CHROMA_PORT", default="8000")

    class Meta:
        proxy = True
        prefix = "CODEBASE"
