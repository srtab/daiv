from pathlib import Path

from django.core.management.base import BaseCommand

from automation.agent.mcp.schemas import McpConfiguration


class Command(BaseCommand):
    help = "Generate the MCP proxy configuration"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            "-o",
            type=Path,
            help=(
                "File path to save the configuration to. "
                "If not provided, the configuration will be printed to the console."
            ),
        )

    def handle(self, *args, **options):
        config = McpConfiguration.populate()
        json_dump = config.model_dump_json(indent=2, by_alias=True, exclude_none=True)
        if options["output"]:
            with options["output"].open("w") as f:
                f.write(json_dump)
        else:
            self.stdout.write(json_dump)
