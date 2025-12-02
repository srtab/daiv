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

import shutil
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langgraph.runtime import Runtime  # noqa: TC002

from automation.agents.tools.navigation import READ_TOOL_NAME

from .load import BUILTIN_SKILLS_DIR, SkillMetadata, list_skills

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


class SkillsState(AgentState):
    """State for the skills middleware."""

    skills_metadata: NotRequired[list[SkillMetadata]]
    """List of loaded skill metadata (name, description, path)."""


class SkillsStateUpdate(TypedDict):
    """State update for the skills middleware."""

    skills_metadata: list[SkillMetadata]
    """List of loaded skill metadata (name, description, path)."""


# Skills System Documentation
SKILLS_SYSTEM_PROMPT = f"""\
## Skills System

You have access to a skills library that provides specialized capabilities and domain knowledge.

**Skills:**

{{skills_list}}

**How to Use Skills (Progressive Disclosure):**

Skills follow a **progressive disclosure** pattern - you know they exist (name + description above), but you only read the full instructions when needed:

1. **Recognize when a skill applies**: Check if the user's task matches any skill's description
2. **Read the skill's full instructions**: The skill list above shows the exact path to use with `{READ_TOOL_NAME}` tool
3. **Follow the skill's instructions**: SKILL.md contains step-by-step workflows, best practices, and examples
4. **Access supporting files**: Skills may include Python scripts, configs, or reference docs - use relative paths for skills

**When to Use Skills:**
- When the user's request matches a skill's domain (e.g., "research X" → web-research skill)
- When you need specialized knowledge or structured workflows
- When a skill provides proven patterns for complex tasks

**Skills are Self-Documenting:**
- Each SKILL.md tells you exactly what the skill does and how to use it
- The skill list above shows the full path for each skill's SKILL.md file

**Executing Skill Scripts:**
Skills may contain Python scripts or other executable files. Always use relative paths for skills.

**Example Workflow:**
<example>
User: "Can you research the latest developments in quantum computing?"

1. Check available skills above → See "web-research" skill with its full path
2. Read the skill using the path shown in the list
3. Follow the skill's research workflow (search → organize → synthesize)
4. Use any helper scripts with relative paths for skills
</example>

Remember: Skills are tools to make you more capable and consistent. When in doubt, check if a skill exists for the task!
"""  # noqa: E501


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

    def __init__(self, *, repo_dir: Path, scope: Literal["issue", "merge_request"] | None = None) -> None:
        """
        Initialize the skills middleware.

        Args:
            repo_dir: Path to the repository directory.
            scope: Scope of the skills to load. If None, all skills will be loaded.
        """
        self.repo_dir = repo_dir.resolve()
        self.skills_dir = repo_dir / ".daiv/skills"
        self.scope = scope
        self.system_prompt_template = SKILLS_SYSTEM_PROMPT

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
        if not self.skills_dir.exists():
            self.skills_dir.mkdir(parents=True, exist_ok=True)

        self._copy_builtin_skills_to_project()

        return SkillsStateUpdate(skills_metadata=list_skills(skills_dir=self.skills_dir))

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

        skills_section = self.system_prompt_template.format(skills_list=self._format_skills_list(skills_metadata))

        system_prompt = request.system_prompt + "\n\n" + skills_section if request.system_prompt else skills_section

        return await handler(request.override(system_prompt=system_prompt))

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """
        Format skills metadata for display in system prompt.
        """
        lines: list[str] = []

        for skill in skills:
            lines.append(f"- **{skill.name}**: {skill.description}")
            lines.append(f"  → Read `{skill.path.as_posix()}` for full instructions")

        lines.append("")

        return "\n".join(lines)

    def _copy_builtin_skills_to_project(self) -> None:
        """
        Copy builtin skills to the project skills directory if they don't exist.

        This is done to allow the agent to find builtin skills as if they were project skills, even if the
        project skills directory is not set up. The copied skills folder will include a .gitignore
        file to prevent them from being committed to the repository.
        """
        for builtin_skill_dir in BUILTIN_SKILLS_DIR.iterdir():
            builtin_skill_path = self.skills_dir / builtin_skill_dir.name
            if not builtin_skill_path.exists():
                shutil.copytree(builtin_skill_dir, builtin_skill_path)
                self._create_gitignore_file(builtin_skill_path)

    def _create_gitignore_file(self, skill_path: Path) -> None:
        """
        Create a gitignore file in the builtin skills directory to prevent from being committed.
        """
        (skill_path / ".gitignore").write_text("*")
