import pytest
from langsmith import testing as t
from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.constants import ModelName
from automation.agents.pr_describer import PullRequestDescriberAgent
from codebase.base import FileChange, FileChangeAction

evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.O4_MINI, thinking_level=ThinkingLevel.MEDIUM),
)

file_changes = [
    FileChange(
        action=FileChangeAction.UPDATE,
        file_path="codebase/managers/issue_addressor.py",
        commit_messages=["Update issue addressor"],
    ),
    FileChange(
        action=FileChangeAction.CREATE,
        file_path="automation/agents/pr_describer/agent.py",
        commit_messages=["Add pr describer agent"],
    ),
]


@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize(
    "branch_name_convention,extra_context,reference_outputs",
    [
        (
            None,
            None,
            {
                "title": "Update issue addressor and add PR describer agent",
                "branch": "update-issue-addressor-add-pr-describer-agent",
                "description": "This change updates the issue addressor component and adds a new PR describer agent. The update to the issue addressor likely involves modifications to how issues are referenced or handled. The addition of the PR describer agent introduces functionality to describe pull requests, potentially improving PR metadata or automation.",  # noqa: E501
                "summary": ["Update issue addressor", "Add PR describer agent"],
                "commit_message": "Update issue addressor and add PR describer agent",
            },
        ),
        (
            "Use 'feat/', 'fix/', or 'chore/' prefixes.",
            None,
            {
                "title": "Update issue addressor and add PR describer agent",
                "branch": "feat/update-issue-addressor-add-pr-describer-agent",
                "description": "This update modifies the issue addressor component and introduces a new PR describer agent. The changes enhance the handling and description of pull requests by adding functionality to describe PRs more effectively and updating the mechanism that addresses issues.",  # noqa: E501
                "summary": [
                    "Update issue addressor to improve issue handling",
                    "Add PR describer agent to enhance pull request descriptions",
                ],
                "commit_message": "Update issue addressor and add PR describer agent",
            },
        ),
        (
            None,
            "The changes are related to the issue #123.",
            {
                "title": "Update issue addressor and add PR describer agent",
                "branch": "update-issue-addressor-add-pr-describer-agent",
                "description": "This change updates the issue addressor component and adds a PR describer agent. The updates relate to issue #123. The modifications enhance how issues are addressed and introduce a new agent to describe pull requests, improving issue and PR handling processes.",  # noqa: E501
                "summary": ["Update issue addressor", "Add PR describer agent"],
                "commit_message": "Update issue addressor and add PR describer agent",
            },
        ),
        (
            "Use 'feat/', 'fix/', or 'chore/' prefixes.",
            "The changes are related to the issue #123.",
            {
                "title": "Update issue addressor and add PR describer agent",
                "branch": "feat/update-issue-addressor-add-pr-describer-agent",
                "description": "This update addresses issue #123 by implementing two main changes:\n\n- Updated the issue addressor component to improve its functionality.\n- Added a PR describer agent to enhance pull request descriptions.\n\nThese changes aim to streamline issue handling and improve the clarity of pull request descriptions.",  # noqa: E501
                "summary": ["Update issue addressor", "Add PR describer agent"],
                "commit_message": "Update issue addressor and add PR describer agent",
            },
        ),
    ],
)
async def test_pr_describer_correctness(branch_name_convention, extra_context, reference_outputs):
    t.log_reference_outputs(reference_outputs)

    pr_describer = await PullRequestDescriberAgent().agent

    inputs = {"changes": [change.model_dump() for change in file_changes]}

    if branch_name_convention:
        inputs["branch_name_convention"] = branch_name_convention
    if extra_context:
        inputs["extra_context"] = extra_context

    t.log_inputs(inputs)

    outputs = await pr_describer.ainvoke({
        "changes": file_changes,
        "branch_name_convention": branch_name_convention,
        "extra_context": extra_context,
    })

    t.log_outputs(outputs.model_dump(mode="json"))

    result = evaluator(inputs=inputs, outputs=outputs.model_dump_json(), reference_outputs=reference_outputs)
    assert result["score"] is True, result["comment"]
