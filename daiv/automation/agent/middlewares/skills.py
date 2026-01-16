from pathlib import Path
from typing import TYPE_CHECKING

from deepagents.middleware.skills import SkillsMiddleware as DeepAgentsSkillsMiddleware
from deepagents.middleware.skills import SkillsState, SkillsStateUpdate

from automation.agent.constants import BUILTIN_SKILLS_PATH, PROJECT_SKILLS_PATH

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

    from codebase.context import RuntimeCtx


SKILLS_SYSTEM_PROMPT = """\
## Skills System

You have access to a skills library that provides specialized capabilities and domain knowledge.

{skills_locations}

**Available Skills:**

{skills_list}

**How to Use Skills (Progressive Disclosure):**

Skills follow a **progressive disclosure** pattern - you see their name and description above, but only read full instructions when needed:

1. **Recognize when a skill applies**: Check if the user's task matches a skill's description
2. **Read the skill's full instructions**: Use the path shown in the skill list above
3. **Follow the skill's instructions**: SKILL.md contains step-by-step workflows, best practices, and examples
4. **Access supporting files**: Skills may include helper scripts, configs, or reference docs - use absolute paths to access them

**When to Use Skills:**
- User's request matches a skill's domain (e.g., "research X" -> web-research skill)
- You need specialized knowledge or structured workflows
- A skill provides proven patterns for complex tasks

**Executing Skill Scripts:**
Skills may contain Python scripts or other executable files. Always use absolute paths from the skill list to execute them.

**Example Workflow:**

User: "Can you research the latest developments in quantum computing?"

1. Check available skills -> See "web-research" skill with its path
2. Read the skill using the path shown
3. Follow the skill's research workflow (search -> organize -> synthesize)
4. Use any helper scripts with absolute paths to execute them

Remember: Skills make you more capable and consistent. When in doubt, check if a skill exists for the task!"""  # noqa: E501


class SkillsMiddleware(DeepAgentsSkillsMiddleware):
    """
    Rewrite the DeepAgentsSkillsMiddleware to copy the builtin skills to the project skills directory to make
    them available to the agent even if the project skills directory is not set up.
    """

    async def abefore_agent(
        self, state: SkillsState, runtime: Runtime[RuntimeCtx], config: RunnableConfig
    ) -> SkillsStateUpdate | None:
        """
        Copy builtin skills to the project skills directory to make them available to the agent.
        """
        if "skills_metadata" in state:
            return None

        await self._copy_builtin_skills(agent_path=Path(runtime.context.repo.working_dir))

        return await super().abefore_agent(state, runtime, config)

    async def _copy_builtin_skills(self, agent_path: Path):
        """
        Copy builtin skills to the project skills directory if they don't exist.

        This allows the agent to find built-in skills and execute scripts bundled with them as if they were project
        skills, even if the project skills directory is not set up. The copied skills folder includes a .gitignore file
        to prevent the skills from being committed to the repository.

        Users can override built-in skills by creating them with the same name in the project skills directory and
        committing them to the repository.
        """
        files_to_upload = []
        project_skills_path = Path(f"/{agent_path.name}/{PROJECT_SKILLS_PATH}")

        for builtin_skill_dir in BUILTIN_SKILLS_PATH.iterdir():
            if not builtin_skill_dir.is_dir() or builtin_skill_dir.name == "__pycache__":
                continue

            for root, _dirs, files in builtin_skill_dir.walk():
                for file in files:
                    source_path = Path(root) / Path(file)
                    dest_path = project_skills_path / source_path.relative_to(BUILTIN_SKILLS_PATH)
                    if not dest_path.exists():
                        files_to_upload.append((str(dest_path), source_path.read_text().encode("utf-8")))

            dest_path = project_skills_path / builtin_skill_dir.relative_to(BUILTIN_SKILLS_PATH)
            files_to_upload.append((str(dest_path / ".gitignore"), b"*"))

        for response in await self._backend.aupload_files(files_to_upload):
            if response.error:
                raise RuntimeError(f"Failed to upload builtin skill: {response.error}")
