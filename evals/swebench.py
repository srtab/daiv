import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path
from textwrap import dedent

import django
from django.apps import apps

from langchain_core.messages import HumanMessage

# The first-party imports below define Django models at import time, so the app registry must be populated
# before them, once: under pytest a second setup re-applies the test LOGGING and disables existing loggers.
if not apps.ready:
    django.setup()

from sessions.executor.lock import NoLock  # noqa: E402
from sessions.executor.run import execute_run  # noqa: E402
from sessions.executor.spec import RunOutcome, RunSpec  # noqa: E402

from automation.agent import ThinkingLevel  # noqa: E402
from automation.agent.constants import ModelName  # noqa: E402
from automation.agent.validators import validate_agent_override  # noqa: E402
from codebase.base import GitPlatform, Scope  # noqa: E402


async def main(
    dataset_path: str,
    dataset_split: str,
    output_path: str,
    model_names: list[ModelName | str],
    instance_ids: list[str] | None = None,
    num_samples: int | None = None,
):
    for model_name in model_names:
        validate_agent_override(model_name, None)

    from datasets import load_dataset

    dataset = load_dataset(dataset_path, split=dataset_split)
    if instance_ids:
        selected_instance_ids = set(instance_ids)
        dataset = dataset.filter(lambda item: item["instance_id"] in selected_instance_ids)

    if num_samples is not None:
        if num_samples >= 20:
            raise ValueError("num_samples must be less than or equal to 20")

        dataset = dataset.take(num_samples)

    predictions = []
    failed = []

    try:
        for item in dataset:
            instance_id = item["instance_id"]
            outcome = None
            try:
                outcome = await _execute(_run_spec(item, model_names))
            except asyncio.CancelledError:
                print(f"[{instance_id}] interrupted", file=sys.stderr)  # noqa: T201
                raise
            except Exception:
                failed.append(instance_id)
                print(f"[{instance_id}] run failed:", file=sys.stderr)  # noqa: T201
                traceback.print_exc()
            finally:
                predictions.append({
                    "model_patch": _model_patch(instance_id, outcome),
                    "model_name_or_path": ", ".join(model_names),
                    "instance_id": instance_id,
                })
    finally:
        print(json.dumps(predictions, indent=2))  # noqa: T201

        with Path(output_path).open("w") as f:
            json.dump(predictions, f, indent=2)

    if failed:
        print(f"{len(failed)}/{len(predictions)} instances failed: {', '.join(failed)}", file=sys.stderr)  # noqa: T201
        if len(failed) == len(predictions):
            raise SystemExit(1)


async def _execute(spec: RunSpec) -> RunOutcome:
    """``execute_run``, except that a cleanup error raised while Ctrl-C unwinds the run still stops the eval."""
    try:
        return await execute_run(spec)
    except Exception as exc:
        if (task := asyncio.current_task()) is not None and task.cancelling():
            raise asyncio.CancelledError from exc
        raise


def _model_patch(instance_id: str, outcome: RunOutcome | None) -> str:
    """A failed or interrupted run predicts no patch; a finished one that captured none means the eval is broken."""
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


def _run_spec(item: dict, model_names: list[ModelName | str]) -> RunSpec:
    """One SWE-bench instance as a one-shot run: no session, no platform API, the exact model chain asked for."""
    return RunSpec(
        thread_id=None,
        repo_id=item["repo"],
        scope=Scope.GLOBAL,
        ref=item["base_commit"],
        input_messages=(HumanMessage(content=_human_message(item)),),
        trigger="eval",
        lock=NoLock(),
        model_names=tuple(model_names),
        agent_thinking_level=ThinkingLevel.HIGH,
        context_options={"offline": True, "git_platform": GitPlatform.SWE, "repo_host": "github.com"},
        agent_options={
            "auto_commit_changes": False,
            "capture_patch": True,
            "web_search_enabled": False,
            "web_fetch_enabled": False,
        },
        extra_metadata={"instance_id": item["instance_id"]},
    )


def _human_message(item: dict) -> str:
    human_message = dedent(
        """\
        You are given a problem statement, along with some hints extracted from the issue tracker, to help you understand and solve the problem.

        VERY IMPORTANT: never activate the plan skill, just solve the problem.

        ## Execution constraints

        - Web research is unavailable: web search/fetch tools are disabled. Rely on the repository itself (existing code patterns, tests, docs). Installing packages with pip/uv DOES work — use it to provision test dependencies.

        ## How to work

        - Treat any root-cause analysis embedded in the problem statement as a hypothesis to verify, not a conclusion — reporters are sometimes wrong or partial.
        - Before running tests, provision the environment in ONE step: check `pyproject.toml` / `setup.py` / `tox.ini` for the supported Python version and the test extra's name, then install it once (e.g. `pip install -e ".[test]"`) instead of discovering missing dependencies one test failure at a time.
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-path", type=str, default="princeton-nlp/SWE-bench_Verified")
    parser.add_argument("--dataset-split", type=str, default="test")
    parser.add_argument("--num-samples", type=int, default=10)
    parser.add_argument("--model-names", type=str, nargs="+", default=[ModelName.MINIMAX_M3])
    parser.add_argument("--instance-ids", type=str, nargs="+")
    parser.add_argument("--output-path", type=str, default="predictions.json")

    args = parser.parse_args()

    asyncio.run(main(**vars(args)))
