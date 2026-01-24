import logging

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand

from langgraph.checkpoint.postgres import PostgresSaver

logger = logging.getLogger("daiv.agents")


class Command(BaseCommand):
    help = "Delete a conversation thread"

    def add_arguments(self, parser):
        parser.add_argument(
            "--thread-id", "-t", type=str, required=True, help="The ID of the conversation thread to delete."
        )

    def handle(self, *args, **options):
        thread_id = options["thread_id"]
        with PostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            checkpointer.delete_thread(thread_id)
        logger.info("Thread %s deleted successfully", thread_id)
