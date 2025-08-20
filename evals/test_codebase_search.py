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
    "query", [("quick_actions decorator registry base"), ("class BaseAgent"), ("fnmatch.fnmatch(")]
)
async def test_codebase_search_relevance(query):
    index = CodebaseIndex(RepoClient.create_instance())
    codebase_search = await CodebaseSearchAgent(retriever=await index.as_retriever()).agent

    t.log_inputs({"query": query})

    outputs = [item.page_content for item in await codebase_search.ainvoke(query)]

    t.log_outputs({"documents": outputs})

    retrieval_relevance_result = retrieval_relevance_evaluator(inputs={"query": query}, context={"documents": outputs})
    assert retrieval_relevance_result["score"] is True, retrieval_relevance_result["comment"]
