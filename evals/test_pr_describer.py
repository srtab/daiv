import pytest
from langsmith import testing as t
from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.constants import ModelName
from automation.agents.pr_describer import PullRequestDescriberAgent

evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.GPT_5_MINI, thinking_level=ThinkingLevel.MEDIUM),
)

file_changes = []  # TODO: Add file changes


@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize(
    "branch_name_convention,extra_context,reference_outputs",
    [
        (
            None,
            None,
            """{
  "title": "Add pull request describer agent to describe PR changes",
  "branch": "feature/pr-describer-agent",
  "description": "This change introduces a new pull request describer agent designed to describe changes in a pull request. It adds a new Python module `automation/agents/pr_describer/agent.py` which defines the `PullRequestDescriberAgent` class inheriting from `BaseAgent`. The agent compiles a runnable that uses a chat prompt template combined with a language model configured to output structured `PullRequestMetadata`. Additionally, the `README.md` file is updated to document the new pull request describer agent and its purpose.",
  "summary": [
    "Add new agent `PullRequestDescriberAgent` in `automation/agents/pr_describer/agent.py` to describe pull request changes",
    "Update `README.md` to document the pull request describer agent and its functionality"
  ],
  "commit_message": "Add pull request describer agent and update README to document it"
}""",  # noqa: E501
        ),
        (
            "Use 'feat/', 'fix/', or 'chore/' prefixes.",
            None,
            """{
  "title": "Add pull request describer agent to describe PR changes",
  "branch": "feat/pr-describer-agent",
  "description": "This change introduces a new pull request describer agent designed to describe changes in a pull request. It includes the creation of a new agent class `PullRequestDescriberAgent` in `automation/agents/pr_describer/agent.py` that compiles a runnable prompt using a chat prompt template and a model configured with structured output for `PullRequestMetadata`. Additionally, the `README.md` is updated to document the new pull request describer agent and its purpose.",
  "summary": [
    "Add new agent class `PullRequestDescriberAgent` in `automation/agents/pr_describer/agent.py`",
    "Add method to compile a runnable prompt with structured output for pull request metadata",
    "Update `README.md` to document the pull request describer agent and its functionality"
  ],
  "commit_message": "Add pull request describer agent and update README to document it"
}""",  # noqa: E501
        ),
        (
            None,
            "The changes are related to the issue #123.",
            """{
  "title": "Add pull request describer agent to describe PR changes",
  "branch": "feature/pr-describer-agent",
  "description": "This change introduces a new agent called `PullRequestDescriberAgent` designed to describe changes in a pull request. The agent is implemented in a new file `automation/agents/pr_describer/agent.py` and uses a chat prompt template combined with a language model to generate structured output conforming to the `PullRequestMetadata` schema. The README is updated to document the addition of this new PR describer agent. This work is related to issue #123.",
  "summary": [
    "Add new agent `PullRequestDescriberAgent` in `automation/agents/pr_describer/agent.py` to describe pull request changes",
    "Update `README.md` to document the new pull request describer agent"
  ],
  "commit_message": "Add pull request describer agent and update README to document it"
}""",  # noqa: E501
        ),
        (
            "Use 'feat/', 'fix/', or 'chore/' prefixes.",
            "The changes are related to the issue #123.",
            """{
  "title": "Add pull request describer agent to describe PR changes",
  "branch": "feat/pr-describer-agent",
  "description": "This change introduces a new pull request describer agent designed to describe changes in a pull request. It includes the creation of a new agent class `PullRequestDescriberAgent` in `automation/agents/pr_describer/agent.py` that compiles a runnable prompt using a chat prompt template and a model configured with structured output for `PullRequestMetadata`. The README is updated to document the addition of this new agent. This addresses issue #123.",
  "summary": [
    "Add `PullRequestDescriberAgent` class in `automation/agents/pr_describer/agent.py` to describe pull request changes",
    "Update `README.md` to document the new pull request describer agent"
  ],
  "commit_message": "Add pull request describer agent to describe PR changes"
}""",  # noqa: E501
        ),
    ],
)
async def test_pr_describer_correctness(branch_name_convention, extra_context, reference_outputs):
    t.log_reference_outputs(reference_outputs)

    pr_describer = await PullRequestDescriberAgent.get_runnable()

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
