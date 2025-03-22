import logging

from django.core.management.base import BaseCommand

from gitlab import GitlabGetError

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

    def handle(self, *args, **options):
        repo_client = RepoClient.create_instance()
        indexer = CodebaseIndex(repo_client=repo_client)

        repositories = []

        if options["repo_id"]:
            try:
                repositories.append(repo_client.get_repository(options["repo_id"]))
            except GitlabGetError:
                logger.error("Repository %s not found, ignoring.", options["repo_id"])
        else:
            repositories = repo_client.list_repositories(topics=options["topics"] or None, load_all=True)

        for repository in repositories:
            if not options["repo_id"] and (
                repository.slug in options["exclude_repo_ids"] or repository.pk in options["exclude_repo_ids"]
            ):
                continue

            if options["reset"] or options["reset_all"]:
                indexer.delete(repo_id=repository.slug, ref=options["ref"], delete_all=options["reset_all"])
            indexer.update(repo_id=repository.slug, ref=options["ref"])
