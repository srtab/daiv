from celery import Celery, signals
from langchain_core.tracers.langchain import wait_for_all_tracers

app = Celery("daiv")

app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@signals.task_postrun.connect
def flush_after_tasks(**kwargs):
    wait_for_all_tracers()
