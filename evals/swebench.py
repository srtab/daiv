import argparse
import asyncio
import json
import traceback
from pathlib import Path
from textwrap import dedent

import django

from datasets import load_dataset
from langgraph.store.memory import InMemoryStore

from automation.agent.constants import ModelName
from automation.agent.graph import create_daiv_agent
from codebase.base import GitPlatform
from codebase.context import set_runtime_ctx
from codebase.utils import GitManager


async def main(
    dataset_path: str,
    dataset_split: str,
    output_path: str,
    model_names: list[ModelName | str],
    num_samples: int | None = None,
):
    dataset = load_dataset(dataset_path, split=dataset_split)
    if num_samples is not None:
        dataset = dataset.take(num_samples)

    predictions = []

    try:
        for item in dataset:
            store = InMemoryStore()
            # checkpointer = InMemorySaver()  # noqa: ERA001

            async with set_runtime_ctx(
                item["repo"],
                ref=item["base_commit"],
                offline=True,
                git_platform=GitPlatform.SWE,
                repo_host="github.com",
            ) as ctx:
                daiv_agent = await create_daiv_agent(
                    model_names=model_names,
                    ctx=ctx,
                    store=store,
                    # checkpointer=checkpointer,  # noqa: ERA001
                    auto_commit_changes=False,
                    offline=True,
                )
                human_message = dedent(
                    """\
                    You are given a problem statement and some hints extracted from the issue tracker to help you understand it and solve it.

                    ## Problem Statement
                    ```markdown
                    {problem_statement}
                    ```

                    ## Hints

                    Hints are **contextual clues**, not authoritative decisions. They may come from:
                    - Issue reporters (who might misunderstand the intended behavior)
                    - Discussion threads (where conclusions may be incomplete)
                    - Maintainer comments (which carry more weight)

                    ```
                    {hints_text}
                    ```
                    """  # noqa: E501
                ).format(problem_statement=item["problem_statement"], hints_text=item["hints_text"])

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
                        "model_patch": GitManager(ctx.repo).get_diff(),
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
    parser.add_argument("--dataset-path", type=str, default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--dataset-split", type=str, default="dev")
    parser.add_argument("--num-samples", type=int)
    parser.add_argument("--model-names", type=str, nargs="+", default=[ModelName.CLAUDE_SONNET_4_5])
    parser.add_argument("--output-path", type=str, default="predictions.json")

    args = parser.parse_args()

    asyncio.run(main(**vars(args)))
