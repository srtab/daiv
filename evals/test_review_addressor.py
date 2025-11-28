import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langsmith import testing as t
from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.constants import ModelName
from automation.agents.review_addressor.agent import ReviewAddressorAgent

evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.GPT_5_MINI, thinking_level=ThinkingLevel.MEDIUM),
)


@pytest.mark.django_db
@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize(
    "notes,diff,reference_outputs",
    [
        (
            [("human", "Is this the most performant way of doing this?")],
            """--- a/daiv/codebase/utils.py
+++ b/daiv/codebase/utils.py
@@ -28,0 +28,1 @@
    return bool(re.search(mention_pattern, note_body, re.IGNORECASE))
""",
            "I’d keep it: `re.search` compiles and caches patterns internally, so for a single `@username` lookup per note this stays at O(n) with minimal overhead. Precompiling would only help if we were hot-looping the same `current_user.username`, which we aren’t.",  # NOQA: E501
        ),
        (
            [("human", "Hi, I'm a human.")],
            """--- a/daiv/codebase/utils.py
+++ b/daiv/codebase/utils.py
@@ -28,0 +28,1 @@
    return bool(re.search(mention_pattern, note_body, re.IGNORECASE))
""",
            "Could you clarify what in `[daiv/codebase/utils.py](daiv/codebase/utils.py)` you’d like me to address?",
        ),
        (
            [("human", "`current_user` will be defined with which user?")],
            """--- a/daiv/codebase/utils.py
+++ b/daiv/codebase/utils.py
@@ -27,0 +27,1 @@
    mention_pattern = rf"@{re.escape(current_user.username)}\b"
""",
            "Could you clarify what in `[daiv/codebase/utils.py](daiv/codebase/utils.py)` you’d like me to address?",
        ),
    ],
)
async def test_review_reply_correctness(notes, diff, reference_outputs):
    """
    Test that the agent can reply to reviewer's comments or questions.
    """
    inputs = {"notes": notes, "diff": diff}

    t.log_reference_outputs(reference_outputs)
    t.log_inputs(inputs)

    store = InMemoryStore()
    checkpointer = InMemorySaver()

    review_addressor = await ReviewAddressorAgent.get_runnable(store=store, checkpointer=checkpointer)

    outputs = await review_addressor.nodes["reply_reviewer"].ainvoke(inputs, store=store)

    t.log_outputs({"reply": outputs.update["reply"]})

    result = evaluator(inputs=inputs, outputs={"reply": outputs.update["reply"]}, reference_outputs=reference_outputs)
    assert result["score"] is True, result["comment"]
