import logging
from typing import Literal, cast

from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from automation.agents import BaseAgent
from automation.agents.base import CODING_PERFORMANT_MODEL_NAME, GENERIC_COST_EFFICIENT_MODEL_NAME
from automation.agents.prebuilt import REACTAgent
from automation.agents.prompts import execute_plan_system
from automation.tools.toolkits import WriteRepositoryToolkit
from codebase.base import FileChange
from codebase.clients import AllRepoClient
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig

from .prompts import autofix_apply_human, pipeline_log_classifier_human, pipeline_log_classifier_system
from .schemas import PipelineLogClassifierOutput
from .state import OverallState

logger = logging.getLogger("daiv.agents")


class PipelineFixerAgent(BaseAgent[CompiledStateGraph]):
    """ """

    def __init__(self, *, repo_client: AllRepoClient, source_repo_id: str, source_ref: str, job_id: int, **kwargs):
        self.repo_client = repo_client
        self.source_repo_id = source_repo_id
        self.source_ref = source_ref
        self.job_id = job_id
        self.repo_config = RepositoryConfig.get_config(self.source_repo_id)
        self.codebase_index = CodebaseIndex(self.repo_client)
        super().__init__(**kwargs)

    def get_config(self) -> RunnableConfig:
        """
        Include the metadata identifying the source repository and pipeline.

        Returns:
            dict: The configuration for the agent.
        """
        config = super().get_config()
        config["tags"].append(self.repo_client.client_slug)
        config["metadata"].update({
            "repo_client": self.repo_client.client_slug,
            "source_repo_id": self.source_repo_id,
            "source_ref": self.source_ref,
            "job_id": self.job_id,
        })
        return config

    def compile(self) -> CompiledStateGraph:
        workflow = StateGraph(OverallState)

        workflow.add_node("categorizer", self.categorizer)
        workflow.add_node("apply_autofix", self.apply_autofix)
        workflow.add_node("respond", self.respond)

        workflow.add_edge(START, "categorizer")
        workflow.add_conditional_edges("categorizer", self.should_autofix)
        workflow.add_edge("apply_autofix", END)
        workflow.add_edge("respond", END)

        in_memory_store = InMemoryStore()

        return workflow.compile(checkpointer=self.checkpointer, store=in_memory_store)

    def categorizer(self, state: OverallState):
        """
        Categorize the issue based on the logs from the failed CI/CD pipeline.

        This will determine whether the issue is directly related to the codebase or caused by external factors.

        Args:
            state (dict): The state of the agent.

        Returns:
            dict: The state of the agent with the category added.
        """
        prompt = ChatPromptTemplate.from_messages([pipeline_log_classifier_system, pipeline_log_classifier_human])

        evaluator = prompt | self.model.with_structured_output(PipelineLogClassifierOutput)

        response = cast(
            PipelineLogClassifierOutput,
            evaluator.invoke(
                {"job_logs": state["job_logs"], "diff": state["diff"]},
                config={"configurable": {"model": GENERIC_COST_EFFICIENT_MODEL_NAME}},
            ),
        )
        return {"category": response.category}

    def should_autofix(self, state: OverallState) -> Literal["apply_autofix", "respond"]:
        """
        Determine whether the issue should be fixed automatically or manually.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Literal["apply_autofix", "respond"]: The next step in the workflow.
        """
        return "apply_autofix" if state["category"] == "codebase" else "respond"

    def apply_autofix(self, state: OverallState, store: BaseStore):
        """
        Apply the autofix.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            dict: The state of the agent with the autofix applied.
        """
        toolkit = WriteRepositoryToolkit.create_instance(self.repo_client, self.source_repo_id, self.source_ref)

        prompt = ChatPromptTemplate.from_messages([
            SystemMessagePromptTemplate.from_template(
                execute_plan_system, "jinja2", additional_kwargs={"cache-control": {"type": "ephemeral"}}
            ),
            autofix_apply_human,
        ])
        messages = prompt.format_messages(
            job_logs=state["job_logs"],
            diff=state["diff"],
            repository_description=self.repo_config.repository_description,
            repository_structure=self.codebase_index.extract_tree(self.source_repo_id, self.source_ref),
        )

        react_agent = REACTAgent(
            run_name="execute_plan_react_agent",
            tools=toolkit.get_tools(),
            model_name=CODING_PERFORMANT_MODEL_NAME,
            store=store,
        )
        react_agent.agent.invoke({"messages": messages}, config={"recursion_limit": 50})

    def respond(self, state: OverallState):
        """ """
        pass

    def get_files_to_commit(self) -> list[FileChange]:
        if self.agent.store is None:
            return []
        return [
            cast(FileChange, item.value["data"])
            for item in self.agent.store.search(("file_changes", self.source_repo_id, self.source_ref))
        ]
