from __future__ import annotations

import re

# Same shape as SKILL_NAME_RE (see daiv/skills/constants.py): lowercase
# alphanumeric + dashes, must start with alphanumeric, max 80 chars.
MCP_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

TOOLS_CACHE_KEY = "mcp_server:tools:{name}:{stamp}"
TOOLS_CACHE_TIMEOUT = 60  # seconds
