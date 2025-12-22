"""
Middleware for loading and exposing agent skills to the system prompt.

This middleware implements Anthropic's "Agent Skills" pattern with progressive disclosure:
1. Parse YAML frontmatter from SKILL.md files at session start
2. Inject skills metadata (name + description) into system prompt
3. Agent reads full SKILL.md content when relevant to a task

Skills directory structure (project-level): {PROJECT_ROOT}/.daiv/skills/

Example structure:
{PROJECT_ROOT}/.daiv/skills/
├── web-research/
│   ├── SKILL.md        # Required: YAML frontmatter + instructions
│   └── helper.py       # Optional: supporting files
├── code-review/
│   ├── SKILL.md
│   └── checklist.md
"""

from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langgraph.runtime import Runtime  # noqa: TC002

from .load import BUILTIN_SKILLS_DIR, SkillMetadata, list_skills

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BACKEND_TYPES


BUILTIN_SKILLS_DEST_DIR = "/skills/"


class SkillsState(AgentState):
    """State for the skills middleware."""

    skills_metadata: NotRequired[list[SkillMetadata]]
    """List of loaded skill metadata (name, description, path)."""


class SkillsStateUpdate(TypedDict):
    """State update for the skills middleware."""

    skills_metadata: list[SkillMetadata]
    """List of loaded skill metadata (name, description, path)."""


# Skills System Documentation
SKILLS_SYSTEM_PROMPT = """\
## Skills System

You have access to a skills library that provides specialized capabilities and domain knowledge.

**Skills:**

{skills_list}

**How to Use Skills (Progressive Disclosure):**

Skills follow a **progressive disclosure** pattern - you know they exist (name + description above), but you only read the full instructions when needed:

1. **Recognize when a skill applies**: Check if the user's task matches any skill's description
2. **Read the skill's full instructions**: The skill list above shows the exact path to use with `read_file` tool
3. **Follow the skill's instructions**: SKILL.md contains step-by-step workflows, best practices, and examples
4. **Access supporting files**: Skills may include Python scripts, configs, or reference docs - use absolute paths for skills

**When to Use Skills:**
- When the user's request matches a skill's domain (e.g., "research X" → web-research skill)
- When you need specialized knowledge or structured workflows
- When a skill provides proven patterns for complex tasks

**Skills are Self-Documenting:**
- Each SKILL.md tells you exactly what the skill does and how to use it
- The skill list above shows the full path for each skill's SKILL.md file

**Executing Skill Scripts:**
Skills may contain Python scripts or other executable files. Always use absolute paths for skills.

**Example Workflow:**
<example>
User: "Can you research the latest developments in quantum computing?"

1. Check available skills above → See "web-research" skill with its full path
2. Read the skill using the path shown in the list
3. Follow the skill's research workflow (search → organize → synthesize)
4. Use any helper scripts with absolute paths for skills
</example>

Remember: Skills are tools to make you more capable and consistent. When in doubt, check if a skill exists for the task."""  # noqa: E501


class SkillsMiddleware(AgentMiddleware):
    """
    Middleware for loading and exposing agent skills.

    This middleware implements Anthropic's agent skills pattern:
    - Loads skills metadata (name, description) from YAML frontmatter at session start
    - Injects skills list into system prompt for discoverability
    - Agent reads full SKILL.md content when a skill is relevant (progressive disclosure)

    Supports both builtin and project-level skills:
    - Builtin skills: daiv/automation/agents/skills/builtin/
    - Project skills: {PROJECT_ROOT}/.daiv/skills/
    - Project skills override builtin skills with the same name
    """

    state_schema = SkillsState

    def __init__(self, *, backend: BACKEND_TYPES, scope: Literal["issue", "merge_request"] | None = None):
        """
        Initialize the skills middleware.

        Args:
            scope: Scope of the skills to load. If None, all skills will be loaded.
            backend: The backend to use for reading the skills.
        """
        self.scope = scope
        self.backend = backend
        self.project_skills_dir = ".daiv/skills/"
        self.builtin_skills_dir = "/skills/"

    def before_agent(self, state: SkillsState, runtime: Runtime) -> SkillsStateUpdate | None:
        """
        Load skills metadata before agent execution.

        This runs once at session start to discover available skills from both builtin-level and
        project-level directories.

        Args:
            state: Current agent state.
            runtime: Runtime context.

        Returns:
            Updated state with skills_metadata populated.
        """
        self._copy_builtin_skills_to_directory()

        print(  # noqa: T201
            list_skills(
                project_skills_dir=self.project_skills_dir,
                builtin_skills_dir=self.builtin_skills_dir,
                backend=self.backend,
            )
        )
        return SkillsStateUpdate(
            skills_metadata=list_skills(
                project_skills_dir=self.project_skills_dir,
                builtin_skills_dir=self.builtin_skills_dir,
                backend=self.backend,
            )
        )

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Inject skills documentation into the system prompt.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        # The state is guaranteed to be SkillsState due to state_schema
        state = cast("SkillsState", request.state)

        skills_metadata = list(
            filter(lambda skill: self.scope is None or skill.scope == self.scope, state.get("skills_metadata", []))
        )

        if not skills_metadata:
            return await handler(request)

        skills_section = SKILLS_SYSTEM_PROMPT.format(skills_list=self._format_skills_list(skills_metadata))

        system_prompt = request.system_prompt + "\n\n" + skills_section if request.system_prompt else skills_section

        return await handler(request.override(system_prompt=system_prompt))

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """
        Format skills metadata for display in system prompt.
        """
        lines: list[str] = []

        for skill in skills:
            lines.append(f"- **{skill.name}**: {skill.description}")
            lines.append(f"  → Read `{skill.path}` for full instructions")

        lines.append("")

        return "\n".join(lines)

    def _copy_builtin_skills_to_directory(self) -> None:
        """
        Copy builtin skills to the /skills/ directory..
        """
        for builtin_skill_dir in BUILTIN_SKILLS_DIR.iterdir():
            for root, _dirs, files in builtin_skill_dir.walk():
                for file in files:
                    source_path = Path(root) / Path(file)
                    dest_path = Path(self.builtin_skills_dir) / source_path.relative_to(BUILTIN_SKILLS_DIR)
                    self.backend.write(str(dest_path), source_path.read_text())
