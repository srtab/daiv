import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sessions.executor.lock import NoLock
from sessions.executor.spec import RunOutcome

from automation.agent import ThinkingLevel
from codebase.base import GitPlatform, Scope
from evals import swebench

ITEM = {
    "instance_id": "owner__repo-1",
    "repo": "owner/repo",
    "base_commit": "abc123",
    "problem_statement": "Parsing an empty file crashes.",
    "hints_text": "",
}
SECOND = ITEM | {"instance_id": "owner__repo-2"}


def _outcome(**values) -> RunOutcome:
    return RunOutcome(agent_result={}, response_text="done", snapshot=SimpleNamespace(values=values))


async def _main(tmp_path, execute, items=(ITEM,)) -> list[dict]:
    with (
        patch.object(swebench, "load_dataset", return_value=list(items)),
        patch.object(swebench, "execute_run", execute),
    ):
        await swebench.main("dataset", "test", str(tmp_path / "predictions.json"), ["model-a", "model-b"])
    return _written(tmp_path)


def _written(tmp_path) -> list[dict]:
    return json.loads((tmp_path / "predictions.json").read_text())


async def test_each_instance_runs_as_a_one_shot_run_on_the_exact_model_chain(tmp_path):
    execute = AsyncMock(return_value=_outcome(model_patch="diff"))

    await _main(tmp_path, execute)

    spec = execute.await_args.args[0]
    assert (spec.thread_id, spec.lock, spec.trigger) == (None, NoLock(), "eval")
    assert (spec.repo_id, spec.ref, spec.scope) == ("owner/repo", "abc123", Scope.GLOBAL)
    assert (spec.model_names, spec.agent_thinking_level) == (("model-a", "model-b"), ThinkingLevel.HIGH)
    assert spec.context_options == {"offline": True, "git_platform": GitPlatform.SWE, "repo_host": "github.com"}
    assert spec.agent_options == {
        "auto_commit_changes": False,
        "capture_patch": True,
        "web_search_enabled": False,
        "web_fetch_enabled": False,
    }
    assert spec.extra_metadata == {"instance_id": "owner__repo-1"}
    [message] = spec.input_messages
    assert "Parsing an empty file crashes." in message.content
    assert "## Hints" not in message.content


async def test_a_finished_run_predicts_the_patch_it_captured(tmp_path):
    predictions = await _main(tmp_path, AsyncMock(return_value=_outcome(model_patch="diff --git a/x b/x")))

    assert predictions == [
        {"model_patch": "diff --git a/x b/x", "model_name_or_path": "model-a, model-b", "instance_id": "owner__repo-1"}
    ]


async def test_a_failed_run_predicts_an_empty_patch_and_the_eval_goes_on(tmp_path, capsys):
    execute = AsyncMock(side_effect=[RuntimeError("clone failed"), _outcome(model_patch="diff")])

    predictions = await _main(tmp_path, execute, items=(ITEM, SECOND))

    assert [(p["instance_id"], p["model_patch"]) for p in predictions] == [
        ("owner__repo-1", ""),
        ("owner__repo-2", "diff"),
    ]
    assert "[owner__repo-1] run failed:" in capsys.readouterr().err


@pytest.mark.parametrize("snapshot", [SimpleNamespace(values={}), None], ids=["no-patch", "no-snapshot"])
async def test_a_finished_run_without_a_captured_patch_ends_the_eval(tmp_path, snapshot):
    """A finished run with no patch means the capture wiring drifted: fail loud, not a file of empty patches."""
    outcome = RunOutcome(agent_result={}, response_text="done", snapshot=snapshot)

    with pytest.raises(KeyError, match="model_patch"):
        await _main(tmp_path, AsyncMock(return_value=outcome))

    assert _written(tmp_path) == []


async def test_a_dirty_workspace_is_reported_next_to_the_run(tmp_path, capsys):
    await _main(tmp_path, AsyncMock(return_value=_outcome(model_patch="diff", pre_run_dirty_files=["setup.cfg"])))

    assert "[owner__repo-1] WARNING: workspace was dirty before the run" in capsys.readouterr().err


async def test_an_eval_stopped_mid_instance_still_writes_its_predictions(tmp_path):
    """Ctrl-C under ``asyncio.run`` cancels ``main``: the finished instances and the interrupted one are kept."""
    execute = AsyncMock(side_effect=[_outcome(model_patch="diff"), asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await _main(tmp_path, execute, items=(ITEM, SECOND))

    assert [(p["instance_id"], p["model_patch"]) for p in _written(tmp_path)] == [
        ("owner__repo-1", "diff"),
        ("owner__repo-2", ""),
    ]
