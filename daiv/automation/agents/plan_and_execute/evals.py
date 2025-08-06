from agentevals.graph_trajectory.llm import create_async_graph_trajectory_llm_as_judge
from agentevals.graph_trajectory.utils import aextract_langgraph_trajectory_from_thread
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from automation.agents.base import BaseAgent
from automation.agents.plan_and_execute.agent import plan_agent
from automation.agents.plan_and_execute.conf import settings


async def main():
    store = InMemoryStore()
    checkpointer = InMemorySaver()

    config = {
        "configurable": {
            "thread_id": "1",
            "bot_username": "test_bot",
            "source_repo_id": "srtab/daiv",
            "source_ref": "main",
        }
    }

    plan_and_execute = await plan_agent(
        BaseAgent.get_model(
            model=settings.PLANNING_MODEL_NAME, max_tokens=8_192, thinking_level=settings.PLANNING_THINKING_LEVEL
        ),
        store,
        config,
        checkpointer=checkpointer,
    )
    await plan_and_execute.ainvoke({"messages": [("human", "Create a quick action.")]}, config)

    extracted_trajectory = await aextract_langgraph_trajectory_from_thread(plan_and_execute, config)
    print("##################", extracted_trajectory)  # NOQA: T201

    reference_outputs = {
        "inputs": [],
        "results": [
            {
                "messages": [
                    {
                        "role": "tool",
                        "name": "finalize_with_targeted_questions",
                        "tool_call_id": "toolu_vrtx_01QrRf9szd6Tg5813mmJScpC",
                        "content": 'Could you provide more details about the quick action you\'d like me to create?\n\n- **What platform/technology**: Is this for a web application, mobile app, desktop software, GitHub Actions, macOS Automator, or something else?\n- **What should it do**: What specific functionality or task should this quick action perform?\n- **Where should it be implemented**: In which part of your codebase or system?\n- **How should it be triggered**: By a button click, keyboard shortcut, API call, or other mechanism?\n\nFor example: "Create a quick action button in the user dashboard that exports data to CSV" or "Create a GitHub Action that runs tests on pull requests".',  # noqa: E501
                    }
                ]
            }
        ],
        "steps": [
            [
                "__start__",
                "agent",
                "tools",
                "agent",
                "tools",
                "agent",
                "tools",
                "agent",
                "tools",
                "agent",
                "tools",
                "agent",
                "tools",
            ]
        ],
    }
    graph_trajectory_evaluator = create_async_graph_trajectory_llm_as_judge(model="openai:o3")
    res = await graph_trajectory_evaluator(
        inputs=extracted_trajectory["inputs"],
        outputs=extracted_trajectory["outputs"],
        reference_outputs=reference_outputs,
    )
    print(res)  # NOQA: T201
