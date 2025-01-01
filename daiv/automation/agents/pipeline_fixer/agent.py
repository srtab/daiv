import logging
from typing import Literal, cast

from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import END, START, CompiledStateGraph, StateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from automation.agents import BaseAgent
from automation.agents.base import (
    CODING_PERFORMANT_MODEL_NAME,
    GENERIC_COST_EFFICIENT_MODEL_NAME,
    GENERIC_PERFORMANT_MODEL_NAME,
)
from automation.agents.prebuilt import REACTAgent
from automation.agents.prompts import execute_plan_system
from automation.constants import DEFAULT_RECURSION_LIMIT
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import ReadRepositoryToolkit, SandboxToolkit, WebSearchToolkit, WriteRepositoryToolkit
from automation.utils import file_changes_namespace
from codebase.base import FileChange
from codebase.clients import AllRepoClient
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig

from .prompts import (
    autofix_apply_human,
    external_factor_plan_human,
    external_factor_plan_system,
    pipeline_log_classifier_human,
    pipeline_log_classifier_system,
)
from .schemas import ExternalFactorPlanOutput, PipelineLogClassifierOutput
from .state import OverallState

logger = logging.getLogger("daiv.agents")


class PipelineFixerAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent for fixing pipeline failures.
    """

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
        """
        Compile the state graph for the agent.

        Returns:
            CompiledStateGraph: The compiled state graph.
        """
        workflow = StateGraph(OverallState)

        workflow.add_node("categorizer", self.categorizer)
        workflow.add_node("apply_unittest_fix", self.apply_unittest_fix)
        workflow.add_node("apply_lint_fix", self.apply_lint_fix)
        workflow.add_node("respond", self.respond)

        workflow.add_edge(START, "categorizer")
        workflow.add_conditional_edges("categorizer", self.determine_next_action)
        workflow.add_conditional_edges(
            "apply_unittest_fix",
            self.determine_if_lint_fix_should_be_applied,
            {"apply_lint_fix": "apply_lint_fix", "end": END},
        )
        workflow.add_edge("apply_lint_fix", END)
        workflow.add_edge("respond", END)

        in_memory_store = InMemoryStore()

        return workflow.compile(checkpointer=self.checkpointer, store=in_memory_store)

    def categorizer(self, state: OverallState):
        """
        Categorize the issue based on the logs from the failed CI/CD pipeline.

        This will determine whether the issue is directly related to the codebase or caused by external factors.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            OverallState: The state of the agent with the category added.
        """
        prompt = ChatPromptTemplate.from_messages([pipeline_log_classifier_system, pipeline_log_classifier_human])

        evaluator = prompt | self.model.with_structured_output(PipelineLogClassifierOutput)

        response = cast(
            "PipelineLogClassifierOutput",
            evaluator.invoke(
                {"job_logs": state["job_logs"], "diff": state["diff"]},
                config={"configurable": {"model": GENERIC_COST_EFFICIENT_MODEL_NAME}},
            ),
        )
        return {
            "category": response.category,
            "pipeline_phase": response.pipeline_phase,
            "root_cause": response.root_cause,
            "iteration": state.get("iteration", 0) + 1,
        }

    def determine_next_action(self, state: OverallState) -> Literal["apply_unittest_fix", "apply_lint_fix", "respond"]:
        """
        Determine whether the issue should be fixed automatically or manually.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            Literal["apply_unittest_fix", "apply_lint_fix", "respond"]: The next step in the workflow.
        """
        if state["category"] == "codebase":
            if state["pipeline_phase"] == "lint" and self.repo_config.commands.enabled():
                return "apply_lint_fix"
            elif state["pipeline_phase"] == "unittest":
                return "apply_unittest_fix"
        return "respond"

    def apply_unittest_fix(self, state: OverallState, store: BaseStore):
        """
        Apply the unittest fix.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            OverallState: The state of the agent with the autofix applied.
        """
        tools = WriteRepositoryToolkit.create_instance(
            self.repo_client, self.source_repo_id, self.source_ref
        ).get_tools()
        if self.repo_config.commands.enabled():
            tools += SandboxToolkit.create_instance().get_tools()

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
            run_name="unittest_fix_react_agent",
            tools=tools,
            model_name=CODING_PERFORMANT_MODEL_NAME,
            fallback_model_name=GENERIC_PERFORMANT_MODEL_NAME,
            store=store,
        )
        react_agent.agent.invoke({"messages": messages}, config={"recursion_limit": DEFAULT_RECURSION_LIMIT})

    def determine_if_lint_fix_should_be_applied(
        self, state: OverallState, store: BaseStore
    ) -> Literal["apply_lint_fix", "end"]:
        """
        Determine whether the lint fix should be applied after the unittest fix.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Literal["apply_lint_fix", "end"]: The next step in the workflow.
        """
        if self.repo_config.commands.enabled() and store.search(
            file_changes_namespace(self.source_repo_id, self.source_ref), limit=1
        ):
            return "apply_lint_fix"
        return "end"

    def apply_lint_fix(self, state: OverallState, store: BaseStore):
        """
        Apply the lint fix.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.
        """
        run_command_tool = RunSandboxCommandsTool(
            source_repo_id=self.source_repo_id, source_ref=self.source_ref, api_wrapper=self.repo_client
        )
        run_command_tool.invoke({
            "commands": [self.repo_config.commands.install_dependencies, self.repo_config.commands.format_code],
            "intent": "Fix linting issues",
            "store": store,
        })

    def respond(self, state: OverallState, store: BaseStore):
        """
        Respond to user with the root cause and a plan to fix the issue.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            OverallState: The state of the agent with the actions added.
        """
        tools = ReadRepositoryToolkit.create_instance(
            self.repo_client, self.source_repo_id, self.source_ref
        ).get_tools()
        tools += WebSearchToolkit.create_instance().get_tools()

        prompt = ChatPromptTemplate.from_messages([external_factor_plan_system, external_factor_plan_human])
        messages = prompt.format_messages(
            root_cause=state["root_cause"],
            repository_description=self.repo_config.repository_description,
            repository_structure=self.codebase_index.extract_tree(self.source_repo_id, self.source_ref),
        )

        react_agent = REACTAgent(
            run_name="pipeline_fixer_react_agent",
            tools=tools,
            model_name=GENERIC_COST_EFFICIENT_MODEL_NAME,
            fallback_model_name=GENERIC_PERFORMANT_MODEL_NAME,
            with_structured_output=ExternalFactorPlanOutput,
            store=store,
        )

        result = react_agent.agent.invoke({"messages": messages})

        return {"actions": cast("ExternalFactorPlanOutput", result["response"]).actions}

    def get_files_to_commit(self) -> list[FileChange]:
        """
        Get the files to commit.

        Returns:
            list[FileChange]: The files to commit.
        """
        if self.agent.store is None:
            return []
        return [
            cast("FileChange", item.value["data"])
            for item in self.agent.store.search(file_changes_namespace(self.source_repo_id, self.source_ref))
        ]
