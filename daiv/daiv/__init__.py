# This will make sure the app is always imported when
# Django starts so that shared_task will use this app.
from .celeryapp import app as celery_app
from .celeryapp import async_task

__version__ = "0.3.0"
USER_AGENT = f"python-daiv-agent/{__version__}"

__all__ = ("celery_app", "async_task", "USER_AGENT")
