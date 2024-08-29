from django.core.management.base import BaseCommand

from codebase.clients import RepoClient
from codebase.conf import settings


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
                f"{options["base_url"]}/api/codebase/webhooks/{settings.CODEBASE_CLIENT}/",
                ["push_events", "merge_requests_events", "issues_events"],
                push_events_branch_filter=project.default_branch,
                enable_ssl_verification=not options["disable_ssl_verification"],
            )
            self.stdout.write(self.style.SUCCESS(f"Set webhook for {project.slug}."))
        self.stdout.write(self.style.SUCCESS("All webhooks set successfully."))