import logging

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from langgraph.checkpoint.postgres import PostgresSaver

logger = logging.getLogger("daiv.agents")

STALE_DEFAULT_DAYS = 180

# SQL to find thread_ids where the most recent checkpoint is older than a given timestamp.
# The checkpoint JSONB column contains a "ts" field with an ISO 8601 timestamp.
STALE_THREADS_SQL = """
SELECT thread_id
FROM checkpoints
GROUP BY thread_id
HAVING MAX((checkpoint->>'ts')::timestamptz) <= %s
"""


class Command(BaseCommand):
    help = "Delete checkpoint data for threads older than the specified number of days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            "-d",
            type=int,
            default=STALE_DEFAULT_DAYS,
            help=(
                f"Delete threads whose latest checkpoint is older than this many days (default: {STALE_DEFAULT_DAYS})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only list threads that would be deleted, without actually deleting them.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timezone.timedelta(days=days)

        with PostgresSaver.from_conn_string(django_settings.DB_URI) as checkpointer:
            with checkpointer._cursor() as cur:
                cur.execute(STALE_THREADS_SQL, (cutoff.isoformat(),))
                thread_ids = [row["thread_id"] for row in cur.fetchall()]

            if not thread_ids:
                logger.info("No stale checkpoint threads found (older than %d days).", days)
                return

            if dry_run:
                logger.info("Dry run: would delete %d threads older than %d days.", len(thread_ids), days)
                for thread_id in thread_ids:
                    logger.info("  Thread: %s", thread_id)
                return

            for thread_id in thread_ids:
                checkpointer.delete_thread(thread_id)

            logger.info("Deleted %d checkpoint threads older than %d days.", len(thread_ids), days)
