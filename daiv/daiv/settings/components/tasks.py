from core.constants import TASK_QUEUE_DEFAULT, TASK_QUEUE_INTERACTIVE

TASKS = {
    "default": {
        "BACKEND": "core.backends.deduplicating.DeduplicatingDatabaseBackend",
        "QUEUES": [TASK_QUEUE_DEFAULT, TASK_QUEUE_INTERACTIVE],
    }
}
