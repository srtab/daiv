import json
from pathlib import Path

import pytest
from langsmith import testing as t

from automation.agents.constants import ModelName
from automation.agents.pr_describer import PullRequestDescriberAgent

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

    pr_describer = await PullRequestDescriberAgent.get_runnable(model=ModelName.GPT_4_1_MINI)
    outputs = await pr_describer.ainvoke(inputs)

    t.log_outputs(outputs.model_dump(mode="json"))

    result = correctness_evaluator(
        inputs=inputs, outputs=outputs.model_dump(mode="json"), reference_outputs=reference_outputs
    )
    assert result["score"] is True, result["comment"]
