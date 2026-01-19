import logging

from django.core.management.base import BaseCommand

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings
from core.conf import settings as core_settings
from core.utils import build_uri

logger = logging.getLogger("daiv.webhooks")


class Command(BaseCommand):
    help = "Set webhooks for all repositories with optional secret token for validation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            type=str,
            help=f"Base URL of DAIV webapp, i.e. {core_settings.EXTERNAL_URL.encoded_string()}",
            default=core_settings.EXTERNAL_URL.encoded_string(),
        )
        parser.add_argument(
            "--disable-ssl-verification", action="store_true", help="Disable SSL verification for webhook"
        )
        parser.add_argument(
            "--secret-token", type=str, help="Secret token for webhook validation (overrides settings)", default=None
        )

    def handle(self, *args, **options):
        if settings.CLIENT == GitPlatform.GITHUB:
            logger.warning(
                "GitHub webhooks must be set on the GitHub App configuration: "
                "https://srtab.github.io/daiv/dev/getting-started/configuration/#step-4-verify-webhook-configuration"
            )
            return

        repo_client = RepoClient.create_instance()
        # Use secret token from command line or from settings
        secret_token = options["secret_token"]
        if not secret_token and settings.GITLAB_WEBHOOK_SECRET:
            secret_token = settings.GITLAB_WEBHOOK_SECRET.get_secret_value()

        for project in repo_client.list_repositories():
            created = repo_client.set_repository_webhooks(
                project.slug,
                build_uri(options["base_url"], f"/api/codebase/callbacks/{settings.CLIENT}/"),
                enable_ssl_verification=not options["disable_ssl_verification"],
                secret_token=secret_token,
            )
            if created:
                logger.info("Created webhook for %s.", project.slug)
            else:
                logger.info("Updated webhook for %s.", project.slug)
        logger.info("All webhooks set successfully.")
