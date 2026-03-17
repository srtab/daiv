import logging

from django.core.management.base import BaseCommand

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.clients.base import WebhookSetupResult
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
        parser.add_argument(
            "--update", action="store_true", help="Update existing webhooks in addition to creating new ones"
        )
        parser.add_argument("--repo-id", type=str, help="Restrict setup to a specific repository ID", default=None)

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

        update = options["update"]
        callback_url = build_uri(options["base_url"], f"/api/codebase/callbacks/{settings.CLIENT}/")

        if options["repo_id"]:
            projects = [repo_client.get_repository(options["repo_id"])]
        else:
            projects = repo_client.list_repositories()

        for project in projects:
            result = repo_client.set_repository_webhooks(
                project.slug,
                callback_url,
                enable_ssl_verification=not options["disable_ssl_verification"],
                secret_token=secret_token,
                update=update,
            )
            if result == WebhookSetupResult.CREATED:
                logger.info("Created webhook for %s.", project.slug)
            elif result == WebhookSetupResult.UPDATED:
                logger.info("Updated webhook for %s.", project.slug)
            else:
                logger.debug("Webhook already exists for %s, skipping.", project.slug)
        logger.info("All webhooks set successfully.")
