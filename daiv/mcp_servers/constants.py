from __future__ import annotations

import re

# Same character shape as SKILL_NAME_RE (see daiv/skills/constants.py):
# lowercase alphanumeric + dashes, must start with alphanumeric. Capped at 80
# chars here (SKILL_NAME_RE caps at 63) to match the model's SlugField length.
MCP_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

# Names that collide with non-slug URL segments under the mcp_servers namespace
# (e.g. ``/new/``, ``/test/``) — reserved so a server can't shadow those routes.
RESERVED_MCP_NAMES: frozenset[str] = frozenset({"new", "test"})

TOOLS_CACHE_KEY = "mcp_server:tools:{name}:{stamp}"
TOOLS_CACHE_TIMEOUT = 60  # seconds
# Empty/unreachable discoveries are cached only briefly: long enough to avoid re-running a
# 5s handshake on every page render, short enough that a recovered server reappears quickly.
TOOLS_NEGATIVE_CACHE_TIMEOUT = 10  # seconds
