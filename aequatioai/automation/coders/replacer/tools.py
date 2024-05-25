import logging
import textwrap

from automation.agents.tools import FunctionTool

logger = logging.getLogger(__name__)


class ReplacerTools:
    def project_path_replacer(self, project_path: str):
        """
        Extracts paths that belong to a project.
        """
        return project_path.replace("feedportal", "bkcf_onboarding")

    def get_tools(self):
        return [
            FunctionTool(
                name="project_path_replacer",
                description=textwrap.dedent(
                    """\
                    Use this as the primary tool to replace project paths found in "Replacement snippet" with new ones.
                    - Ignore external paths, don't replace theme.
                    - If multiple replacements needed, call this function multiple times.
                    """
                ),
                parameters=[
                    {
                        "name": "project_path",
                        "type": "string",
                        "description": "List of paths referenced in the unified diff.",
                    }
                ],
                fn=self.project_path_replacer,
                required=["project_path"],
            )
        ]
