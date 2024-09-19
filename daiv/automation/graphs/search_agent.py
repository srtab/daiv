import logging
from textwrap import dedent
from typing import cast

from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from codebase.indexes import CodebaseIndex

MAX_ITERATIONS = 3

logger = logging.getLogger(__name__)


class GradeDocumentsOutput(BaseModel):
    """
    Binary score for relevance check on retrieved documents.
    """

    binary_score: bool = Field(description="Documents are relevant to the query. True if relevant, False otherwise.")


class ImprovedQueryOutput(BaseModel):
    """
    Represents a better query.
    """

    query: str = Field(description="The improved query.")


class SearchState(TypedDict):
    """
    Represents the state of our graph.
    """

    index: CodebaseIndex
    repo_id: str
    query: str
    query_intent: str
    documents: list[Document]
    iterations: int


model = ChatOpenAI(model="gpt-4o-mini-2024-07-18", temperature=0)


def retrieve(state: SearchState):
    """
    Retrieve documents from the codebase index.

    Args:
        state (GraphState): The current state of the graph.
    """
    codebase_index = state["index"]
    return {
        "documents": codebase_index.search(state["repo_id"], state["query"]),
        "iterations": state.get("iterations", 0) + 1,
    }


def grade_documents(state: SearchState):
    """
    Grade the relevance of the retrieved documents to the query.

    Args:
        state (GraphState): The current state of the graph.
    """
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            dedent(
                """\
                You are a grader assessing relevance of a retrieved snippet of code to a query and its intent.
                It does not need to be a stringent test. The goal is to filter out erroneous/irrelevant retrievals.
                """
            ),
        ),
        ("human", "Query: {query}\nIntent of the query: {query_intent}\n\nRetrieved snippet:\n{document}"),
    ])

    grader_agent = prompt | model.with_structured_output(GradeDocumentsOutput)

    filtered_docs = []

    for document in state["documents"]:
        score = cast(
            GradeDocumentsOutput,
            grader_agent.invoke({
                "query": state["query"],
                "query_intent": state["query_intent"],
                "document": document.page_content,
            }),
        )
        if score.binary_score:
            logger.info("[grade_documents] Document '%s' is relevant to the query", document.metadata["source"])
            filtered_docs.append(document)

    return {"documents": filtered_docs}


def transform_query(state: SearchState):
    """
    Transform the query to improve retrieval.

    Args:
        state (GraphState): The current state of the graph.
    """
    re_write_prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            dedent(
                """\
                Act as a search query rewriter to improve the relevance and precision of code search queries.
                The rewritten queries should include more **code-related keywords**.
                Focus on keywords that developers would typically use when searching for code snippets.

                ## Tips
                1. Use synonyms of the keywords to increase the chances of finding the relevant code snippet.
                2. Avoid ambiguous terms in the query to get precise results.
                3. Don't use: "code", "snippet", "example", "sample", etc. as they are redundant.

                ## Examples:
                Query: class FOOField
                Improved query: class implementation FOOField

                Query: get all elements from a list
                Improved query: retrieve all elements from a list

                Query: sort a list of integers
                Improved query: order a list of integers
                """
            ),
        ),
        ("human", "Initial query: {query}\nIntent of the query: {query_intent}\nFormulate an improved query."),
    ])

    query_rewriter = re_write_prompt | model.with_structured_output(ImprovedQueryOutput).with_config(temperature=0.5)
    better_query = cast(
        ImprovedQueryOutput, query_rewriter.invoke({"query": state["query"], "query_intent": state["query_intent"]})
    )

    logger.info("[transform_query] Query '%s' improved to '%s'", state["query"], better_query.query)

    return {"query": better_query.query}


def should_grade_documents(state: SearchState):
    """
    Check if we should transform the query.
    """
    if not state["documents"]:
        logger.info("[should_grade_documents] No documents retrieved. Moving to transform_query state.")
        return "transform_query"
    logger.info("[should_grade_documents] Documents retrieved. Moving to grade_documents state.")
    return "grade_documents"


def should_transform_query(state: SearchState):
    """
    Check if we should transform the query.
    """

    if not state["documents"] and state["iterations"] < MAX_ITERATIONS:
        logger.info("[should_transform_query] No relevant documents found. Moving to transform_query state.")
        return "transform_query"
    logger.info("[should_transform_query] Relevant documents found.")
    return END


# Create the workflow
workflow = StateGraph(SearchState)

# Add nodes
workflow.add_node("retrieve", retrieve)
workflow.add_node("grade_documents", grade_documents)
workflow.add_node("transform_query", transform_query)

# Add edges
workflow.add_edge(START, "retrieve")
workflow.add_conditional_edges("retrieve", should_grade_documents)
workflow.add_conditional_edges("grade_documents", should_transform_query)
workflow.add_edge("transform_query", "retrieve")

search_agent = workflow.compile()
