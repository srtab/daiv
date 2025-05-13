import logging

from django.core.management.base import BaseCommand

from codebase.clients import RepoClient
from codebase.conf import settings
from core.conf import settings as core_settings

logger = logging.getLogger("daiv.webhooks")


class Command(BaseCommand):
    help = "Set webhooks for all repositories with optional secret token for validation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            type=str,
            help="Base URL of DAIV webapp, i.e. https://app:8000",
            default=core_settings.EXTERNAL_URL,
        )
        parser.add_argument(
            "--disable-ssl-verification", action="store_true", help="Disable SSL verification for webhook"
        )
        parser.add_argument(
            "--secret-token", type=str, help="Secret token for webhook validation (overrides settings)", default=None
        )

    def handle(self, *args, **options):
        repo_client = RepoClient.create_instance()
        # Use secret token from command line or from settings
        secret_token = options["secret_token"]
        if not secret_token and settings.CLIENT == "gitlab" and settings.GITLAB_WEBHOOK_SECRET:
            secret_token = settings.GITLAB_WEBHOOK_SECRET.get_secret_value()
        elif not secret_token and settings.CLIENT == "github" and settings.GITHUB_WEBHOOK_SECRET:
            secret_token = settings.GITHUB_WEBHOOK_SECRET.get_secret_value()

        for project in repo_client.list_repositories(load_all=True):
            created = repo_client.set_repository_webhooks(
                project.slug,
                f"{options['base_url']}/api/codebase/callbacks/{settings.CLIENT}/",
                ["push_events", "issues_events", "note_events", "pipeline_events"],
                enable_ssl_verification=not options["disable_ssl_verification"],
                secret_token=secret_token,
            )
            if created:
                logger.info("Created webhook for %s.", project.slug)
            else:
                logger.info("Updated webhook for %s.", project.slug)
        logger.info("All webhooks set successfully.")
