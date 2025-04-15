import logging

from django.core.management.base import BaseCommand

from neomodel import db

logger = logging.getLogger("daiv.graph")


class Command(BaseCommand):
    help = "Install labels and constraints for your neo4j database"

    def handle(self, *args, **options):
        logger.info("Starting installation of Neo4j labels and constraints")
        try:
            db.install_all_labels(stdout=self.stdout)
            logger.info("Successfully installed Neo4j labels and constraints")
        except Exception as e:
            logger.error("Failed to install Neo4j labels and constraints: %s", str(e))
            raise
