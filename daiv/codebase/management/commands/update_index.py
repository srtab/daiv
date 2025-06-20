import logging
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand
from django.db import transaction

from gitlab import GitlabGetError

from codebase.base import Repository
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

logger = logging.getLogger("daiv.indexes")


class Command(BaseCommand):
    help = "Update the index of repositories.\nIf no repository is provided, all repositories will be updated."

    def add_arguments(self, parser):
        parser.add_argument("--repo-id", type=str, help="Update a specific repository by namepsace, slug or id.")
        parser.add_argument(
            "--ref",
            type=str,
            help="Update a specific reference of the repository. If not provided, the default branch will be used.",
        )
        parser.add_argument(
            "--topic",
            dest="topics",
            type=str,
            nargs="+",
            help="Only update repositories with the given topics. "
            "If repo-id is provided, this argument will be ignored.",
        )
        parser.add_argument(
            "--max-workers", type=int, default=4, help="The number of repositories to update in parallel (default: 4)"
        )
        parser.add_argument("--reset", action="store_true", help="Reset the index before updating.")
        parser.add_argument(
            "--reset-all",
            action="store_true",
            help="Reset all indexes for the repository, ignoring the reference branch.",
        )
        parser.add_argument(
            "--exclude-repo-id",
            dest="exclude_repo_ids",
            default=[],
            type=str,
            nargs="+",
            help="Exclude specific repositories by slug or id. If repo-id is provided, this argument will be ignored.",
        )
        parser.add_argument(
            "--semantic-augmented-context",
            action="store_true",
            help=(
                "Apply semantic augmented context for the index. "
                "This will add a description to each code snippet in the index."
            ),
        )

    def handle(self, *args, **options):
        repo_client = RepoClient.create_instance()
        indexer = CodebaseIndex(
            repo_client=repo_client, semantic_augmented_context=options["semantic_augmented_context"]
        )

        repositories = []

        if options["repo_id"]:
            try:
                repositories.append(repo_client.get_repository(options["repo_id"]))
            except GitlabGetError:
                logger.warning("Repository %s not found, ignoring.", options["repo_id"])
        else:
            repositories = repo_client.list_repositories(topics=options["topics"] or None, load_all=True)

        with ThreadPoolExecutor(max_workers=options["max_workers"]) as executor:
            executor.map(lambda repository: self._update_repository(indexer, repository, options), repositories)

    @transaction.atomic
    def _update_repository(self, indexer: CodebaseIndex, repository: Repository, options: dict):
        """
        Update the index of a repository.

        This method is synchronous because it uses Django's transaction.atomic, which doesn't support async mode yet.

        Args:
            indexer: The indexer to use.
            repository: The repository to update.
            options: The options for the update.
        """
        if not options["repo_id"] and (
            repository.slug in options["exclude_repo_ids"] or repository.pk in options["exclude_repo_ids"]
        ):
            return

        if options["reset"] or options["reset_all"]:
            indexer.delete(repo_id=repository.pk, ref=options["ref"], delete_all=options["reset_all"])
        indexer.update(repo_id=repository.slug, ref=options["ref"])
