import logging

from django.core.management.base import BaseCommand

from codebase.clients import RepoClient
from codebase.conf import settings

logger = logging.getLogger("daiv.webhooks")


class Command(BaseCommand):
    help = "Set webhooks for all repositories."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", type=str, help="URL for webhook", required=True)
        parser.add_argument("--disable-ssl-verification", action="store_true", help="Disable SSL verification")

    def handle(self, *args, **options):
        repo_client = RepoClient.create_instance()
        for project in repo_client.list_repositories(load_all=True):
            repo_client.set_repository_webhooks(
                project.slug,
                f"{options['base_url']}/api/codebase/callbacks/{settings.CODEBASE_CLIENT}/",
                ["push_events", "issues_events", "note_events", "job_events"],
                enable_ssl_verification=not options["disable_ssl_verification"],
            )
            logger.info("Set webhook for %s.", project.slug)
        logger.info("All webhooks set successfully.")
