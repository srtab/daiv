from langchain_core.prompts.string import jinja2_formatter

from automation.agents.pipeline_fixer.templates import PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE
from automation.quick_actions.base import BaseAction, QuickAction, Scope, TriggerLocation
from automation.quick_actions.decorator import quick_action
from codebase.base import Discussion, Issue, Job, MergeRequest, Note, Pipeline
from codebase.managers.pipeline_repair import PipelineRepairManager


class RepairAction(BaseAction):
    trigger: str = "repair"
    description: str = "Suggest a repair plan to fix the failed pipeline."
    location: TriggerLocation = TriggerLocation.DISCUSSION


class RepairApplyAction(BaseAction):
    trigger: str = "repair apply"
    description: str = "Apply the repair plan to fix the pipeline."
    location: TriggerLocation = TriggerLocation.REPLY


@quick_action(verb="pipeline", scopes=[Scope.MERGE_REQUEST])
class PipelineQuickAction(QuickAction):
    """
    Actions related to the pipeline of a merge request.
    """

    actions = [RepairAction, RepairApplyAction]

    async def execute_action(
        self,
        repo_id: str,
        *,
        args: str,
        scope: Scope,
        discussion: Discussion,
        note: Note,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        is_reply: bool = False,
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
            is_reply: Whether the action was triggered as a reply.
        """
        pipeline = self.client.get_merge_request_latest_pipeline(repo_id, merge_request.merge_request_id)
        if pipeline is None or pipeline.status != "failed" or not (failed_job := self._get_failed_job(pipeline)):
            self.client.create_merge_request_discussion_note(
                repo_id,
                merge_request.merge_request_id,
                jinja2_formatter(
                    PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE, pipeline_url=pipeline and pipeline.web_url or ""
                ),
                discussion.id,
                mark_as_resolved=True,
            )
            return

        # Plan the fix if the action is the first note in the discussion
        if RepairAction.match(args or "", is_reply) and len(discussion.notes) == 1:
            await PipelineRepairManager.plan_fix(
                repo_id,
                merge_request.source_branch,
                merge_request.merge_request_id,
                job_id=failed_job.id,
                job_name=failed_job.name,
                discussion_id=discussion.id,
            )
        elif RepairApplyAction.match(args or "", is_reply) and len(discussion.notes) > 1:
            await PipelineRepairManager.execute_fix(
                repo_id,
                merge_request.source_branch,
                merge_request.merge_request_id,
                job_id=failed_job.id,
                job_name=failed_job.name,
                discussion_id=discussion.id,
            )

    def _get_failed_job(self, pipeline: Pipeline) -> Job | None:
        """
        Get the failed job from the pipeline.

        Args:
            pipeline: The pipeline to get the failed job from.

        Returns:
            Job | None: The failed job or None if no failed job is found.
        """
        try:
            return next(
                job
                for job in pipeline.jobs
                if job.status == "failed" and not job.allow_failure and job.failure_reason == "script_failure"
            )
        except StopIteration:
            return None
