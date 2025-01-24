import logging

from django.core.management.base import BaseCommand

from automation.agents.codebase_search.agent import CodebaseSearchAgent
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

logger = logging.getLogger("daiv.indexes")


class Command(BaseCommand):
    help = "Search documents in the index."

    def add_arguments(self, parser):
        parser.add_argument("--repo-id", type=str, help="Update a specific repository by namepsace, slug or id.")
        parser.add_argument(
            "--ref",
            type=str,
            help="Update a specific reference of the repository. If not provided, the default branch will be used.",
        )
        parser.add_argument("--show-content", action="store_true", help="Show the content of the documents.")
        parser.add_argument("query", type=str, help="The query to search for.")

    def handle(self, *args, **options):
        repo_client = RepoClient.create_instance()
        indexer = CodebaseIndex(repo_client=repo_client)

        namespace = None

        if options["repo_id"]:
            namespace = indexer._get_codebase_namespace(options["repo_id"], options["ref"])

        codebase_search = CodebaseSearchAgent(indexer.as_retriever(namespace))

        for doc in codebase_search.agent.invoke(options["query"]):
            self.stdout.write("-" * 100)
            self.stdout.write(f"{doc.metadata['repo_id']}[{doc.metadata['ref']}]: {doc.metadata['source']}\n")
            if options["show_content"]:
                self.stdout.write(doc.page_content)
            self.stdout.write("-" * 100)
