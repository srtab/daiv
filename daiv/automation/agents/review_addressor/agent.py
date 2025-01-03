import logging
from typing import Literal, cast

from langchain_core.messages import SystemMessage
from langchain_core.output_parsers.openai_tools import PydanticToolsParser
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from automation.agents import CODING_PERFORMANT_MODEL_NAME, BaseAgent
from automation.agents.base import (
    CODING_COST_EFFICIENT_MODEL_NAME,
    GENERIC_PERFORMANT_MODEL_NAME,
    PLANING_PERFORMANT_MODEL_NAME,
)
from automation.agents.prebuilt import REACTAgent
from automation.agents.prompts import execute_plan_human, execute_plan_system
from automation.agents.schemas import AskForClarification, AssesmentClassificationResponse
from automation.constants import DEFAULT_RECURSION_LIMIT
from automation.tools.sandbox import RunSandboxCommandsTool
from automation.tools.toolkits import ReadRepositoryToolkit, SandboxToolkit, WebSearchToolkit, WriteRepositoryToolkit
from automation.utils import file_changes_namespace
from codebase.base import FileChange
from codebase.clients import AllRepoClient
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig

from .prompts import respond_reviewer_system, review_analyzer_plan, review_assessment_system
from .schemas import DetermineNextActionResponse, RespondReviewerResponse
from .state import OverallState

logger = logging.getLogger("daiv.agents")


class ReviewAddressorAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

    def __init__(
        self,
        repo_client: AllRepoClient,
        *,
        source_repo_id: str,
        source_ref: str,
        merge_request_id: int,
        discussion_id: str,
        file_changes: list[FileChange] | None = None,
        **kwargs,
    ):
        self.repo_client = repo_client
        self.source_repo_id = source_repo_id
        self.source_ref = source_ref
        self.merge_request_id = merge_request_id
        self.discussion_id = discussion_id
        self.repo_config = RepositoryConfig.get_config(self.source_repo_id)
        self.codebase_index = CodebaseIndex(self.repo_client)
        self.file_changes = file_changes or []
        super().__init__(**kwargs)

    def get_config(self) -> RunnableConfig:
        """
        Include the metadata identifying the source repository, reference, merge request, and discussion.

        Returns:
            dict: The configuration for the agent.
        """
        config = super().get_config()
        config["tags"].append(self.repo_client.client_slug)
        config["metadata"].update({
            "repo_client": self.repo_client.client_slug,
            "source_repo_id": self.source_repo_id,
            "source_ref": self.source_ref,
            "merge_request_id": self.merge_request_id,
            "discussion_id": self.discussion_id,
        })
        return config

    def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.

        Returns:
            CompiledStateGraph: The compiled workflow.
        """
        workflow = StateGraph(OverallState)

        workflow.add_node("assessment", self.assessment)
        workflow.add_node("plan", self.plan)
        workflow.add_node("execute_plan", self.execute_plan)
        workflow.add_node("apply_lint_fix", self.apply_lint_fix)
        workflow.add_node("respond_to_reviewer", self.respond_to_reviewer)
        workflow.add_node("human_feedback", self.human_feedback)

        workflow.add_edge(START, "assessment")
        workflow.add_conditional_edges("assessment", self.continue_planning)
        workflow.add_edge("human_feedback", "plan")
        workflow.add_conditional_edges("plan", self.continue_executing)
        workflow.add_conditional_edges(
            "execute_plan",
            self.determine_if_lint_fix_should_be_applied,
            {"apply_lint_fix": "apply_lint_fix", "end": END},
        )
        workflow.add_edge("apply_lint_fix", END)
        workflow.add_edge("respond_to_reviewer", END)

        in_memory_store = InMemoryStore()

        # Pre-populate the store with file changes uncommitted yet.
        for file_change in self.file_changes:
            in_memory_store.put(
                file_changes_namespace(self.source_repo_id, self.source_ref),
                file_change.file_path,
                {"data": file_change, "action": file_change.action},
            )

        return workflow.compile(
            checkpointer=self.checkpointer, interrupt_before=["human_feedback"], store=in_memory_store
        )

    def assessment(self, state: OverallState):
        """
        Assess the feedback provided by the reviewer.

        This node will help distinguish if the comments are requests to change the code or just feedback and
        define the next steps to follow.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(review_assessment_system),
            MessagesPlaceholder("comments"),
        ])

        evaluator = (
            prompt
            # We could use `with_structured_output` but it define tool_choice as "any", forcing the llm to respond with
            # a tool call without reasoning, which is crucial here to make the right decision.
            # Defining tool_choice as "auto" would let the llm to reason before calling the tool.
            | self.model.bind_tools([AssesmentClassificationResponse], tool_choice="auto")
            | PydanticToolsParser(tools=[AssesmentClassificationResponse], first_tool_only=True)
        )

        response = cast(
            "AssesmentClassificationResponse",
            evaluator.invoke(
                {"comments": state["messages"]}, config={"configurable": {"model": CODING_COST_EFFICIENT_MODEL_NAME}}
            ),
        )
        return {"request_for_changes": response.request_for_changes}

    def plan(self, state: OverallState, store: BaseStore):
        """
        Plan the steps to follow.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to save the state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        tools = ReadRepositoryToolkit.create_instance(
            self.repo_client, self.source_repo_id, self.source_ref
        ).get_tools()
        tools += WebSearchToolkit.create_instance().get_tools()
        if self.repo_config.commands.enabled():
            tools += SandboxToolkit.create_instance().get_tools()

        system_message_template = SystemMessagePromptTemplate.from_template(review_analyzer_plan, "jinja2")
        system_message = system_message_template.format(
            diff=state.get("diff"),
            project_description=self.repo_config.repository_description,
            repository_structure=self.codebase_index.extract_tree(self.source_repo_id, self.source_ref),
            tools=[tool.name for tool in tools],
            recursion_limit=DEFAULT_RECURSION_LIMIT,
        )

        react_agent = REACTAgent(
            run_name="plan_react_agent",
            tools=tools,
            model_name=PLANING_PERFORMANT_MODEL_NAME,
            fallback_model_name=GENERIC_PERFORMANT_MODEL_NAME,
            with_structured_output=DetermineNextActionResponse,
            store=store,
        )
        response = react_agent.agent.invoke(
            {"messages": [system_message] + state["messages"]}, config={"recursion_limit": DEFAULT_RECURSION_LIMIT}
        )

        if "response" not in response:
            return {"response": "I couldn't address the review for you this time. Leave a comment so i can try again."}

        if isinstance(response["response"].action, AskForClarification):
            return {"response": "\n".join(response["response"].action.questions)}

        return {
            "plan_tasks": response["response"].action.tasks,
            "goal": response["response"].action.goal,
            "show_diff_hunk_to_executor": response["response"].action.show_diff_hunk_to_executor,
        }

    def execute_plan(self, state: OverallState, store: BaseStore):
        """
        Execute the plan by making the necessary changes to the codebase.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to save the state of the agent.

        Returns:
            dict: The state of the agent to update.
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
            HumanMessagePromptTemplate.from_template(execute_plan_human, "jinja2"),
        ])
        messages = prompt.format_messages(
            goal=state["goal"],
            plan_tasks=enumerate(state["plan_tasks"]),
            diff=state["diff"],
            show_diff_hunk_to_executor=state["show_diff_hunk_to_executor"],
            project_description=self.repo_config.repository_description,
            repository_structure=self.codebase_index.extract_tree(self.source_repo_id, self.source_ref),
        )

        react_agent = REACTAgent(
            run_name="execute_plan_react_agent",
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
        Determine whether the lint fix should be applied after the plan has been executed.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to use for caching.

        Returns:
            Literal["apply_lint_fix", "end"]: The next step in the workflow.
        """
        return (
            "apply_lint_fix"
            if self.repo_config.commands.enabled()
            and store.search(file_changes_namespace(self.source_repo_id, self.source_ref), limit=1)
            else "end"
        )

    def apply_lint_fix(self, state: OverallState, store: BaseStore):
        """
        Apply lint fix to the file changes made by the agent.

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

    def respond_to_reviewer(self, state: OverallState, store: BaseStore):
        """
        Respond to reviewer's comments if no changes requested or if planning rises questions.

        Args:
            state (OverallState): The state of the agent.
            store (BaseStore): The store to save the state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        tools = ReadRepositoryToolkit.create_instance(
            self.repo_client, self.source_repo_id, self.source_ref
        ).get_tools()
        tools += WebSearchToolkit.create_instance().get_tools()
        if self.repo_config.commands.enabled():
            tools += SandboxToolkit.create_instance().get_tools()

        system_message_template = SystemMessagePromptTemplate.from_template(
            respond_reviewer_system, "jinja2", additional_kwargs={"cache-control": {"type": "ephemeral"}}
        )
        system_message = system_message_template.format(
            diff=state["diff"],
            project_description=self.repo_config.repository_description,
            repository_structure=self.codebase_index.extract_tree(self.source_repo_id, self.source_ref),
        )

        react_agent = REACTAgent(
            run_name="respond_reviewer_react_agent",
            tools=tools,
            model_name=CODING_PERFORMANT_MODEL_NAME,
            fallback_model_name=GENERIC_PERFORMANT_MODEL_NAME,
            with_structured_output=RespondReviewerResponse,
            store=store,
        )
        result = react_agent.agent.invoke({"messages": [system_message] + state["messages"]})
        return {"response": cast("RespondReviewerResponse", result["response"]).response}

    def human_feedback(self, state: OverallState):
        """
        Request human feedback to address the reviewer's comments.

        Args:
            state (OverallState): The state of the agent.
        """

    def continue_planning(self, state: OverallState) -> Literal["plan", "respond_to_reviewer"]:
        """
        Check if the agent should continue planning or provide/request human feedback.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "request_for_changes" in state and state["request_for_changes"]:
            return "plan"
        return "respond_to_reviewer"

    def continue_executing(self, state: OverallState) -> Literal["execute_plan", "human_feedback"]:
        """
        Check if the agent should continue executing the plan or request human feedback

        Args:
            state (OverallState): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        if "response" in state and state["response"]:
            return "human_feedback"
        return "execute_plan"

    def get_files_to_commit(self) -> list[FileChange]:
        if self.agent.store is None:
            return []
        return [
            cast("FileChange", item.value["data"])
            for item in self.agent.store.search(file_changes_namespace(self.source_repo_id, self.source_ref))
        ]
