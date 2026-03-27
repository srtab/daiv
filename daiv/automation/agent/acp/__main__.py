"""
Standalone DAIV ACP server entry point.

Runs the DAIV agent over the Agent Client Protocol (ACP) via stdio,
without requiring Django or any external infrastructure (database, Redis).

Usage:
    python -m automation.agent.acp

The only required environment variables are the LLM API keys, e.g.:
    AUTOMATION_ANTHROPIC_API_KEY=sk-...
    AUTOMATION_OPENROUTER_API_KEY=sk-...
"""

import asyncio
import logging
import sys


def _patch_acp_schema():
    """
    Patch the ACP SDK schema for compatibility with newer clients like Zed.

    - Accept string protocol versions (e.g., "0.11.2") in addition to integers.
      Zed sends semver strings, but agent-client-protocol<=0.9.0 only accepts int.
    - Make mcp_servers optional in NewSessionRequest (Zed may omit it).
    """
    from acp import schema

    for model in (schema.InitializeRequest, schema.InitializeResponse):
        field = model.model_fields["protocol_version"]
        if field.annotation is int:
            field.annotation = int | str
            field.metadata = [m for m in field.metadata if not hasattr(m, "ge") and not hasattr(m, "le")]
            model.model_rebuild(force=True)

    mcp_field = schema.NewSessionRequest.model_fields.get("mcp_servers")
    if mcp_field and mcp_field.is_required():
        mcp_field.default = []
        schema.NewSessionRequest.model_rebuild(force=True)


def _patch_acp_error_logging():
    """Patch the ACP router to log original exceptions before they're wrapped."""
    import acp.router as router_mod

    _original_route = router_mod.Route

    class LoggingRoute(_original_route):
        async def handle(self, params):
            try:
                return await super().handle(params)
            except Exception:
                logging.getLogger("daiv.acp").exception("ACP route error")
                raise

    router_mod.Route = LoggingRoute


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    _patch_acp_schema()
    _patch_acp_error_logging()

    from acp import run_agent as run_acp_agent

    from automation.agent.acp import create_acp_server

    server = create_acp_server()
    logging.getLogger("daiv.acp").info("Starting DAIV ACP server over stdio")
    asyncio.run(run_acp_agent(server))


if __name__ == "__main__":
    main()
