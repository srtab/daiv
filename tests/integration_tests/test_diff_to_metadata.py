import json
from pathlib import Path

import pytest
from langsmith import testing as t

from automation.agent.diff_to_metadata.graph import create_diff_to_metadata_graph
from automation.agent.diff_to_metadata.prompts import sanitize_agent_report
from codebase.base import GitPlatform, Scope
from codebase.context import set_runtime_ctx

from .description_shape import shape_violations, validate_expectation
from .evaluators import get_correctness_evaluator
from .utils import FAST_MODEL_NAMES, require_provider_for_model

DATA_DIR = Path(__file__).parent / "data" / "diff_to_metadata"
TEST_SUITE = "DAIV: Diff to Metadata"


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
        # Fed through the publisher's own key and sanitizer, so a case exercises the real
        # composition — the framing text lives in the template both paths share.
        if agent_summary := inputs.pop("agent_summary", None):
            inputs["agent_report"] = sanitize_agent_report(agent_summary)

        reference_outputs = row.get("reference_outputs")
        case_id = row.get("id", "case")
        expect = row.get("expect", {})
        validate_expectation(expect)

        yield pytest.param(inputs, reference_outputs, expect, id=case_id)


@pytest.mark.diff_to_metadata
@pytest.mark.langsmith(test_suite_name=TEST_SUITE, output_keys=["reference_outputs"])
@pytest.mark.parametrize("model_name", FAST_MODEL_NAMES)
@pytest.mark.parametrize("inputs,reference_outputs,expect", load_cases())
async def test_diff_to_metadata(model_name, inputs, reference_outputs, expect):
    require_provider_for_model(model_name)

    t.log_inputs(inputs)
    t.log_reference_outputs(reference_outputs)

    async with set_runtime_ctx(
        "srtab/daiv", scope=Scope.GLOBAL, ref="main", offline=True, git_platform=GitPlatform.GITLAB
    ) as ctx:
        agent_path = Path(ctx.gitrepo.working_dir)
        if "context_file_content" in inputs:
            (agent_path / ctx.config.context_file_name).write_text(inputs.pop("context_file_content"))
        else:
            (agent_path / ctx.config.context_file_name).unlink()
        changes_metadata_graph = create_diff_to_metadata_graph(ctx=ctx, model_names=[model_name])
        outputs = await changes_metadata_graph.ainvoke(inputs)
        outputs = {
            "pr_metadata": outputs["pr_metadata"].model_dump(mode="json") if "pr_metadata" in outputs else None,
            "commit_message": outputs["commit_message"].model_dump(mode="json")
            if "commit_message" in outputs
            else None,
        }

    assert "pr_metadata" in outputs or "commit_message" in outputs, outputs

    t.log_outputs(outputs)

    # Shape first: the judge grades meaning against the reference output and has no opinion on
    # padding, so a description that says the right thing at four times the length passes it.
    # Unconditional on `expect`: some checks apply to every description, not only to opted-in ones.
    if outputs["pr_metadata"] is not None:
        description = outputs["pr_metadata"]["description"]
        assert description.strip(), "Empty description"
        violations = shape_violations(description, expect)
        assert not violations, "Description shape: " + "; ".join(violations) + f"\n\n---\n{description}"

    result = await get_correctness_evaluator()(inputs=inputs, outputs=outputs, reference_outputs=reference_outputs)
    assert result["score"] is True, result["comment"]
