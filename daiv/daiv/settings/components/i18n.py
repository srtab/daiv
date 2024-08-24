from django.utils.translation import gettext_lazy as _

from daiv.settings.components import PROJECT_DIR

LANGUAGES = (("pt", _("Portuguese")), ("en", _("English")), ("en-GB", _("English (UK)")))
LANGUAGE_CODE = "en"

TIME_ZONE = "Europe/Lisbon"
USE_TZ = True

USE_THOUSAND_SEPARATOR = True

LOCALE_PATHS = (PROJECT_DIR / "accounts" / "locale",)

# CELERY

CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = False
