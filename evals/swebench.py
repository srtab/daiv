import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path
from textwrap import dedent

import django

from datasets import load_dataset
from langgraph.store.memory import InMemoryStore

# The first-party imports below define Django models at import time, so the app
# registry must be populated before them.
django.setup()

from automation.agent import ThinkingLevel  # noqa: E402
from automation.agent.constants import ModelName  # noqa: E402
from automation.agent.graph import create_daiv_agent  # noqa: E402
from codebase.base import GitPlatform, Scope  # noqa: E402
from codebase.context import set_runtime_ctx  # noqa: E402


async def main(
    dataset_path: str,
    dataset_split: str,
    output_path: str,
    model_names: list[ModelName | str],
    instance_ids: list[str] | None = None,
    num_samples: int | None = None,
):
    dataset = load_dataset(dataset_path, split=dataset_split)
    if instance_ids:
        selected_instance_ids = set(instance_ids)
        dataset = dataset.filter(lambda item: item["instance_id"] in selected_instance_ids)

    if num_samples is not None:
        if num_samples >= 20:
            raise ValueError("num_samples must be less than or equal to 20")

        dataset = dataset.take(num_samples)

    predictions = []

    try:
        for item in dataset:
            store = InMemoryStore()
            # checkpointer = InMemorySaver()  # noqa: ERA001

            async with set_runtime_ctx(
                item["repo"],
                scope=Scope.GLOBAL,
                ref=item["base_commit"],
                offline=True,
                git_platform=GitPlatform.SWE,
                repo_host="github.com",
            ) as ctx:
                daiv_agent = await create_daiv_agent(
                    model_names=model_names,
                    thinking_level=ThinkingLevel.HIGH,
                    ctx=ctx,
                    store=store,
                    # checkpointer=checkpointer,  # noqa: ERA001
                    auto_commit_changes=False,
                    # On sandbox-enabled runs the agent's edits live in the sandbox
                    # /workspace/repo, not in this local clone (it only seeds the session), so
                    # a local diff here would be empty. capture_patch makes GitMiddleware take
                    # the diff at turn end from whichever workspace is authoritative and expose
                    # it as `model_patch` in the output state.
                    capture_patch=True,
                    web_search_enabled=False,
                    web_fetch_enabled=False,
                )
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

                result = None
                try:
                    result = await daiv_agent.ainvoke(
                        {"messages": [human_message]},
                        context=ctx,
                        config={"configurable": {"thread_id": item["instance_id"]}},
                    )
                except Exception:
                    print(f"[{item['instance_id']}] run failed:", file=sys.stderr)  # noqa: T201
                    traceback.print_exc()
                finally:
                    # GitMiddleware sets this when the workspace differed from HEAD before the
                    # agent acted — the patch below is poisoned with changes the agent never
                    # made. Surface it next to the run so a bad grading batch is explainable
                    # without grepping server logs. (Kept out of the predictions file: SWE-bench
                    # loaders may be strict about its schema.)
                    if result is not None and (dirty := result.get("pre_run_dirty_files")):
                        print(  # noqa: T201
                            f"[{item['instance_id']}] WARNING: workspace was dirty before the run; "
                            f"model_patch includes pre-existing changes to: {', '.join(dirty)}",
                            file=sys.stderr,
                        )
                    # A failed run degrades to an empty patch (the traceback above is the
                    # signal); a *successful* run missing the key means capture_patch wiring
                    # drifted — let the KeyError kill the eval rather than silently emit a
                    # predictions file full of empty patches.
                    predictions.append({
                        "model_patch": result["model_patch"] if result is not None else "",
                        "model_name_or_path": ", ".join(model_names),
                        "instance_id": item["instance_id"],
                    })
    finally:
        print(json.dumps(predictions, indent=2))  # noqa: T201

        with Path(output_path).open("w") as f:
            json.dump(predictions, f, indent=2)


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
