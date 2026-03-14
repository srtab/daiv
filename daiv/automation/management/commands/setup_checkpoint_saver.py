import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from langgraph.checkpoint.redis import RedisSaver

logger = logging.getLogger("daiv.checkpoint")


class Command(BaseCommand):
    help = "Initialize LangGraph Redis checkpointer indices"

    def handle(self, *args, **options):
        with RedisSaver.from_conn_string(settings.DJANGO_REDIS_CHECKPOINT_URL) as checkpointer:
            checkpointer.setup()
        logger.info("Redis checkpointer indices created")
