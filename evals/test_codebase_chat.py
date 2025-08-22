import pytest
from langsmith import testing as t
from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT, RAG_HELPFULNESS_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.codebase_chat import CodebaseChatAgent
from automation.agents.constants import ModelName

correctness_evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.O4_MINI, thinking_level=ThinkingLevel.MEDIUM),
)

rag_helpfulness_evaluator = create_llm_as_judge(
    prompt=RAG_HELPFULNESS_PROMPT,
    feedback_key="helpfulness",
    judge=BaseAgent.get_model(model=ModelName.O4_MINI, thinking_level=ThinkingLevel.MEDIUM),
)


@pytest.mark.django_db
@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize(
    "question,reference_outputs",
    [
        (
            "What are the configuration options for the codebase chat agent?",
            """1 · Answer
The configuration options for the codebase chat agent are set via environment variables and a settings class. The main options are:

- `CODEBASE_CHAT_NAME`: The name of the codebase chat agent. Default is `CodebaseChat`.
- `CODEBASE_CHAT_MODEL_NAME`: The model used for codebase chat (e.g., `openrouter:openai/gpt-4-1-mini`). Default is `openrouter:openai/gpt-4-1-mini`.
- `CODEBASE_CHAT_TEMPERATURE`: The temperature parameter for the chat model, controlling randomness. Default is `0.2`.

These can be set as environment variables or configured via the `CodebaseChatSettings` class in the codebase.

2 · References:
- [docs/getting-started/environment-variables.md](http://gitlab:8929/srtab/daiv/-/blob/main/docs/getting-started/environment-variables.md)
- [daiv/automation/agents/codebase_chat/conf.py](http://gitlab:8929/srtab/daiv/-/blob/main/daiv/automation/agents/codebase_chat/conf.py)""",  # noqa: E501
        ),
        ("How many agents are there in DAIV and what are they?", ""),
        ("Hi, what is the capital of France?", ""),
    ],
)
async def test_codebase_chat_correctness(question, reference_outputs):
    t.log_reference_outputs(reference_outputs)

    codebase_chat = await CodebaseChatAgent.get_runnable()

    t.log_inputs({"question": question})

    outputs = await codebase_chat.ainvoke({"messages": [("human", question)]})

    t.log_outputs({"response": outputs["messages"][-1].content})

    correctness_result = correctness_evaluator(
        inputs={"question": question},
        outputs={"response": outputs["messages"][-1].content},
        reference_outputs=reference_outputs,
    )
    assert correctness_result["score"] is True, correctness_result["comment"]

    rag_helpfulness_result = rag_helpfulness_evaluator(
        inputs={"question": question}, outputs={"response": outputs["messages"][-1].content}
    )
    assert rag_helpfulness_result["score"] is True, rag_helpfulness_result["comment"]
