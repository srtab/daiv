import logging
from typing import cast

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from automation.graphs.agents import BaseAgent
from automation.graphs.search_agent import GradeDocumentsOutput, ImprovedQueryOutput
from codebase.indexes import CodebaseIndex

from .prompts import grade_human, grade_system, re_write_human, re_write_system
from .state import OverallState

MAX_ITERATIONS = 3

logger = logging.getLogger(__name__)


class CodebaseSearchAgent(BaseAgent):
    """
    Agent to search for code snippets in the codebase.
    """

    def __init__(self, source_repo_id: str, index: CodebaseIndex):
        super().__init__()
        self.index = index
        self.source_repo_id = source_repo_id

    def compile(self) -> CompiledStateGraph:
        workflow = StateGraph(OverallState)

        # Add nodes
        workflow.add_node("retrieve", self.retrieve)
        workflow.add_node("grade_documents", self.grade_documents)
        workflow.add_node("transform_query", self.transform_query)

        # Add edges
        workflow.add_edge(START, "retrieve")
        workflow.add_conditional_edges("retrieve", self.should_grade_documents)
        workflow.add_conditional_edges("grade_documents", self.should_transform_query)
        workflow.add_edge("transform_query", "retrieve")

        return workflow.compile()

    def retrieve(self, state: OverallState):
        """
        Retrieve documents from the codebase index.

        Args:
            state (GraphState): The current state of the graph.
        """
        return {
            "documents": self.index.search(self.source_repo_id, state["query"]),
            "iterations": state.get("iterations", 0) + 1,
        }

    def grade_documents(self, state: OverallState):
        """
        Grade the relevance of the retrieved documents to the query.

        Args:
            state (GraphState): The current state of the graph.
        """

        grader_agent = self.model.with_structured_output(GradeDocumentsOutput)

        filtered_docs = []

        for document in state["documents"]:
            messages = [
                SystemMessage(grade_system),
                HumanMessage(
                    grade_human.format(
                        query=state["query"], query_intent=state["query_intent"], document=document.page_content
                    )
                ),
            ]
            response = cast(GradeDocumentsOutput, grader_agent.invoke(messages))

            if response.binary_score:
                logger.info("[grade_documents] Document '%s' is relevant to the query", document.metadata["source"])
                filtered_docs.append(document)

        return {"documents": filtered_docs}

    def transform_query(self, state: OverallState):
        """
        Transform the query to improve retrieval.

        Args:
            state (GraphState): The current state of the graph.
        """
        messages = [
            SystemMessage(re_write_system),
            HumanMessage(re_write_human.format(query=state["query"], query_intent=state["query_intent"])),
        ]

        query_rewriter = self.model.with_structured_output(ImprovedQueryOutput).with_config(temperature=0.5)
        response = cast(ImprovedQueryOutput, query_rewriter.invoke(messages))

        logger.info("[transform_query] Query '%s' improved to '%s'", state["query"], response.query)

        return {"query": response.query}

    def should_grade_documents(self, state: OverallState):
        """
        Check if we should transform the query.
        """
        if not state["documents"]:
            logger.info("[should_grade_documents] No documents retrieved. Moving to transform_query state.")
            return "transform_query"
        logger.info("[should_grade_documents] Documents retrieved. Moving to grade_documents state.")
        return "grade_documents"

    def should_transform_query(self, state: OverallState):
        """
        Check if we should transform the query.
        """

        if not state["documents"] and state["iterations"] < MAX_ITERATIONS:
            logger.info("[should_transform_query] No relevant documents found. Moving to transform_query state.")
            return "transform_query"
        logger.info("[should_transform_query] Relevant documents found.")
        return END
