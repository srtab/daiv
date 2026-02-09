import json
from pathlib import Path

import pytest
from langsmith import testing as t

from automation.agent.constants import ModelName
from automation.agent.pr_describer.graph import create_pr_describer_agent
from codebase.base import GitPlatform, Scope
from codebase.context import set_runtime_ctx

from .evaluators import correctness_evaluator

DATA_DIR = Path(__file__).parent / "data" / "pr_describer"


def _read_text(rel_path: str) -> str:
    return (DATA_DIR / rel_path).read_text()


def load_cases() -> list[pytest.param]:
    for line in _read_text("cases.jsonl").splitlines():
        if not line.strip():
            continue

        row = json.loads(line)

        inputs = dict(row["inputs"])
        if "diff_path" in inputs:
            inputs["diff"] = _read_text(inputs.pop("diff_path"))
        if "context_file_content_path" in inputs:
            inputs["context_file_content"] = _read_text(inputs.pop("context_file_content_path"))
        if "extra_context_path" in inputs:
            inputs["extra_context"] = _read_text(inputs.pop("extra_context_path"))

        reference_outputs = row.get("reference_outputs")
        case_id = row.get("id", "case")

        yield pytest.param(inputs, reference_outputs, id=case_id)


@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize("inputs,reference_outputs", load_cases())
async def test_pr_describer(inputs, reference_outputs):
    t.log_inputs(inputs)
    t.log_reference_outputs(reference_outputs)

    async with set_runtime_ctx(
        "srtab/daiv", scope=Scope.GLOBAL, ref="main", offline=True, git_platform=GitPlatform.GITLAB
    ) as ctx:
        agent_path = Path(ctx.repo.working_dir)
        if "context_file_content" in inputs:
            (agent_path / ctx.config.context_file_name).write_text(inputs.pop("context_file_content"))
        else:
            (agent_path / ctx.config.context_file_name).unlink()
        pr_describer = create_pr_describer_agent(model=ModelName.CLAUDE_HAIKU_4_5, ctx=ctx)
        outputs = await pr_describer.ainvoke(inputs)

    assert "structured_response" in outputs, outputs

    t.log_outputs(outputs["structured_response"].model_dump(mode="json"))

    result = correctness_evaluator(
        inputs=inputs,
        outputs=outputs["structured_response"].model_dump(mode="json"),
        reference_outputs=reference_outputs,
    )
    assert result["score"] is True, result["comment"]
