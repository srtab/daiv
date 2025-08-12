import pytest
from langsmith import testing as t
from openevals.llm import create_llm_as_judge
from openevals.prompts import RAG_RETRIEVAL_RELEVANCE_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.codebase_search.agent import CodebaseSearchAgent
from automation.agents.constants import ModelName
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

retrieval_relevance_evaluator = create_llm_as_judge(
    prompt=RAG_RETRIEVAL_RELEVANCE_PROMPT,
    feedback_key="retrieval_relevance",
    judge=BaseAgent.get_model(model=ModelName.O4_MINI, thinking_level=ThinkingLevel.MEDIUM),
)


@pytest.mark.django_db
@pytest.mark.langsmith
@pytest.mark.parametrize(
    "question,rephrase",
    [
        ("How many agents are there in daiv and what are they?", True),
        ("How many agents are there in daiv and what are they?", False),
        ("How can i setup a test project on local GitLab to be used with DAIV?", True),
        ("How can i setup a test project on local GitLab to be used with DAIV?", False),
        ("What are the configuration options for the codebase chat agent?", True),
        ("What are the configuration options for the codebase chat agent?", False),
        ("What are the supported models in DAIV?", True),
        ("What are the supported models in DAIV?", False),
        ("Is there a way to configure embeddings for the codebase? If yes, what are the options?", True),
        ("Is there a way to configure embeddings for the codebase? If yes, what are the options?", False),
    ],
)
async def test_codebase_search_relevance(question, rephrase):
    index = CodebaseIndex(RepoClient.create_instance())
    codebase_search = await CodebaseSearchAgent(retriever=await index.as_retriever(), rephrase=rephrase).agent

    t.log_inputs({"question": question})

    outputs = [item.page_content for item in await codebase_search.ainvoke(question)]

    t.log_outputs({"documents": outputs})

    retrieval_relevance_result = retrieval_relevance_evaluator(
        inputs={"question": question}, context={"documents": outputs}
    )
    assert retrieval_relevance_result["score"] is True, retrieval_relevance_result["comment"]
