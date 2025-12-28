import argparse
import asyncio
import json
from pathlib import Path
from textwrap import dedent

import django

from datasets import load_dataset
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from automation.agents.constants import ModelName
from automation.agents.deepagent.graph import create_daiv_agent
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

    for item in dataset:
        store = InMemoryStore()
        checkpointer = InMemorySaver()

        async with set_runtime_ctx(
            item["repo"],
            ref=item["base_commit"],
            offline=True,
            client_slug="swe",
            repo_host="github.com",
            scope="issue",
        ) as ctx:
            daiv_agent = await create_daiv_agent(
                model_names=model_names, ctx=ctx, store=store, checkpointer=checkpointer
            )
            human_message = dedent(
                """\
                You are given a problem statement and some hints extracted from the issue tracker to help you understand it and solve it.

                ## Problem Statement
                ```markdown
                {problem_statement}
                ```

                ## Hints

                These hints are **contextual clues**, not authoritative decisions. They may come from:
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
            except Exception as e:
                print(f"Error invoking DAIV agent for item {item['instance_id']}: {e}")  # noqa: T201
                continue
            else:
                predictions.append({
                    "model_patch": GitManager(ctx.repo).get_diff(),
                    "model_name_or_path": ", ".join(model_names),
                    "instance_id": item["instance_id"],
                })

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
