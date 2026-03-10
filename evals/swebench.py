import argparse
import asyncio
import json
import traceback
from pathlib import Path
from textwrap import dedent

import django

from datasets import load_dataset
from langgraph.store.memory import InMemoryStore

from automation.agent import ThinkingLevel
from automation.agent.constants import ModelName
from automation.agent.graph import create_daiv_agent
from codebase.base import GitPlatform, Scope
from codebase.context import set_runtime_ctx
from codebase.utils import GitManager


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
                    web_search_enabled=False,
                    web_fetch_enabled=False,
                )
                human_message = dedent(
                    """\
                    You are given a problem statement, along with some hints extracted from the issue tracker, to help you understand and solve the problem.

                    VERY IMPORTANT: never activate the plan skill, just solve the problem.

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

                        When hints describe a specific approach or say "the fix is simple," prefer the simplest implementation that matches the hint over a more elaborate design. A 3-line change that follows the hint is better than a 30-line architectural change.

                        **Hints:**
                        ```
                        {hints_text}
                        ```
                        """  # noqa: E501
                    ).format(hints_text=item["hints_text"])

                try:
                    await daiv_agent.ainvoke(
                        {"messages": [human_message]},
                        context=ctx,
                        config={"configurable": {"thread_id": item["instance_id"]}},
                    )
                except Exception:
                    traceback.print_exc()
                    continue
                finally:
                    predictions.append({
                        "model_patch": GitManager(ctx.gitrepo).get_diff(),
                        "model_name_or_path": ", ".join(model_names),
                        "instance_id": item["instance_id"],
                    })
    finally:
        print(json.dumps(predictions, indent=2))  # noqa: T201

        with Path(output_path).open("w") as f:
            json.dump(predictions, f, indent=2)


if __name__ == "__main__":
    django.setup()

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-path", type=str, default="princeton-nlp/SWE-bench_Verified")
    parser.add_argument("--dataset-split", type=str, default="test")
    parser.add_argument("--num-samples", type=int, default=10)
    parser.add_argument("--model-names", type=str, nargs="+", default=[ModelName.CLAUDE_SONNET_4_6])
    parser.add_argument("--instance-ids", type=str, nargs="+")
    parser.add_argument("--output-path", type=str, default="predictions.json")

    args = parser.parse_args()

    asyncio.run(main(**vars(args)))
