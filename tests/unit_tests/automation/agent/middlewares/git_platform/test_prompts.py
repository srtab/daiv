from __future__ import annotations

from automation.agent.middlewares.git_platform.prompts import GITHUB_TOOL_DESCRIPTION, GITLAB_TOOL_DESCRIPTION


async def test_tool_descriptions_document_output_to_file():
    for desc in (GITLAB_TOOL_DESCRIPTION, GITHUB_TOOL_DESCRIPTION):
        assert "output_to_file" in desc
        assert "read_file" in desc  # how to consume the saved file
    assert "--output json" in GITLAB_TOOL_DESCRIPTION  # gitlab forces JSON when writing to file
    assert "--json" in GITHUB_TOOL_DESCRIPTION  # gh opts into JSON via its own flag


def test_tool_descriptions_document_project_job_trace_raw_text():
    """GITLAB_TOOL_DESCRIPTION must mention project-job trace together with raw log text."""
    assert "project-job trace" in GITLAB_TOOL_DESCRIPTION
    assert "raw log text" in GITLAB_TOOL_DESCRIPTION
