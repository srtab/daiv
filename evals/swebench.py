"""Run DAIV on SWE-bench-format tasks and write predictions for the official SWE-bench harness.

Each task runs through ``execute_run`` in its own SWE-bench image with no network, so the agent finds the task's
dependencies installed and cannot fetch a later release or the upstream repository, which hold the fix. Its captured
patch becomes one row of the predictions file. A run that raises predicts an empty patch, so it is graded
unresolved; ``--retry-failed`` runs those tasks again. Every attempt, failed and interrupted ones included, appends
its time, tokens and cost to ``<predictions>.usage.jsonl``. A finished task's conversation goes to
``<predictions>.transcripts/<instance_id>.json``, in the shape the trace-analyzer skill reads. A task already in the
predictions file is skipped, so a rerun resumes.
"""

import argparse
import asyncio
import json
import random
import sys
import traceback
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from textwrap import dedent
from time import monotonic
from typing import TYPE_CHECKING

import django
from django.apps import apps

from langchain_core.messages import HumanMessage, convert_to_openai_messages
from langchain_core.tracers.context import register_configure_hook

# The first-party imports below define Django models at import time, so the app registry must be populated
# before them, once: under pytest a second setup re-applies the test LOGGING and disables existing loggers.
if not apps.ready:
    django.setup()

from sandbox_envs.models import SandboxEnvironment, Visibility  # noqa: E402
from sessions.executor.lock import NoLock  # noqa: E402
from sessions.executor.run import execute_run  # noqa: E402
from sessions.executor.spec import RunOutcome, RunSpec  # noqa: E402

from automation.agent.base import ThinkingLevel  # noqa: E402
from automation.agent.usage_tracking import CostAwareUsageMetadataCallbackHandler, build_usage_summary  # noqa: E402
from automation.agent.validators import validate_agent_override  # noqa: E402
from codebase.base import Scope  # noqa: E402
from core.constants import ModelName  # noqa: E402
from evals.swe_client import SWERepoClient  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator, Iterable

DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_THINKING_LEVEL = ThinkingLevel.HIGH.value
TASK_ENV_VARS = {
    "PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONPATH": "/workspace/repo",
    "LANG": "C.UTF-8",
}

_run_usage: ContextVar[CostAwareUsageMetadataCallbackHandler | None] = ContextVar("swebench_run_usage", default=None)
register_configure_hook(_run_usage, inheritable=True)


@contextmanager
def track_run_usage() -> Generator[CostAwareUsageMetadataCallbackHandler]:
    """One task's model usage, subagents included: ``execute_run`` tallies its own, but returns none when it raises."""
    handler = CostAwareUsageMetadataCallbackHandler()
    token = _run_usage.set(handler)
    try:
        yield handler
    finally:
        _run_usage.reset(token)


def select_items(
    rows: Iterable[dict], instance_ids: list[str] | None, num_samples: int | None, seed: int
) -> list[dict]:
    """The named tasks in dataset order, else the dataset shuffled by ``seed`` and cut to ``num_samples``.

    The shuffle does not depend on ``num_samples``, so with one seed a smaller sample is the start of a larger one.
    """
    rows = list(rows)
    if instance_ids:
        wanted = set(instance_ids)
        if missing := wanted - {row["instance_id"] for row in rows}:
            raise SystemExit(f"Not in the dataset: {', '.join(sorted(missing))}")
        return [row for row in rows if row["instance_id"] in wanted]
    random.Random(seed).shuffle(rows)  # noqa: S311
    return rows if num_samples is None else rows[:num_samples]


def load_predictions(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def write_predictions(path: Path, predictions: list[dict]) -> None:
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(predictions, indent=2))
    partial.replace(path)


def write_transcript(path: Path, messages: list, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript = {"messages": convert_to_openai_messages(messages), "metadata": metadata}
    path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1, default=str))


def load_attempts(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def failed_instance_ids(attempts: list[dict]) -> set[str]:
    """The tasks whose latest attempt raised."""
    latest = {attempt["instance_id"]: attempt["status"] for attempt in attempts}
    return {instance_id for instance_id, status in latest.items() if status == "failed"}


def check_resumable(
    predictions: list[dict], attempts: list[dict], model_name_or_path: str, thinking_level: str
) -> None:
    """A rerun only adds to an output that the same model chain and thinking level wrote."""
    models = {row["model_name_or_path"] for row in [*predictions, *attempts]}
    levels = {attempt["thinking_level"] for attempt in attempts}
    if models - {model_name_or_path} or levels - {thinking_level}:
        raise SystemExit(
            f"The output already holds runs of {', '.join(sorted(models))} at thinking level "
            f"{', '.join(sorted(levels)) or 'unknown'}: pass another --output-path for {model_name_or_path} "
            f"at {thinking_level}."
        )


def attempt_record(
    instance_id: str,
    status: str,
    model_name_or_path: str,
    thinking_level: str,
    usage: CostAwareUsageMetadataCallbackHandler,
    seconds: float,
) -> dict:
    """One attempt's line in the usage log; ``cost_usd`` is None when a model it called has no known price."""
    record = {
        "instance_id": instance_id,
        "status": status,
        "model_name_or_path": model_name_or_path,
        "thinking_level": thinking_level,
        "seconds": round(seconds, 1),
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "by_model": {},
    }
    if usage.usage_metadata:
        summary = build_usage_summary(usage)
        record |= {
            "input_tokens": summary.input_tokens,
            "output_tokens": summary.output_tokens,
            "total_tokens": summary.total_tokens,
            "cost_usd": None if summary.cost_usd is None else float(Decimal(summary.cost_usd)),
            "by_model": summary.by_model,
        }
    return record


def cost_summary(attempts: list[dict]) -> str:
    priced = [attempt for attempt in attempts if attempt["cost_usd"] is not None]
    summary = f"{len(attempts)} attempt(s) logged, ${sum(attempt['cost_usd'] for attempt in priced):.2f}"
    if finished := [attempt["cost_usd"] for attempt in priced if attempt["status"] == "finished"]:
        summary += f"; a finished task averaged ${sum(finished) / len(finished):.2f}"
    if unpriced := len(attempts) - len(priced):
        summary += f"; {unpriced} attempt(s) called a model with no known price"
    return summary


async def main(
    dataset_path: str,
    dataset_split: str,
    output_path: str,
    model_names: list[ModelName | str],
    thinking_level: str = DEFAULT_THINKING_LEVEL,
    instance_ids: list[str] | None = None,
    num_samples: int | None = None,
    seed: int = 0,
    retry_failed: bool = False,
):
    for model_name in model_names:
        validate_agent_override(model_name, thinking_level)

    from datasets import load_dataset

    output = Path(output_path)
    usage_path = output.with_suffix(".usage.jsonl")
    model_name_or_path = ", ".join(model_names)
    predictions = load_predictions(output)
    attempts = load_attempts(usage_path)
    check_resumable(predictions, attempts, model_name_or_path, thinking_level)
    if retry_failed:
        retried = failed_instance_ids(attempts)
        predictions = [prediction for prediction in predictions if prediction["instance_id"] not in retried]
    predicted = {prediction["instance_id"] for prediction in predictions}

    items = select_items(load_dataset(dataset_path, split=dataset_split), instance_ids, num_samples, seed)
    todo = [item for item in items if item["instance_id"] not in predicted]
    print(f"{len(todo)} of {len(items)} selected task(s) to run, the rest already predicted", file=sys.stderr)  # noqa: T201
    output.parent.mkdir(parents=True, exist_ok=True)
    write_predictions(output, predictions)

    failed = []
    for item in todo:
        instance_id = item["instance_id"]
        outcome = None
        status = "failed"
        started = monotonic()
        with track_run_usage() as usage:
            try:
                async with task_sandbox_env(item) as sandbox_env_id:
                    outcome = await _execute(_run_spec(item, model_names, thinking_level, sandbox_env_id))
                status = "finished"
            except asyncio.CancelledError:
                status = "interrupted"
                print(f"[{instance_id}] interrupted", file=sys.stderr)  # noqa: T201
                raise
            except Exception:
                failed.append(instance_id)
                print(f"[{instance_id}] run failed:", file=sys.stderr)  # noqa: T201
                traceback.print_exc()
            finally:
                record = attempt_record(
                    instance_id, status, model_name_or_path, thinking_level, usage, monotonic() - started
                )
                with usage_path.open("a") as log:
                    log.write(json.dumps(record) + "\n")
                cost = "unpriced" if record["cost_usd"] is None else f"${record['cost_usd']:.2f}"
                print(f"[{instance_id}] {status} after {record['seconds']:.0f}s, {cost}", file=sys.stderr)  # noqa: T201
        predictions.append({
            "model_patch": _model_patch(instance_id, outcome),
            "model_name_or_path": model_name_or_path,
            "instance_id": instance_id,
        })
        write_predictions(output, predictions)
        if outcome is not None and outcome.snapshot is not None:
            write_transcript(
                output.with_suffix(".transcripts") / f"{instance_id}.json",
                outcome.snapshot.values.get("messages") or [],
                {"instance_id": instance_id, "model": model_name_or_path, "thinking_level": thinking_level},
            )

    print(f"{len(predictions)} prediction(s) in {output}; {cost_summary(load_attempts(usage_path))}", file=sys.stderr)  # noqa: T201
    if failed:
        print(  # noqa: T201
            f"{len(failed)}/{len(todo)} instances failed: {', '.join(failed)}. They count as unresolved; rerun with "
            "--retry-failed if they failed for reasons outside the agent, such as the sandbox or the clone.",
            file=sys.stderr,
        )
        if len(failed) == len(todo):
            raise SystemExit(1)


def task_image(item: dict) -> str:
    """The task's SWE-bench image: Verified and SWE-rebench rows name it; the Mini's is derived the harness's way."""
    if image := item.get("image") or item.get("docker_image"):
        return image
    return f"swebench/sweb.eval.x86_64.{item['instance_id'].replace('__', '_1776_')}:latest".lower()


@asynccontextmanager
async def task_sandbox_env(item: dict) -> AsyncGenerator[str]:
    """A sandbox environment on the task's image, with no network, for the length of one run.

    The image's ``testbed`` interpreter has the task's dependencies installed, and ``PYTHONPATH`` points it at the
    agent's copy of the repository. The row is deleted after the run, so it never stays in the environment picker;
    its name is unique, so evals running the same task at once each get their own.
    """
    env = await SandboxEnvironment.objects.acreate(
        scope=Visibility.GLOBAL,
        name=f"swebench {uuid.uuid4().hex[:8]} {item['instance_id']}"[:80],
        base_image=task_image(item),
        egress_policy=None,
        env_vars=[{"name": name, "value": value} for name, value in TASK_ENV_VARS.items()],
    )
    try:
        yield str(env.pk)
    finally:
        await env.adelete()


async def _execute(spec: RunSpec) -> RunOutcome:
    """``execute_run``, except that a cleanup error raised while Ctrl-C unwinds the run still stops the eval."""
    try:
        return await execute_run(spec)
    except Exception as exc:
        if (task := asyncio.current_task()) is not None and task.cancelling():
            raise asyncio.CancelledError from exc
        raise


def _model_patch(instance_id: str, outcome: RunOutcome | None) -> str:
    """A failed run predicts no patch; a finished one that captured none means the eval is broken."""
    if outcome is None:
        return ""
    if outcome.snapshot is None:
        raise RuntimeError(f"[{instance_id}] finished, but its checkpoint could not be read")
    values = outcome.snapshot.values
    # Printed, not put in the prediction: SWE-bench loaders may be strict about its schema.
    if dirty := values.get("pre_run_dirty_files"):
        print(  # noqa: T201
            f"[{instance_id}] WARNING: workspace was dirty before the run; "
            f"model_patch includes pre-existing changes to: {', '.join(dirty)}",
            file=sys.stderr,
        )
    if "model_patch" not in values:
        raise RuntimeError(f"[{instance_id}] finished without a model_patch: the capture_patch wiring drifted")
    return values["model_patch"]


def _run_spec(item: dict, model_names: list[ModelName | str], thinking_level: str, sandbox_env_id: str) -> RunSpec:
    """One SWE-bench instance as a one-shot run: no session, no platform API, the exact model chain asked for."""
    return RunSpec(
        sandbox_env_id=sandbox_env_id,
        thread_id=None,
        repo_id=item["repo"],
        scope=Scope.GLOBAL,
        ref=item["base_commit"],
        input_messages=(HumanMessage(content=_human_message(item)),),
        trigger="eval",
        lock=NoLock(),
        model_names=tuple(model_names),
        agent_thinking_level=thinking_level,
        context_options={"offline": True, "repo_client": SWERepoClient(repo_host="github.com")},
        web_search_enabled=False,
        web_fetch_enabled=False,
        agent_options={"auto_commit_changes": False, "capture_patch": True},
        extra_metadata={"instance_id": item["instance_id"]},
    )


def _human_message(item: dict) -> str:
    human_message = dedent(
        """\
        You are given a problem statement, along with some hints extracted from the issue tracker, to help you understand and solve the problem.

        VERY IMPORTANT: never activate the plan skill, just solve the problem.

        ## Execution constraints

        - The sandbox has no network: web search/fetch tools are disabled, and pip, uv and git cannot download anything. Rely on the repository itself (existing code patterns, tests, docs).
        - The environment is ready: `python` is the project's interpreter with its test dependencies installed, and `/workspace/repo` comes first on `PYTHONPATH`, so tests run against your changes. Do not install anything.

        ## How to work

        - Treat any root-cause analysis embedded in the problem statement as a hypothesis to verify, not a conclusion — reporters are sometimes wrong or partial.
        - Prefer the smallest change that addresses the problem statement while preserving all existing intended behavior. A 3-line fix that follows the issue beats a 30-line architectural change; avoid touching files the fix doesn't require.
        - When the bug was introduced by an identifiable feature commit, fix the interaction between that feature and the broken case — do NOT revert or remove the feature; upstream maintainers virtually never resolve a regression by deleting the feature that caused it. Untested does not mean unintended: behavior promised by the offending commit's docs/changelog must keep working even when no test pins it, and a green test suite does not prove behavior preservation.
        - The grader resets every test file to its original state and applies its own held-out tests before scoring, so your edits to existing test files are discarded and never graded — only your source change is scored. Editing a pre-existing test to make it pass therefore cannot help here; if one seems to need changing, treat that as a signal you may be altering behavior the task wants preserved and re-examine your source. Adding new tests to characterize behavior is fine.
        - Once your fix is implemented, run the issue's reproduction (if any) and the tests directly affected by your change. When those pass, do ONE final check and stop:
          - Re-read the problem statement and verify every described behavior / expected outcome is addressed — multi-symptom issues need all symptoms fixed, not just the first one you reproduced.
          - If your change introduced warnings in the test output that were not there before (RuntimeWarning, DeprecationWarning, ...), eliminate them — new warnings are regressions even when every test passes.
          - Do NOT re-run test suites repeatedly, do not investigate or fix pre-existing failures unrelated to your change, and do not keep polishing (docstrings, comments, optional refactors).

        ## Problem Statement
        ```markdown
        {problem_statement}
        ```
        """  # noqa: E501
    ).format(problem_statement=item["problem_statement"])

    if item["hints_text"]:
        human_message += dedent(
            """
            ## Hints

            Hints are **contextual clues** extracted from mid-discussion comments, not authoritative decisions. They may come from:
            - Issue reporters (who might misunderstand the intended behavior)
            - Discussion threads (where conclusions may be incomplete or premature)
            - Maintainer comments (which carry more weight but may reflect an intermediate position)

            **CRITICAL:** Even if hints suggest "closing", "won't fix", "working as intended", or otherwise imply no change is needed — **you MUST still produce a code fix**. This task always requires a code change. Hints that dismiss the issue may reflect an early opinion that was later reversed, or the fix may be a small improvement (e.g., better error messages, edge-case handling, or documentation) rather than the reporter's exact request. Your job is to find and implement the change that addresses the problem statement.

            When hints describe a specific approach, prefer the simplest implementation that matches the hint over a more elaborate design.

            **Hints:**
            ```
            {hints_text}
            ```
            """  # noqa: E501
        ).format(hints_text=item["hints_text"])

    return human_message


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset-path", default=DEFAULT_DATASET)
    parser.add_argument("--dataset-split", default="test")
    parser.add_argument(
        "--output-path", default="predictions.json", help="The usage log goes next to it, as <stem>.usage.jsonl."
    )
    parser.add_argument("--model-names", nargs="+", default=[ModelName.MINIMAX_M3])
    parser.add_argument("--thinking-level", choices=ThinkingLevel.values, default=DEFAULT_THINKING_LEVEL)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--instance-ids", nargs="+", help="Run exactly these tasks.")
    selection.add_argument(
        "--num-samples", type=int, help="Run this many tasks of the dataset shuffled by --seed; all of them if omitted."
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true", help="Run again the tasks whose last attempt raised.")

    asyncio.run(main(**vars(parser.parse_args())))
