import textwrap

from langchain_core.prompts.string import jinja2_formatter

from automation.agents.pipeline_fixer.templates import PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE
from automation.quick_actions.base import BaseAction, QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from codebase.base import Discussion, Issue, Job, MergeRequest, Note, Pipeline
from codebase.clients import RepoClient
from codebase.managers.pipeline_fixer import PipelineFixerManager

QUICK_ACTION_VERB = "pipeline"


class Action(BaseAction):
    FIX = "Define a plan to fix the failed pipeline."
    FIX_EXECUTE = "Execute the defined plan to fix the pipeline."


@quick_action(verb=QUICK_ACTION_VERB, scopes=[Scope.MERGE_REQUEST])
class PipelineQuickAction(QuickAction):
    """
    Actions related to the pipeline of a merge request.
    """

    can_reply = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = RepoClient.create_instance()

    @staticmethod
    def description() -> str:
        """
        Get the description of the pipeline action.
        """
        return "Actions related to the pipeline of a merge request."

    @classmethod
    def help(cls, username: str) -> str:
        """
        Get the help message for the pipeline action.
        """
        return "\n".join([
            f" * `@{username} {cls.verb} {Action.get_name(action)}` - {action.value}" for action in Action
        ])

    async def execute(
        self,
        repo_id: str,
        *,
        scope: Scope,
        discussion: Discussion,
        note: Note,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        args: str | None = None,
    ) -> None:
        """
        Execute the pipeline action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            discussion: The discussion that triggered the action.
            note: The note that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            merge_request: The merge request where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        if not args or not self._validate_action(args, discussion):
            self._add_invalid_action_message(
                repo_id, merge_request.merge_request_id, discussion.id, note.author.username, args or None
            )
            return

        pipeline = self.client.get_merge_request_latest_pipeline(repo_id, merge_request.merge_request_id)
        if pipeline.status == "failed":
            if not (failed_job := self._get_failed_job(pipeline)):
                self.client.create_merge_request_discussion_note(
                    repo_id,
                    merge_request.merge_request_id,
                    jinja2_formatter(PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE, pipeline_url=pipeline.web_url),
                    discussion.id,
                )
                self.client.resolve_merge_request_discussion(repo_id, merge_request.merge_request_id, discussion.id)
                return

            # Plan the fix if the action is the first note in the discussion
            if Action.get_name(Action.FIX) == args and len(discussion.notes) == 1:
                await PipelineFixerManager.plan_fix(
                    repo_id,
                    merge_request.source_branch,
                    merge_request.merge_request_id,
                    job_id=failed_job.id,
                    job_name=failed_job.name,
                    discussion_id=discussion.id,
                )
            elif Action.get_name(Action.FIX_EXECUTE) == args and len(discussion.notes) > 1:
                await PipelineFixerManager.execute_fix(
                    repo_id,
                    merge_request.source_branch,
                    merge_request.merge_request_id,
                    job_id=failed_job.id,
                    job_name=failed_job.name,
                    discussion_id=discussion.id,
                )
        else:
            self.client.create_merge_request_discussion_note(
                repo_id, merge_request.merge_request_id, "ℹ️ The pipeline is not failing. No fix needed.", discussion.id
            )
            self.client.resolve_merge_request_discussion(repo_id, merge_request.merge_request_id, discussion.id)

    def _get_failed_job(self, pipeline: Pipeline) -> Job | None:
        """
        Get the failed job from the pipeline.
        """
        try:
            return next(
                job
                for job in pipeline.jobs
                if job.status == "failed" and not job.allow_failure and job.failure_reason == "script_failure"
            )
        except StopIteration:
            return None

    def _add_invalid_action_message(
        self, repo_id: str, merge_request_iid: int, note_discussion_id: str, username: str, invalid_action: str | None
    ) -> None:
        """
        Add an invalid action message to the merge request discussion.
        """
        note_message = textwrap.dedent(
            f"""\
            ❌ The action `{invalid_action or "no action"}` is not valid.

            The available actions for the `{QUICK_ACTION_VERB}` are as follows:
            """
        ) + self.help(username)

        self.client.create_merge_request_discussion_note(repo_id, merge_request_iid, note_message, note_discussion_id)
        self.client.resolve_merge_request_discussion(repo_id, merge_request_iid, note_discussion_id)

    def _validate_action(self, action: str, discussion: Discussion) -> bool:
        """
        Validate the action is valid.
        """
        return action.lower() in [Action.get_name(action) for action in Action] and (
            # Need to be the first note in the discussion to plan the fix
            action == Action.get_name(Action.FIX)
            and len(discussion.notes) == 1
            # Need to be a reply to the first note in the discussion to execute the fix
            or action == Action.get_name(Action.FIX_EXECUTE)
            and len(discussion.notes) > 1
        )
