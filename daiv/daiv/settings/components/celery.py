from decouple import config
from get_docker_secret import get_docker_secret

# CELERY - http://docs.celeryproject.org/en/latest/userguide/configuration.html

CELERY_BROKER_URL = get_docker_secret("DJANGO_BROKER_URL", default="memory:///")
CELERY_BROKER_USE_SSL = config("DJANGO_BROKER_USE_SSL", default=False, cast=bool)
CELERY_TASK_COMPRESSION = "gzip"
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # half hour
CELERY_WORKER_MAX_MEMORY_PER_CHILD = 200 * 1000  # 200Mb
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
