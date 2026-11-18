import annotationlib
import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.services import build_sandbox_spec
from sessions.executor.lock import NoLock
from sessions.executor.spec import RunOutcome

from automation.agent.base import ThinkingLevel
from automation.agent.context import set_runtime_ctx
from automation.agent.graph import create_daiv_agent
from automation.agent.validators import AgentOverrideError
from codebase.base import Scope
from evals import swebench
from evals.swe_client import SWERepoClient

ITEM = {
    "instance_id": "owner__repo-1",
    "repo": "owner/repo",
    "base_commit": "abc123",
    "problem_statement": "Parsing an empty file crashes.",
    "hints_text": "",
}
SECOND = ITEM | {"instance_id": "owner__repo-2"}
THIRD = ITEM | {"instance_id": "owner__repo-3"}


def _outcome(**values) -> RunOutcome:
    return RunOutcome(agent_result={}, response_text="done", snapshot=SimpleNamespace(values=values))


@asynccontextmanager
async def _task_env(item):
    yield f"env-{item['instance_id']}"


async def _main(
    tmp_path, execute, items=(ITEM,), validate=None, models=("model-a", "model-b"), **options
) -> list[dict]:
    with (
        patch("datasets.load_dataset", return_value=list(items)),
        patch.object(swebench, "execute_run", execute),
        patch.object(swebench, "validate_agent_override", validate or MagicMock()),
        patch.object(swebench, "task_sandbox_env", _task_env),
    ):
        await swebench.main("dataset", "test", str(tmp_path / "predictions.json"), list(models), **options)
    return _written(tmp_path)


def _written(tmp_path) -> list[dict]:
    return json.loads((tmp_path / "predictions.json").read_text())


def _attempts(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "predictions.usage.jsonl").read_text().splitlines()]


def _patched(*instance_ids: str) -> list[tuple[str, str]]:
    return [(instance_id, "diff") for instance_id in instance_ids]


def _ran(execute: AsyncMock) -> list[str]:
    return [call.args[0].extra_metadata["instance_id"] for call in execute.await_args_list]


async def test_each_instance_runs_as_a_one_shot_run_on_the_exact_model_chain(tmp_path):
    execute = AsyncMock(return_value=_outcome(model_patch="diff"))

    await _main(tmp_path, execute)

    spec = execute.await_args.args[0]
    assert (spec.thread_id, spec.lock, spec.trigger) == (None, NoLock(), "eval")
    assert (spec.repo_id, spec.ref, spec.scope) == ("owner/repo", "abc123", Scope.GLOBAL)
    assert (spec.model_names, spec.agent_thinking_level) == (("model-a", "model-b"), ThinkingLevel.HIGH)
    assert spec.context_options["offline"] is True
    client = spec.context_options["repo_client"]
    assert isinstance(client, SWERepoClient)
    assert client.repo_host == "github.com"
    assert spec.agent_options == {"auto_commit_changes": False, "capture_patch": True}
    assert (spec.web_search_enabled, spec.web_fetch_enabled) == (False, False)
    assert spec.extra_metadata == {"instance_id": "owner__repo-1"}
    assert spec.sandbox_env_id == "env-owner__repo-1"
    [message] = spec.input_messages
    assert "Parsing an empty file crashes." in message.content
    assert "## Hints" not in message.content


async def test_a_finished_run_predicts_the_patch_it_captured(tmp_path):
    predictions = await _main(tmp_path, AsyncMock(return_value=_outcome(model_patch="diff --git a/x b/x")))

    assert predictions == [
        {"model_patch": "diff --git a/x b/x", "model_name_or_path": "model-a, model-b", "instance_id": "owner__repo-1"}
    ]


async def test_a_finished_run_keeps_its_conversation_and_a_failed_one_writes_none(tmp_path):
    messages = [HumanMessage("Parsing an empty file crashes."), AIMessage("Fixed the empty-input branch.")]
    execute = AsyncMock(side_effect=[_outcome(model_patch="diff", messages=messages), RuntimeError("clone failed")])

    await _main(tmp_path, execute, items=(ITEM, SECOND))

    transcripts = tmp_path / "predictions.transcripts"
    assert [path.name for path in transcripts.iterdir()] == ["owner__repo-1.json"]
    transcript = json.loads((transcripts / "owner__repo-1.json").read_text())
    assert [message["role"] for message in transcript["messages"]] == ["user", "assistant"]
    assert transcript["metadata"] == {
        "instance_id": "owner__repo-1",
        "model": "model-a, model-b",
        "thinking_level": "high",
    }


async def test_a_model_the_site_cant_run_stops_the_eval_before_any_instance(tmp_path):
    execute = AsyncMock()
    validate = MagicMock(side_effect=AgentOverrideError("Unknown/Unsupported provider for model model-a"))

    with pytest.raises(AgentOverrideError):
        await _main(tmp_path, execute, validate=validate)

    validate.assert_called_once_with("model-a", "high")
    execute.assert_not_awaited()


async def test_a_failed_run_predicts_an_empty_patch_and_the_eval_goes_on(tmp_path, capsys):
    execute = AsyncMock(side_effect=[RuntimeError("clone failed"), _outcome(model_patch="diff")])

    predictions = await _main(tmp_path, execute, items=(ITEM, SECOND))

    assert [(p["instance_id"], p["model_patch"]) for p in predictions] == [
        ("owner__repo-1", ""),
        ("owner__repo-2", "diff"),
    ]
    err = capsys.readouterr().err
    assert "[owner__repo-1] run failed:" in err
    assert "1/2 instances failed: owner__repo-1" in err


async def test_an_eval_where_every_instance_failed_exits_non_zero(tmp_path):
    """The same setup error on every instance is a broken eval, not a batch of empty predictions."""
    execute = AsyncMock(side_effect=RuntimeError("sandbox unavailable"))

    with pytest.raises(SystemExit) as exit_info:
        await _main(tmp_path, execute, items=(ITEM, SECOND))

    assert exit_info.value.code == 1
    assert [p["model_patch"] for p in _written(tmp_path)] == ["", ""]


@pytest.mark.parametrize(
    ("snapshot", "cause"),
    [(SimpleNamespace(values={}), "without a model_patch"), (None, "checkpoint could not be read")],
    ids=["no-patch", "no-snapshot"],
)
async def test_a_finished_run_without_a_captured_patch_ends_the_eval(tmp_path, snapshot, cause):
    """A finished run with no patch to read fails loud, naming the instance, instead of predicting ''."""
    outcome = RunOutcome(agent_result={}, response_text="done", snapshot=snapshot)

    with pytest.raises(RuntimeError, match=rf"\[owner__repo-1\].*{cause}"):
        await _main(tmp_path, AsyncMock(return_value=outcome))

    assert _written(tmp_path) == []


async def test_a_dirty_workspace_is_reported_next_to_the_run(tmp_path, capsys):
    await _main(tmp_path, AsyncMock(return_value=_outcome(model_patch="diff", pre_run_dirty_files=["setup.cfg"])))

    assert "[owner__repo-1] WARNING: workspace was dirty before the run" in capsys.readouterr().err


async def test_an_eval_stopped_mid_instance_keeps_the_finished_predictions_and_reruns_the_interrupted_one(
    tmp_path, capsys
):
    """Ctrl-C under ``asyncio.run`` cancels ``main``: an interrupted task is not a result, so the rerun runs it."""
    execute = AsyncMock(side_effect=[_outcome(model_patch="diff"), asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await _main(tmp_path, execute, items=(ITEM, SECOND))

    assert [(p["instance_id"], p["model_patch"]) for p in _written(tmp_path)] == _patched("owner__repo-1")
    assert [a["status"] for a in _attempts(tmp_path)] == ["finished", "interrupted"]
    assert "[owner__repo-2] interrupted" in capsys.readouterr().err

    rerun = AsyncMock(return_value=_outcome(model_patch="diff"))
    predictions = await _main(tmp_path, rerun, items=(ITEM, SECOND))

    assert _ran(rerun) == ["owner__repo-2"]
    assert [(p["instance_id"], p["model_patch"]) for p in predictions] == _patched("owner__repo-1", "owner__repo-2")


async def test_a_cleanup_error_while_ctrl_c_unwinds_a_run_still_stops_the_eval(tmp_path):
    started = asyncio.Event()
    specs = []

    async def execute(spec):
        specs.append(spec)
        if len(specs) > 1:
            return _outcome(model_patch="diff")
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise OSError("clone cleanup failed") from None

    eval_task = asyncio.create_task(_main(tmp_path, execute, items=(ITEM, SECOND)))
    async with asyncio.timeout(5):
        await started.wait()
    eval_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await eval_task

    assert len(specs) == 1
    assert _written(tmp_path) == []


def test_the_options_an_instance_runs_with_are_ones_the_clone_and_the_agent_accept():
    spec = swebench._run_spec(ITEM, ["model-a"], "high", "env")

    agent_sig = inspect.signature(create_daiv_agent, annotation_format=annotationlib.Format.FORWARDREF)
    agent_sig.bind(settings=None, ctx=None, checkpointer=None, workspace=None, **spec.agent_options)

    ctx_sig = inspect.signature(set_runtime_ctx, annotation_format=annotationlib.Format.FORWARDREF)
    ctx_sig.bind(spec.repo_id, scope=spec.scope, sandbox_spec=None, **spec.context_options)


def test_each_instance_clones_through_a_client_of_its_own():
    first = swebench._run_spec(ITEM, ["model-a"], "high", "env").context_options["repo_client"]
    second = swebench._run_spec(SECOND, ["model-a"], "high", "env").context_options["repo_client"]

    assert first is not second


@pytest.mark.parametrize(
    ("named", "image"),
    [
        ({"image": "swebench/sweb.eval.x86_64.a_1776_b-1:latest"}, "swebench/sweb.eval.x86_64.a_1776_b-1:latest"),
        ({"docker_image": "swerebench/sweb.eval.x86_64.c_1776_d-2:v1"}, "swerebench/sweb.eval.x86_64.c_1776_d-2:v1"),
        ({"instance_id": "Owner__Repo-1"}, "swebench/sweb.eval.x86_64.owner_1776_repo-1:latest"),
    ],
    ids=["verified", "swe-rebench", "mini"],
)
def test_a_task_runs_in_the_image_its_dataset_names_or_the_harness_would_build(named, image):
    assert swebench.task_image(ITEM | named) == image


@pytest.mark.django_db(transaction=True)
async def test_a_task_runs_with_no_network_on_its_image_and_the_environment_is_removed_after():
    item = ITEM | {"image": "swebench/sweb.eval.x86_64.owner_1776_repo-1:latest"}

    async with swebench.task_sandbox_env(item) as env_id:
        sandbox = await build_sandbox_spec(env_id)

    assert sandbox.base_image == "swebench/sweb.eval.x86_64.owner_1776_repo-1:latest"
    assert sandbox.egress is None
    assert sandbox.env_vars["PYTHONPATH"] == "/workspace/repo"
    assert sandbox.env_vars["PATH"].startswith("/opt/miniconda3/envs/testbed/bin:")
    assert not await SandboxEnvironment.objects.filter(pk=env_id).aexists()


@pytest.mark.django_db(transaction=True)
async def test_two_evals_on_the_same_task_keep_their_own_environment_and_both_are_removed_even_when_a_run_raises():
    with pytest.raises(RuntimeError):
        async with swebench.task_sandbox_env(ITEM) as first:
            async with swebench.task_sandbox_env(ITEM) as second:
                pass
            assert (await build_sandbox_spec(first)).base_image == swebench.task_image(ITEM)
            raise RuntimeError("sandbox unavailable")

    assert first != second
    assert not await SandboxEnvironment.objects.filter(name__startswith="swebench ").aexists()


def test_a_message_with_hints_includes_the_hints_section():
    item = ITEM | {"hints_text": "Check the parser's empty-input branch."}

    message = swebench._human_message(item)

    assert "## Hints" in message
    assert "Check the parser's empty-input branch." in message


async def test_the_thinking_level_asked_for_is_validated_and_run_with(tmp_path):
    execute = AsyncMock(return_value=_outcome(model_patch="diff"))
    validate = MagicMock()

    await _main(tmp_path, execute, validate=validate, thinking_level="low")

    assert validate.call_args_list == [(("model-a", "low"),), (("model-b", "low"),)]
    assert execute.await_args.args[0].agent_thinking_level == "low"


async def test_named_tasks_run_exactly_and_in_dataset_order(tmp_path):
    execute = AsyncMock(return_value=_outcome(model_patch="diff"))

    await _main(tmp_path, execute, items=(ITEM, SECOND, THIRD), instance_ids=["owner__repo-3", "owner__repo-1"])

    assert _ran(execute) == ["owner__repo-1", "owner__repo-3"]


async def test_a_named_task_the_dataset_lacks_stops_the_eval_before_any_run(tmp_path):
    execute = AsyncMock()

    with pytest.raises(SystemExit, match="owner__repo-9"):
        await _main(tmp_path, execute, instance_ids=["owner__repo-1", "owner__repo-9"])

    execute.assert_not_awaited()


def test_a_sample_is_reproducible_spans_the_dataset_and_a_larger_one_extends_it():
    rows = [ITEM | {"instance_id": f"owner__repo-{index}"} for index in range(50)]

    sample = swebench.select_items(rows, None, 5, seed=0)

    assert sample == swebench.select_items(rows, None, 5, seed=0)
    assert sample != rows[:5]
    assert swebench.select_items(rows, None, 20, seed=0)[:5] == sample
    assert swebench.select_items(rows, None, 5, seed=1) != sample
    assert sorted(row["instance_id"] for row in swebench.select_items(rows, None, None, seed=0)) == sorted(
        row["instance_id"] for row in rows
    )


async def test_each_attempt_logs_its_time_tokens_and_cost_even_when_the_run_raises(tmp_path, capsys):
    """The usage log is the eval's bill: a run that raises after calling the model has spent money too."""
    reply = AIMessage(
        content="done",
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        response_metadata={"model_name": "model-a", "provider_cost_usd": 0.25},
    )

    async def execute(spec):
        await GenericFakeChatModel(messages=iter([reply])).ainvoke("fix it")
        if spec.extra_metadata["instance_id"] == "owner__repo-1":
            raise RuntimeError("sandbox lost")
        return _outcome(model_patch="diff")

    await _main(tmp_path, execute, items=(ITEM, SECOND))

    attempts = _attempts(tmp_path)
    assert [(a["instance_id"], a["status"]) for a in attempts] == [
        ("owner__repo-1", "failed"),
        ("owner__repo-2", "finished"),
    ]
    for attempt in attempts:
        assert (attempt["input_tokens"], attempt["output_tokens"], attempt["cost_usd"]) == (100, 20, 0.25)
        assert (attempt["model_name_or_path"], attempt["thinking_level"]) == ("model-a, model-b", "high")
        assert attempt["seconds"] >= 0
    assert "2 attempt(s) logged, $0.50; a finished task averaged $0.25" in capsys.readouterr().err


async def test_a_rerun_skips_the_predicted_tasks_and_keeps_their_predictions(tmp_path):
    await _main(tmp_path, AsyncMock(return_value=_outcome(model_patch="diff")))
    rerun = AsyncMock(return_value=_outcome(model_patch="diff"))

    predictions = await _main(tmp_path, rerun, items=(ITEM, SECOND))

    assert _ran(rerun) == ["owner__repo-2"]
    assert [(p["instance_id"], p["model_patch"]) for p in predictions] == _patched("owner__repo-1", "owner__repo-2")


@pytest.mark.parametrize(
    "rerun_with",
    [{"models": ("model-c",)}, {"thinking_level": "low"}],
    ids=["another-model-chain", "another-thinking-level"],
)
async def test_a_rerun_with_another_setup_is_refused_before_any_run(tmp_path, rerun_with):
    await _main(tmp_path, AsyncMock(return_value=_outcome(model_patch="diff")))
    rerun = AsyncMock()

    with pytest.raises(SystemExit, match="another --output-path"):
        await _main(tmp_path, rerun, items=(ITEM, SECOND), **rerun_with)

    rerun.assert_not_awaited()


async def test_a_failed_task_stays_unresolved_on_rerun_until_its_retry_is_asked_for(tmp_path):
    """Rerunning a failure by default would grade the best of several attempts."""
    first = AsyncMock(side_effect=[RuntimeError("clone failed"), _outcome(model_patch="diff")])
    await _main(tmp_path, first, items=(ITEM, SECOND))
    rerun = AsyncMock(return_value=_outcome(model_patch="diff"))

    await _main(tmp_path, rerun, items=(ITEM, SECOND))
    rerun.assert_not_awaited()

    predictions = await _main(tmp_path, rerun, items=(ITEM, SECOND), retry_failed=True)

    assert _ran(rerun) == ["owner__repo-1"]
    patches = sorted((p["instance_id"], p["model_patch"]) for p in predictions)
    assert patches == _patched("owner__repo-1", "owner__repo-2")
