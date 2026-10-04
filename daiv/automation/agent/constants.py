import tempfile
from pathlib import Path

from daiv.settings.components import PROJECT_DIR

# Path where the builtin skills are stored in the filesystem to be copied to the repository.
BUILTIN_SKILLS_PATH = PROJECT_DIR / "automation" / "agent" / "skills"

# Unified sandbox workspace. One backend (sandbox-authoritative) serves all of /workspace.
# The sandbox holds the one true workspace: repo at /workspace/repo, seeded skills at
# /workspace/skills, and the per-run scratchpad at /workspace/tmp.
WORKSPACE_PATH = "/workspace"
REPO_PATH = "/workspace/repo"
SKILLS_PATH = "/workspace/skills"
# Per-run ephemeral scratchpad. In sandbox mode it is a real sandbox dir; in disk mode it is the
# composite's default fall-through area (alongside offloaded artifacts).
TMP_PATH = "/workspace/tmp"

# On-disk root the composite backend mounts at ``SKILLS_PATH``. Created at module
# import (loud failure if ``$TMPDIR`` is unwritable, which would manifest as a startup
# ImportError on production, or a confusing pytest collection error locally).
#
# Recreated only on container image change; stale entries from a prior deploy may
# survive a container restart on hosts where ``/tmp`` is not a tmpfs, so do not rely on
# this for runtime invalidation. ``SkillsMiddleware._collect_skill_files`` makes per-run
# uploads idempotent via an existence check against this path.
SKILLS_CACHE_PATH = Path(tempfile.gettempdir()) / "daiv-skills"
SKILLS_CACHE_PATH.mkdir(parents=True, exist_ok=True)

# Path where the skills are stored in repository.
CURSOR_SKILLS_PATH = ".cursor/skills"
CLAUDE_CODE_SKILLS_PATH = ".claude/skills"
AGENTS_SKILLS_PATH = ".agents/skills"

# Paths where the skills are stored in repository.
SKILLS_SOURCES = [CURSOR_SKILLS_PATH, CLAUDE_CODE_SKILLS_PATH, AGENTS_SKILLS_PATH]

SKILLS_TOOL_NAME = "skill"

# Path where the custom subagents are stored in repository.
AGENTS_SUBAGENTS_PATH = ".agents/subagents"

# Paths where the custom subagents are stored in repository.
SUBAGENTS_SOURCES = [AGENTS_SUBAGENTS_PATH]

# Path where the memory is stored in repository.
AGENTS_MEMORY_PATH = ".agents/AGENTS.md"
