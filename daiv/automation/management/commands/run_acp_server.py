import asyncio

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the DAIV ACP server over stdio"

    def handle(self, *args, **options):
        asyncio.run(self._run())

    async def _run(self):
        from acp import run_agent as run_acp_agent

        from automation.agent.acp import create_acp_server

        server = create_acp_server()
        await run_acp_agent(server)
