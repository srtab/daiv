import logging

from django.core.management.base import BaseCommand

from neomodel import db

logger = logging.getLogger("daiv.graph")


class Command(BaseCommand):
    help = "Clear the neo4j database"

    def add_arguments(self, parser):
        parser.add_argument("--noinput", "--no-input", action="store_true", help="Skip confirmation prompt")
        parser.add_argument("--no-clear-constraints", action="store_true", help="Skip clearing constraints")
        parser.add_argument("--no-clear-indexes", action="store_true", help="Skip clearing indexes")

    def handle(self, *args, **options):
        if not options["noinput"]:
            confirm = input(
                "You are about to clear the entire Neo4j database. "
                "This operation cannot be undone.\n"
                "Are you sure you want to continue? [y/N]: "
            )
            if confirm.lower() != "y":
                logger.warning("Operation cancelled.")
                return

        clear_constraints = not options["no_clear_constraints"]
        clear_indexes = not options["no_clear_indexes"]

        db.clear_neo4j_database(clear_constraints, clear_indexes)
        logger.info("Successfully cleared the Neo4j database.")
