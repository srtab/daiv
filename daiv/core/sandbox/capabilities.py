from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils.translation import gettext_noop

import httpx

from .client import DAIVSandboxClient
from .schemas import SandboxCapabilities

logger = logging.getLogger("daiv.sandbox")

CAPABILITIES_CACHE_KEY = "sandbox:capabilities"
CAPABILITIES_CACHE_TTL = 300

NO_BROWSER_MESSAGE = gettext_noop(
    "The sandbox service does not support the browser, so runs get no browser tools. "
    "Upgrade daiv-sandbox to 0.6.0 or later."
)


class SandboxCapabilityProbe:
    """What the sandbox service can do, asked at most once per cache period across the workers sharing the cache.

    A missing capability is logged at ERROR on each fresh answer, so an operator sees it once per period instead of
    once per run.
    """

    async def aget(self, client: DAIVSandboxClient | None = None) -> SandboxCapabilities | None:
        """The service's answer; ``None`` when it could not be asked, which is not cached, so the next call asks
        again."""
        cached = await cache.aget(CAPABILITIES_CACHE_KEY)
        if cached is not None:
            return SandboxCapabilities.model_validate(cached)
        capabilities = await self._fetch(client)
        if capabilities is not None:
            await cache.aset(CAPABILITIES_CACHE_KEY, capabilities.model_dump(), CAPABILITIES_CACHE_TTL)
        return capabilities

    async def _fetch(self, client: DAIVSandboxClient | None) -> SandboxCapabilities | None:
        try:
            if client is not None:
                capabilities = await client.get_capabilities()
            else:
                async with DAIVSandboxClient() as own_client:
                    capabilities = await own_client.get_capabilities()
        except httpx.HTTPError, ValueError:
            logger.exception("Could not ask the sandbox service for its capabilities, so runs get no browser tools.")
            return None
        if not capabilities.browser:
            logger.error(NO_BROWSER_MESSAGE)
        return capabilities
