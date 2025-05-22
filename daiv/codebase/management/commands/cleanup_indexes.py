import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from gitlab import GitlabGetError

from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from codebase.models import CodebaseNamespace, RepositoryInfo

logger = logging.getLogger("daiv.indexes")


class Command(BaseCommand):
    help = "Clean up inaccessible repositories and old non-default branch indexes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--branch-age-days",
            type=int,
            default=30,
            help="Delete non-default branch namespaces older than this many days (default: 30)",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would be deleted without actually deleting"
        )

    def handle(self, *args, **options):
        repo_client = RepoClient.create_instance()
        indexer = CodebaseIndex(repo_client=repo_client)

        dry_run = options["dry_run"]
        branch_age_days = options["branch_age_days"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be made"))

        # Statistics tracking
        total_repos_checked = 0
        inaccessible_repos = 0
        old_namespaces_cleaned = 0

        # 1. Cleanup inaccessible repositories
        self.stdout.write("Checking repository accessibility...")

        repositories = RepositoryInfo.objects.all()
        total_repos_checked = repositories.count()

        for repo_info in repositories:
            try:
                repo_client.get_repository(repo_info.external_slug)
                logger.debug("Repository %s is accessible", repo_info.external_slug)
            except GitlabGetError as e:
                logger.warning(
                    "Repository %s is inaccessible (error: %s), marking for cleanup", repo_info.external_slug, e
                )
                inaccessible_repos += 1

                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Would delete all namespaces for inaccessible repository: {repo_info.external_slug}"
                        )
                    )
                else:
                    self._cleanup_repository_namespaces(indexer, repo_info)

        # 2. Cleanup old non-default branch namespaces
        self.stdout.write(f"Cleaning up non-default branch namespaces older than {branch_age_days} days...")

        cutoff_date = timezone.now() - timedelta(days=branch_age_days)

        # Find namespaces with non-default branch documents
        old_namespaces = (
            CodebaseNamespace.objects.filter(created__lt=cutoff_date, documents__is_default_branch=False)
            .distinct()
            .select_related("repository_info")
        )

        old_namespaces_count = old_namespaces.count()
        old_namespaces_cleaned = old_namespaces_count

        for namespace in old_namespaces:
            if dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"Would delete old non-default branch namespace: "
                        f"{namespace.repository_info.external_slug}[{namespace.tracking_ref}] "
                        f"(created: {namespace.created})"
                    )
                )
            else:
                self._cleanup_namespace(indexer, namespace)

        # 3. Summary
        self.stdout.write(self.style.SUCCESS("\nCleanup Summary:"))
        self.stdout.write(f"Total repositories checked: {total_repos_checked}")
        self.stdout.write(f"Inaccessible repositories found: {inaccessible_repos}")
        self.stdout.write(f"Old non-default branch namespaces cleaned: {old_namespaces_cleaned}")

        if dry_run:
            self.stdout.write(self.style.WARNING("This was a dry run - no actual changes were made"))
        else:
            self.stdout.write(self.style.SUCCESS("Cleanup completed successfully"))

    @transaction.atomic
    def _cleanup_repository_namespaces(self, indexer: CodebaseIndex, repo_info: RepositoryInfo):
        """
        Clean up all namespaces for an inaccessible repository.

        Args:
            indexer: CodebaseIndex instance for cleanup operations
            repo_info: RepositoryInfo instance for the inaccessible repository
        """
        namespaces = repo_info.namespaces.all()
        namespace_count = namespaces.count()

        logger.info(
            "Cleaning up %d namespaces for inaccessible repository %s", namespace_count, repo_info.external_slug
        )

        for namespace in namespaces:
            self._cleanup_namespace(indexer, namespace)

        self.stdout.write(
            self.style.SUCCESS(f"Cleaned up {namespace_count} namespaces for repository: {repo_info.external_slug}")
        )

    def _cleanup_namespace(self, indexer: CodebaseIndex, namespace: CodebaseNamespace):
        """
        Clean up a single namespace using the CodebaseIndex.delete() method.

        Args:
            indexer: CodebaseIndex instance for cleanup operations
            namespace: CodebaseNamespace instance to clean up
        """
        try:
            logger.info("Cleaning up namespace %s[%s]", namespace.repository_info.external_slug, namespace.tracking_ref)

            # Use the existing CodebaseIndex.delete() method for proper cleanup
            indexer.delete(repo_id=namespace.repository_info.external_slug, ref=namespace.tracking_ref)

            logger.info(
                "Successfully cleaned up namespace %s[%s]",
                namespace.repository_info.external_slug,
                namespace.tracking_ref,
            )

        except Exception as e:
            logger.error(
                "Error cleaning up namespace %s[%s]: %s",
                namespace.repository_info.external_slug,
                namespace.tracking_ref,
                e,
            )
            raise
