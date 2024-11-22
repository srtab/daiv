from django.conf import settings  # NOQA
from decouple import config

from appconf import AppConf


class CoreAppConf(AppConf):
    """
    Core configurations.
    https://django-appconf.readthedocs.io/en/latest/
    """

    SANDBOX_URL = config("DAIV_SANDBOX_URL", default="http://sandbox:8000")
    SANDBOX_TIMEOUT = 600.0  # 10 minutes (in seconds)
    SANDBOX_API_KEY = config("DAIV_SANDBOX_API_KEY")

    class Meta:
        proxy = True
        prefix = "DAIV"
