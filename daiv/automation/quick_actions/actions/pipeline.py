import textwrap
from enum import StrEnum

from langchain_core.prompts.string import jinja2_formatter

from automation.agents.pipeline_fixer.templates import PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE
from automation.quick_actions.base import QuickAction, Scope
from automation.quick_actions.decorator import quick_action
from codebase.api.models import Issue, MergeRequest, Note, User
from codebase.clients import RepoClient
from codebase.managers.pipeline_fixer import PipelineFixerManager

QUICK_ACTION_VERB = "pipeline"


class Action(StrEnum):
    FIX = "Try to fix the pipeline of the merge request."


@quick_action(verb=QUICK_ACTION_VERB, scopes=[Scope.MERGE_REQUEST])
class PipelineAction(QuickAction):
    """
    Actions related to the pipeline of a merge request.
    """

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
        return "\n".join([f" * `@{username} {cls.verb} {action.name.lower()}` - {action.value}" for action in Action])

    async def execute(
        self,
        repo_id: str,
        scope: Scope,
        note: Note,
        user: User,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        args: list[str] | None = None,
    ) -> None:
        """
        Execute the pipeline action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            note: The note data that triggered the action.
            user: The user who triggered the action.
            issue: The issue data.
            merge_request: The merge request data.
            args: Additional parameters from the command.
        """
        client = RepoClient.create_instance()

        if not args or args[0].lower() not in [action.name.lower() for action in Action]:
            client.create_merge_request_discussion_note(
                repo_id,
                merge_request.iid,
                self._invalid_action_message(client.current_user.username, args and args[0] or None),
                note.discussion_id,
            )
            client.resolve_merge_request_discussion(repo_id, merge_request.iid, note.discussion_id)
            return

        if Action.FIX.name.lower() == args[0].lower():
            pipeline = client.get_merge_request_latest_pipeline(repo_id, merge_request.iid)
            if pipeline.status == "failed":
                try:
                    failed_job = next(
                        job
                        for job in pipeline.jobs
                        if job.status == "failed" and not job.allow_failure and job.failure_reason == "script_failure"
                    )
                except StopIteration:
                    client.create_merge_request_discussion_note(
                        repo_id,
                        merge_request.iid,
                        jinja2_formatter(PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE, pipeline_url=pipeline.web_url),
                        note.discussion_id,
                    )
                    client.resolve_merge_request_discussion(repo_id, merge_request.iid, note.discussion_id)
                    return

                await PipelineFixerManager.process_job(
                    repo_id,
                    merge_request.source_branch,
                    merge_request.iid,
                    job_id=failed_job.id,
                    job_name=failed_job.name,
                    discussion_id=note.discussion_id,
                )
            else:
                client.create_merge_request_discussion_note(
                    repo_id, merge_request.iid, "ℹ️ The pipeline is not failing. No fix needed.", note.discussion_id
                )
                client.resolve_merge_request_discussion(repo_id, merge_request.iid, note.discussion_id)

    def _invalid_action_message(self, username: str, invalid_action: str | None) -> str:
        """
        Get the help message for the plan action.
        """
        return textwrap.dedent(
            f"""\
            ❌ The action `{invalid_action or "no action"}` is not valid.

            The available actions for the `{QUICK_ACTION_VERB}` are as follows:
            """
        ) + self.help(username)
