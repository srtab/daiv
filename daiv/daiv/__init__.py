# This will make sure the app is always imported when
# Django starts so that shared_task will use this app.
from .celeryapp import app as celery_app

__version__ = "0.1.0-alpha.22"

__all__ = ("celery_app",)
