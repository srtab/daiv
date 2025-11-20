import argparse
import asyncio
import json
from pathlib import Path
from textwrap import dedent

import django

from datasets import load_dataset
from langgraph.store.memory import InMemoryStore

from automation.agents.constants import ModelName
from automation.agents.plan_and_execute import PlanAndExecuteAgent
from codebase.context import set_runtime_ctx
from codebase.utils import GitManager


async def main(
    dataset_path: str,
    dataset_split: str,
    planning_model_names: list[ModelName | str],
    execution_model_names: list[ModelName | str],
    num_samples: int | None = None,
):
    dataset = load_dataset(dataset_path, split=dataset_split)
    if num_samples is not None:
        dataset = dataset.take(num_samples)

    predictions = []

    for item in dataset:
        store = InMemoryStore()

        plan_and_execute = await PlanAndExecuteAgent.get_runnable(
            store=store,
            skip_approval=True,
            skip_format_code=True,
            planning_model_names=planning_model_names,
            execution_model_names=execution_model_names,
        )

        async with set_runtime_ctx(
            item["repo"], ref=item["base_commit"], offline=True, client_slug="swe", repo_host="github.com"
        ) as ctx:
            human_message = dedent(
                """\
                ## Problem Statement:
                ```
                {problem_statement}
                ```

                ## Hints:
                ```
                {hints_text}
                ```
                """
            ).format(problem_statement=item["problem_statement"], hints_text=item["hints_text"])

            try:
                await plan_and_execute.ainvoke({"messages": [human_message]}, context=ctx)
            except Exception as e:
                print(f"Error invoking plan and execute for item {item['instance_id']}: {e}")  # noqa: T201
                continue
            else:
                predictions.append({
                    "model_patch": GitManager(ctx.repo).get_diff(),
                    "model_name_or_path": ", ".join(planning_model_names),
                    "instance_id": item["instance_id"],
                })

    print(json.dumps(predictions, indent=2))  # noqa: T201

    with Path(f"predictions_{dataset_path.replace('/', '_')}_{dataset_split}.json").open("w") as f:
        json.dump(predictions, f, indent=2)


if __name__ == "__main__":
    django.setup()

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-path", type=str, default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--dataset-split", type=str, default="dev")
    parser.add_argument("--num-samples", type=int)
    parser.add_argument("--planning-model-names", type=str, nargs="+", default=[ModelName.Z_AI_GLM_4_6])
    parser.add_argument("--execution-model-names", type=str, nargs="+", default=[ModelName.QWEN_3_CODER_PLUS])

    args = parser.parse_args()

    asyncio.run(main(**vars(args)))
