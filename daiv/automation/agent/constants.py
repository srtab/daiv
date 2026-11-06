# Unified sandbox workspace. One backend (sandbox-authoritative) serves all of /workspace.
# The sandbox holds the one true workspace: repo at /workspace/repo, seeded skills at
# /workspace/skills, and the per-run scratchpad at /workspace/tmp.
WORKSPACE_PATH = "/workspace"
REPO_PATH = "/workspace/repo"
EXTRA_REPOS_PATH = "/workspace/repos"
# Per-run ephemeral scratchpad. In sandbox mode it is a real sandbox dir; in disk mode it is the
# composite's default fall-through area (alongside offloaded artifacts).
TMP_PATH = "/workspace/tmp"

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
