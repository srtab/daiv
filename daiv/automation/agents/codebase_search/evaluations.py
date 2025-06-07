import asyncio

from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_core.runnables import Runnable

from automation.agents.base import BaseAgent
from automation.agents.constants import ModelName
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

from .agent import CodebaseSearchAgent

questions = [
    "How many agents are there in daiv and what are they?",
    "How can i setup a test project on local GitLab to be used with DAIV?",
    "what are the configuration options for the codebase chat agent?",
    "What are the supported models in DAIV?",
    "Is there a way to configure embeddings for the codebase? If yes, what are the options?",
]

codebase_search_evaluator_system = SystemMessagePromptTemplate.from_template(
    """You are a helpful assistant that evaluates the quality of the codebase search.

You will be given a question and a list of documents.

You need to evaluate if the documents are relevant to the question.
You need to return a list of IDs of the documents that are irrelevant to the question.

Only return the list of IDs, no other text.

---

Question: {{ question }}
Documents:
{% for document in documents %}
Document {{loop.index}}: {{document}}
{% endfor %}
""",
    template_format="jinja2",
)


class CodebaseSearchEvaluator(BaseAgent[Runnable[dict[str, str | list[str]], list[str]]]):
    async def compile(self):
        return ChatPromptTemplate.from_messages([codebase_search_evaluator_system]) | self.get_model(
            model=ModelName.GPT_4_1_MINI
        )


async def evaluate(question, result):
    codebase_search_evaluator = await CodebaseSearchEvaluator().agent

    response = await codebase_search_evaluator.ainvoke({
        "question": question,
        "documents": [item.page_content for item in result],
    })
    return response.content


async def run_evaluations(rephrase: bool = False):
    index = CodebaseIndex(RepoClient.create_instance())

    codebase_search = await CodebaseSearchAgent(retriever=await index.as_retriever(), rephrase=rephrase).agent

    results = await asyncio.gather(*[codebase_search.ainvoke(question) for question in questions])

    evaluations = await asyncio.gather(*[
        evaluate(question, result) for question, result in zip(questions, results, strict=True)
    ])

    return evaluations
