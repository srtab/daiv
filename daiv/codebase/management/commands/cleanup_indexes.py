import logging
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from gitlab import GitlabGetError

from codebase.clients import RepoClient
from codebase.conf import settings
from codebase.indexes import CodebaseIndex
from codebase.models import CodebaseNamespace, RepositoryInfo

logger = logging.getLogger("daiv.indexes")


class Command(BaseCommand):
    help = "Clean up outdated indexes and inaccessible repositories"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would be deleted without actually deleting anything"
        )
        parser.add_argument(
            "--check-accessibility",
            action="store_true",
            help="Check repository accessibility and remove indexes for inaccessible repositories",
        )
        parser.add_argument(
            "--cleanup-old-branches",
            action="store_true",
            help="Clean up indexes from non-default branches older than the threshold",
        )
        parser.add_argument(
            "--branch-age-days",
            type=int,
            default=settings.CLEANUP_OLD_BRANCH_AGE_DAYS,
            help=(
                f"Age threshold in days for non-default branch indexes "
                f"(default: {settings.CLEANUP_OLD_BRANCH_AGE_DAYS})"
            ),
        )
        parser.add_argument(
            "--all", action="store_true", help="Run all cleanup operations (accessibility check + old branches cleanup)"
        )
        parser.add_argument(
            "--repo-id", type=str, help="Limit cleanup to a specific repository by namespace, slug or id"
        )
        parser.add_argument(
            "--no-input", action="store_true", help="Run in non-interactive mode (automatically confirm deletions)"
        )

    def handle(self, *args, **options):
        if not any([options["check_accessibility"], options["cleanup_old_branches"], options["all"]]):
            raise CommandError(
                "You must specify at least one cleanup operation: "
                "--check-accessibility, --cleanup-old-branches, or --all"
            )

        self.repo_client = RepoClient.create_instance()
        self.indexer = CodebaseIndex(repo_client=self.repo_client)

        if options["all"]:
            options["check_accessibility"] = True
            options["cleanup_old_branches"] = True

        if options["check_accessibility"]:
            self._cleanup_inaccessible_repositories(options.get("repo_id"), options["dry_run"], options["no_input"])

        if options["cleanup_old_branches"]:
            self._cleanup_old_branch_indexes(
                options["branch_age_days"], options.get("repo_id"), options["dry_run"], options["no_input"]
            )

    def _cleanup_inaccessible_repositories(
        self, repo_id: str | None = None, dry_run: bool = False, no_input: bool = False
    ):
        """
        Clean up indexes for repositories that are no longer accessible.

        Args:
            repo_id (str | None): Limit cleanup to a specific repository by namespace, slug or id
            dry_run (bool): If True, only return what would be deleted without actually deleting
            no_input (bool): If True, proceed with deletion without confirmation
        """

        repo_infos = RepositoryInfo.objects.all()
        if repo_id:
            repo_infos = repo_infos.filter(Q(external_slug=repo_id) | Q(external_id=repo_id))

        inaccessible_repos = []

        for repo_info in repo_infos.iterator():
            try:
                # Try to get the repository to check if it's still accessible
                self.repo_client.get_repository(repo_info.external_slug)
            except GitlabGetError as e:
                if e.response_code in [403, 404]:  # Forbidden or Not Found
                    inaccessible_repos.append(repo_info)
                    logger.warning("Repository %s is no longer accessible: %s", repo_info.external_slug, e)
                # Other errors might be temporary, so we don't delete
            except Exception:
                logger.exception("Unexpected error accessing %s", repo_info.external_slug)

        if not inaccessible_repos:
            logger.info("No inaccessible repositories found to delete")
            return

        if dry_run:
            logger.info("DRY RUN: Skipping cleanup of inaccessible repositories.")
            return

        if not no_input:
            try:
                confirm = input("Do you want to proceed? [y/N]: ")
            except KeyboardInterrupt:
                logger.info("Cleanup cancelled")
                return

            if confirm.lower() != "y":
                logger.info("Cleanup cancelled")
                return

        for repo_info in inaccessible_repos:
            with transaction.atomic():
                try:
                    # Re-fetch with lock to ensure it still exists and lock it
                    repo_info = RepositoryInfo.objects.select_for_update().get(pk=repo_info.pk)

                    self.indexer.delete(repo_info.external_id, delete_all=True)

                    # Delete the repository info, namespaces and documents are deleted on the indexer delete
                    repo_info.delete()
                except Exception:
                    logger.exception("Error deleting indexes for %s", repo_info.external_slug)
                else:
                    logger.info("Deleted indexes for inaccessible repository: %s", repo_info.external_slug)

    def _cleanup_old_branch_indexes(
        self, age_days: int, repo_id: str | None = None, dry_run: bool = False, no_input: bool = False
    ):
        """
        Clean up indexes from non-default branches that are older than the threshold.

        Args:
            age_days (int): Age threshold in days for non-default branch indexes
            repo_id (str | None): Limit cleanup to a specific repository by namespace, slug or id
            dry_run (bool): If True, only return what would be deleted without actually deleting
            no_input (bool): If True, proceed with deletion without confirmation
        """
        current_time = timezone.now()
        cutoff_date = current_time - timedelta(days=age_days)

        # Get all namespaces that are old
        old_namespaces = (
            CodebaseNamespace.objects.filter(Q(created__lt=cutoff_date) | Q(status=CodebaseNamespace.Status.FAILED))
            .exclude(tracking_ref=F("repository_info__default_branch"))
            .select_related("repository_info")
        )

        if repo_id:
            old_namespaces = old_namespaces.filter(
                Q(repository_info__external_slug=repo_id) | Q(repository_info__external_id=repo_id)
            )

        if not old_namespaces.exists():
            logger.info("No old branch indexes found to delete")
            return

        for namespace in old_namespaces.iterator():
            if namespace.status == CodebaseNamespace.Status.FAILED:
                logger.info(
                    "Old branch index found: %s[%s] (failed to index)",
                    namespace.repository_info.external_slug,
                    namespace.tracking_ref,
                )
            else:
                logger.info(
                    "Old branch index found: %s[%s] (%s days old)",
                    namespace.repository_info.external_slug,
                    namespace.tracking_ref,
                    (current_time - namespace.created).days,
                )

        if dry_run:
            logger.info("DRY RUN: Skipping cleanup of old branch indexes.")
            return

        if not no_input:
            try:
                confirm = input("Do you want to proceed? [y/N]: ")
            except KeyboardInterrupt:
                logger.info("Cleanup cancelled")
                return

            if confirm.lower() != "y":
                logger.info("Cleanup cancelled")
                return

        for namespace in old_namespaces.iterator():
            with transaction.atomic():
                try:
                    self.indexer.delete(namespace.repository_info.external_id, ref=namespace.tracking_ref)
                except Exception:
                    logger.exception(
                        "Error deleting namespace %s[%s]",
                        namespace.repository_info.external_slug,
                        namespace.tracking_ref,
                    )
                else:
                    if namespace.status == CodebaseNamespace.Status.FAILED:
                        logger.info(
                            "Deleted old branch index: %s[%s] (failed to index)",
                            namespace.repository_info.external_slug,
                            namespace.tracking_ref,
                        )
                    else:
                        logger.info(
                            "Deleted old branch index: %s[%s] (%s days old)",
                            namespace.repository_info.external_slug,
                            namespace.tracking_ref,
                            (current_time - namespace.created).days,
                        )
