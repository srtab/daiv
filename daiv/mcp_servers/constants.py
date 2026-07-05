from __future__ import annotations

import re

# Same character shape as SKILL_NAME_RE (see daiv/skills/constants.py):
# lowercase alphanumeric + dashes, must start with alphanumeric. Capped at 80
# chars here (SKILL_NAME_RE caps at 63) to match the model's SlugField length.
MCP_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

# Reserved so a server can't be named after a top-level mcp_servers action/route
# ("new", "test") — avoids user confusion even though no current route collides on these.
RESERVED_MCP_NAMES: frozenset[str] = frozenset({"new", "test"})
