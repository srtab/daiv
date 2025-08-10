import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langsmith import testing as t
from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.constants import ModelName
from automation.agents.plan_and_execute.agent import PlanAndExecuteAgent
from automation.agents.plan_and_execute.tools import FINALIZE_WITH_PLAN_NAME, FINALIZE_WITH_TARGETED_QUESTIONS_NAME

evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.O4_MINI, thinking_level=ThinkingLevel.MEDIUM),
)

config = {"configurable": {"thread_id": "1", "source_repo_id": "srtab/daiv", "source_ref": "main"}}


@pytest.mark.django_db
@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize(
    "messages,reference_outputs",
    [
        (
            [("human", "Create a quick action.")],
            {
                "plan_questions": """I found the existing quick actions framework in this DAIV repository that handles GitLab automation. To create a new quick action, I need these specific details:

**Action Definition**:
- What should the `verb` be? (users will type `@daiv <verb> <arguments>`)
- What functionality should this action provide?
- Brief `description` for the help message?

**Scope & Context**:
- Where should this work: `Issues`, `Merge Requests`, or both?
- Trigger location: `Discussion` (initial comments), `Reply` (follow-ups), or both?

**Implementation**:
- What specific operations should it perform? (e.g., create GitLab artifacts, call external APIs, run analysis, post formatted responses)
- Any required arguments or parameters from the user?
- What GitLab API calls or DAIV services should it integrate with?

**Examples based on existing patterns**:
- `@daiv help` - shows available actions
- `@daiv plan execute` - approves and runs issue plans
- `@daiv plan revise` - discards current plan and creates new one"""  # noqa: E501
            },
        )
    ],
)
async def test_plan_vague_requirements_correctness(messages, reference_outputs):
    """
    Test that the agent can handle vague requirements by asking targeted questions.
    """
    t.log_reference_outputs(reference_outputs)

    store = InMemoryStore()
    checkpointer = InMemorySaver()
    plan_and_execute = await PlanAndExecuteAgent(store=store, checkpointer=checkpointer).agent

    inputs = {"messages": messages}

    t.log_inputs(inputs)

    outputs = await plan_and_execute.ainvoke(inputs, config=config)

    assert "plan_questions" in outputs, (
        f"The agent should have called the `{FINALIZE_WITH_TARGETED_QUESTIONS_NAME}` tool"
    )

    t.log_outputs({"plan_questions": outputs["plan_questions"]})

    result = evaluator(
        inputs=inputs, outputs={"plan_questions": outputs["plan_questions"]}, reference_outputs=reference_outputs
    )
    assert result["score"] is True, result["comment"]


@pytest.mark.django_db
@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize(
    "messages,reference_outputs",
    [
        (
            [
                (
                    "human",
                    "Create a quick action that echoes the text provided by the user with markdown formatting, "
                    "that work on issues and merge requests.",
                )
            ],
            {
                "plan_tasks": [
                    {
                        "relevant_files": [
                            "daiv/automation/quick_actions/base.py",
                            "daiv/automation/quick_actions/decorator.py",
                            "daiv/automation/quick_actions/registry.py",
                            "daiv/automation/quick_actions/parser.py",
                            "daiv/automation/quick_actions/actions/help.py",
                            "daiv/automation/quick_actions/actions/plan.py",
                            "daiv/automation/quick_actions/templates.py",
                            "daiv/codebase/api/callbacks_gitlab.py",
                        ],
                        "file_path": "daiv/automation/quick_actions/actions/echo.py",
                        "details": 'Add a new quick action that echoes back the user-provided text (rendered by GitLab Markdown). This action must work for both Issues and Merge Requests.\n\nImplementation details (grounded in existing framework):\n- Follow the QuickAction patterns used in HelpQuickAction (see `daiv/automation/quick_actions/actions/help.py`) and registration via the `@quick_action` decorator (see `daiv/automation/quick_actions/decorator.py`).\n- `execute_quick_action_task` passes the arguments as a single string to `QuickAction.execute(..., args=...)` (see `daiv/automation/quick_actions/tasks.py` and `daiv/codebase/api/callbacks_gitlab.py`). We should echo that string directly so GitLab renders any Markdown the user included.\n- Validation: `QuickAction.execute` calls `validate_action(args, is_reply)` which consults `BaseAction.match` on each configured sub-action (see `daiv/automation/quick_actions/base.py`). For an echo feature, any non-empty args should be considered valid. Implement a `BaseAction` subclass whose `match` returns True iff `args.strip()` is non-empty and the trigger location matches (default BOTH).\n- Help text: The help listing (used by the existing `help` action) renders entries via `BaseAction.help` combining the action `verb` and sub-action `trigger` (see `HelpQuickAction` and `QuickAction.help`). For clarity, set the trigger display to `<text>` and update the description accordingly.\n- Posting the echo: Use the same RepoClient methods used elsewhere to add a discussion note depending on scope (see calls in `HelpQuickAction.execute_action` and in error handling in `daiv/automation/quick_actions/tasks.py`). Do not pass `mark_as_resolved` by default (error path also omits it), so we don’t auto-resolve discussions when echoing.\n\nCreate the file with the following contents:\n~~~python\nfrom automation.quick_actions.base import BaseAction, QuickAction, Scope, TriggerLocation\nfrom automation.quick_actions.decorator import quick_action\nfrom codebase.base import Discussion, Issue, MergeRequest, Note\n\n\nclass EchoAction(BaseAction):\n    # Displayed in help as: `@<bot> echo <text>`\n    trigger: str = "<text>"\n    description: str = "Echo back the provided text with Markdown rendering."\n    location: TriggerLocation = TriggerLocation.BOTH\n\n    @classmethod\n    def match(cls, action: str, is_reply: bool = False) -> bool:\n        # Accept any non-empty text after the verb; respect location rules\n        return bool((action or "").strip()) and cls.match_location(is_reply)\n\n\n@quick_action(verb="echo", scopes=[Scope.ISSUE, Scope.MERGE_REQUEST])\nclass EchoQuickAction(QuickAction):\n    """Echo the user-provided text right back into the discussion."""\n\n    actions = [EchoAction]\n\n    async def execute_action(\n        self,\n        repo_id: str,\n        *,\n        args: str,\n        scope: Scope,\n        discussion: Discussion,\n        note: Note,\n        issue: Issue | None = None,\n        merge_request: MergeRequest | None = None,\n        is_reply: bool = False,\n    ) -> None:\n        # GitLab will render Markdown in notes; echo the args verbatim.\n        message = args\n\n        if scope == Scope.ISSUE:\n            assert issue is not None\n            self.client.create_issue_discussion_note(repo_id, issue.iid, message, discussion.id)\n        elif scope == Scope.MERGE_REQUEST:\n            assert merge_request is not None\n            self.client.create_merge_request_discussion_note(\n                repo_id, merge_request.merge_request_id, message, discussion.id\n            )\n~~~',  # NOQA: E501
                    },
                    {
                        "relevant_files": ["daiv/automation/quick_actions/actions/__init__.py"],
                        "file_path": "daiv/automation/quick_actions/actions/__init__.py",
                        "details": 'Ensure the new Echo quick action is imported so that the `@quick_action` decorator runs at import time and registers it in the registry used by callbacks and tasks (see `quick_action_registry` usage in `daiv/codebase/api/callbacks_gitlab.py` and `daiv/automation/quick_actions/tasks.py`).\n\n- Add import and export entries to this module to mirror existing actions.\n\nEdit the file to include:\n~~~python\nfrom .echo import EchoQuickAction\n\n__all__ = [\n    "HelpQuickAction",\n    "PlanQuickAction",\n    "PipelineQuickAction",\n    "EchoQuickAction",\n]\n~~~\n\nNote: Keep the existing imports (`from .help import HelpQuickAction`, `from .pipeline import PipelineQuickAction`, `from .plan import PlanQuickAction`) and append the echo import; update `__all__` to include the echo symbol.',  # NOQA: E501
                    },
                    {
                        "relevant_files": [
                            "tests/automation/quick_actions/test_actions.py",
                            "tests/automation/quick_actions/test_base.py",
                            "tests/automation/quick_actions/test_tasks.py",
                        ],
                        "file_path": "tests/automation/quick_actions/test_echo_action.py",
                        "details": 'Add unit tests for the new Echo quick action, following the style of `tests/automation/quick_actions/test_actions.py` (which tests HelpQuickAction) and exercising both Issue and Merge Request scopes. Also verify behavior when no text is provided (invalid action path uses the UNKNOWN_QUICK_ACTION_TEMPLATE from `daiv/automation/quick_actions/templates.py`).\n\nCreate the file with tests:\n~~~python\nfrom unittest.mock import MagicMock\n\nfrom automation.quick_actions.actions.echo import EchoQuickAction\nfrom automation.quick_actions.base import Scope\n\n\nclass TestEchoAction:\n    def setup_method(self):\n        self.action = EchoQuickAction()\n        # Mock client and minimal objects\n        self.action.client = MagicMock(current_user=MagicMock(username="bot"))\n\n        self.mock_note = MagicMock()\n        self.mock_note.id = 1\n\n        self.mock_discussion = MagicMock()\n        self.mock_discussion.id = "disc-1"\n        self.mock_discussion.notes = [self.mock_note]\n\n        self.mock_issue = MagicMock()\n        self.mock_issue.iid = 101\n\n        self.mock_mr = MagicMock()\n        self.mock_mr.merge_request_id = 202\n\n    def test_attributes(self):\n        assert hasattr(EchoQuickAction, "verb")\n        assert EchoQuickAction.verb == "echo"\n        assert Scope.ISSUE in EchoQuickAction.scopes\n        assert Scope.MERGE_REQUEST in EchoQuickAction.scopes\n\n    async def test_execute_on_issue(self):\n        msg = "Hello **world**"\n        await self.action.execute(\n            repo_id="repo/x",\n            args=msg,\n            scope=Scope.ISSUE,\n            discussion=self.mock_discussion,\n            note=self.mock_note,\n            issue=self.mock_issue,\n        )\n        self.action.client.create_issue_discussion_note.assert_called_once_with(\n            "repo/x", 101, msg, "disc-1"\n        )\n\n    async def test_execute_on_merge_request(self):\n        msg = "- list\\n- items"\n        await self.action.execute(\n            repo_id="repo/x",\n            args=msg,\n            scope=Scope.MERGE_REQUEST,\n            discussion=self.mock_discussion,\n            note=self.mock_note,\n            merge_request=self.mock_mr,\n        )\n        self.action.client.create_merge_request_discussion_note.assert_called_once_with(\n            "repo/x", 202, msg, "disc-1"\n        )\n\n    async def test_invalid_when_no_text(self):\n        # With empty args, EchoAction.match returns False, triggering the invalid action message path\n        await self.action.execute(\n            repo_id="repo/x",\n            args="",\n            scope=Scope.ISSUE,\n            discussion=self.mock_discussion,\n            note=self.mock_note,\n            issue=self.mock_issue,\n        )\n        # A note must still be posted with the unknown quick-action template\n        assert self.action.client.create_issue_discussion_note.called\n        posted_message = self.action.client.create_issue_discussion_note.call_args[0][2]\n        assert "Unknown Quick-Action" in posted_message\n        assert "echo" in posted_message\n~~~\n\nNotes for reviewers:\n- These tests mirror how `HelpQuickAction` is tested (see `tests/automation/quick_actions/test_actions.py`) by mocking the RepoClient attached to the action instance.\n- We assert the exact RepoClient method and arguments, consistent with how other actions post discussion notes.',  # NOQA: E501
                    },
                ]
            },
        )
    ],
)
async def test_plan_complete_requirements_correctness(messages, reference_outputs):
    """
    Test that the agent can handle complete requirements by planning the action.
    """
    t.log_reference_outputs(reference_outputs)

    store = InMemoryStore()
    checkpointer = InMemorySaver()
    plan_and_execute = await PlanAndExecuteAgent(store=store, checkpointer=checkpointer).agent

    inputs = {"messages": messages}

    t.log_inputs(inputs)

    outputs = await plan_and_execute.ainvoke(inputs, config=config)

    assert "plan_questions" not in outputs, (
        f"The agent called the `{FINALIZE_WITH_TARGETED_QUESTIONS_NAME}` tool "
        f"instead of the `{FINALIZE_WITH_PLAN_NAME}` tool"
    )
    assert "plan_tasks" in outputs, f"The agent should have called the `{FINALIZE_WITH_PLAN_NAME}` tool"

    json_serializable_outputs = {"plan_tasks": [task.model_dump(mode="json") for task in outputs["plan_tasks"]]}

    t.log_outputs(json_serializable_outputs)

    result = evaluator(inputs=inputs, outputs=json_serializable_outputs, reference_outputs=reference_outputs)
    assert result["score"] is True, result["comment"]
