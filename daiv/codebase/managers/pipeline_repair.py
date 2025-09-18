import logging
import re
from collections.abc import Iterable
from typing import Literal

from django.conf import settings

from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from automation.agents.pipeline_fixer.agent import PipelineFixerAgent
from automation.agents.pipeline_fixer.conf import settings as pipeline_fixer_settings
from automation.agents.pipeline_fixer.schemas import TroubleshootingDetail
from automation.agents.pipeline_fixer.templates import (
    PIPELINE_FIXER_REPAIR_PLAN_APPLIED_TEMPLATE,
    PIPELINE_FIXER_REVIEW_PLAN_TEMPLATE,
    PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE,
)
from automation.utils import get_file_changes
from codebase.base import ClientType, MergeRequestDiff
from codebase.clients import RepoClient
from codebase.managers.base import BaseManager
from core.constants import BOT_NAME
from core.utils import generate_uuid

logger = logging.getLogger("daiv.managers")


class PipelineRepairManager(BaseManager):
    """
    Manages the pipeline fix process.
    """

    def __init__(
        self,
        repo_id: str,
        ref: str,
        merge_request_id: int,
        job_id: int,
        job_name: str,
        discussion_id: str | None = None,
    ):
        super().__init__(RepoClient.create_instance(), repo_id, ref, discussion_id)
        self.thread_id = generate_uuid(f"{repo_id}{merge_request_id}{discussion_id}")
        self.merge_request_id = merge_request_id
        self.job_id = job_id
        self.job_name = job_name

    @classmethod
    async def plan_fix(
        cls, repo_id: str, ref: str, merge_request_id: int, job_id: int, job_name: str, discussion_id: str | None = None
    ):
        """
        Process pipeline fix for a job.

        Args:
            repo_id: The repository ID
            ref: The source reference
            merge_request_id: The merge request ID
            job_id: The job ID to process
            job_name: The job name
            discussion_id: The discussion ID that triggered the pipeline fix (optional)
        """
        manager = cls(repo_id, ref, merge_request_id, job_id, job_name, discussion_id)

        try:
            await manager._plan_fix()
        except Exception:
            logger.exception("Error processing pipeline fix for job '%s[%s]:%d'.", repo_id, ref, merge_request_id)
            manager._add_unable_to_process_job_note()

    @classmethod
    async def execute_fix(
        cls, repo_id: str, ref: str, merge_request_id: int, job_id: int, job_name: str, discussion_id: str | None = None
    ):
        """
        Execute the pipeline fix for a job.
        """
        manager = cls(repo_id, ref, merge_request_id, job_id, job_name, discussion_id)

        try:
            await manager._execute_fix()
        except Exception:
            logger.exception("Error executing pipeline fix for job '%s[%s]:%d'.", repo_id, ref, merge_request_id)
            manager._add_unable_to_process_job_note()

    async def _plan_fix(self):
        """
        Plan the pipeline fix for a job.
        """
        async with AsyncPostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
            pipeline_fixer = await PipelineFixerAgent.get_runnable(
                checkpointer=checkpointer, store=self._file_changes_store
            )
            current_state = await pipeline_fixer.aget_state(self._config, subgraphs=True)

            if not current_state.next and current_state.created_at is None:
                log_trace = self._clean_logs(self.client.job_log_trace(self.repo_id, self.job_id))
                diffs = self.client.get_merge_request_diff(self.repo_id, self.merge_request_id)

                async for event in pipeline_fixer.astream_events(
                    {"diff": self._merge_request_diffs_to_str(diffs), "job_logs": log_trace, "need_manual_fix": False},
                    self._config,
                    include_names=["troubleshoot", "plan_and_execute"],
                    include_types=["on_chain_start"],
                ):
                    if event["event"] == "on_chain_start":
                        self._add_workflow_step_note(event["name"], planning=True)

                current_state = await pipeline_fixer.aget_state(self._config, subgraphs=True)

                # This is not supposed to happen, but just in case we need to commit the changes to avoid lost work.
                if file_changes := await get_file_changes(self._file_changes_store):
                    await self._commit_changes(file_changes=file_changes, thread_id=self.thread_id)

                manual_fix_details = [
                    troubleshooting_detail
                    for troubleshooting_detail in current_state.values.get("troubleshooting")
                    if troubleshooting_detail.category != "codebase"
                ]

                if current_state.values.get("need_manual_fix", False) and manual_fix_details:
                    self._add_manual_fix_note(manual_fix_details)

                elif "plan_and_execute" in current_state.next and current_state.interrupts:
                    self._add_plan_review_note(current_state.interrupts[0].value, manual_fix_details=manual_fix_details)

    async def _execute_fix(self):
        """
        Execute the pipeline fix for a job.
        """
        async with AsyncPostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
            pipeline_fixer = await PipelineFixerAgent.get_runnable(
                checkpointer=checkpointer, store=self._file_changes_store
            )

            current_state = await pipeline_fixer.aget_state(self._config, subgraphs=True)

            if "plan_and_execute" in current_state.next and current_state.interrupts:
                async for event in pipeline_fixer.astream_events(
                    Command(resume="Plan approved"),
                    self._config,
                    include_names=["plan_and_execute", "apply_format_code"],
                    include_types=["on_chain_start"],
                ):
                    if event["event"] == "on_chain_start":
                        self._add_workflow_step_note(event["name"])

                if file_changes := await get_file_changes(self._file_changes_store):
                    self._add_workflow_step_note("commit_changes")
                    await self._commit_changes(file_changes=file_changes, thread_id=self.thread_id)
                    self._add_repair_plan_applied_note()

            elif current_state.values.get("need_manual_fix") and (
                manual_fix_details := [
                    troubleshooting_detail
                    for troubleshooting_detail in current_state.values.get("troubleshooting")
                    if troubleshooting_detail.category != "codebase"
                ]
            ):
                self._add_manual_fix_note(manual_fix_details)
            elif not current_state.next and current_state.created_at:
                self._add_repair_plan_applied_note()
            else:
                self._add_no_plan_to_execute_note()

    @property
    def _config(self):
        """
        Get the config for the agent.
        """
        return RunnableConfig(
            tags=[pipeline_fixer_settings.NAME, str(self.client.client_slug)],
            metadata={"merge_request_id": self.merge_request_id, "job_id": self.job_id},
            configurable={
                "thread_id": self.thread_id,
                "source_repo_id": self.repo_id,
                "source_ref": self.ref,
                "job_name": self.job_name,
                "discussion_id": self.discussion_id,
                "bot_username": self.client.current_user.username,
            },
        )

    def _clean_logs(self, log: str):
        """
        Clean the logs by removing irrelevant information.

        Args:
            log: The logs to clean

        Returns:
            Cleaned logs
        """
        if self.client.client_slug == ClientType.GITLAB:
            return self._extract_last_command_from_gitlab_logs(self._clean_gitlab_logs(log))
        return log

    def _clean_gitlab_logs(self, log: str):
        """
        Clean GitLab CI/CD job logs by removing irrelevant information.

        Args:
            log: Raw GitLab CI/CD job logs to clean

        Returns:
            Cleaned GitLab CI/CD job logs
        """
        # Replace Windows line endings with Unix line endings
        content = log.replace("\r\n", "\n")
        # Replace carriage return with newline
        content = content.replace("\r", "\n")

        # Replace section start and end markers
        content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]section_start:[0-9]*:\s*", r">>> ", content)
        content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]section_end:[0-9]*:\s*", r"<<< ", content)
        content = re.sub(r"section_end:[0-9]*:\s*", r"<<< ", content)

        # Remove ANSI escape codes
        content = re.sub(r"\x1B\[[0-9;]*[a-zA-Z]", "", content)

        return content

    def _extract_last_command_from_gitlab_logs(self, log: str) -> str:
        """
        Extract the output of the last executed command from the log.
        We assume that the last command is the one that failed.

        Args:
            log: Full log containing multiple commands and outputs

        Returns:
            Output of the last executed command or an empty string if no command was found
        """
        lines = log.split("\n$")
        if lines and (text := lines[-1].strip()):
            # Extract only the step_script output to avoid including other steps outputs leading to LLM hallucinations.
            # Also, add the last line to the command because it's where tipically the exit code is displayed.
            return f"$ {text.partition('<<< step_script')[0].strip()}\n{text.split('\n')[-1]}"
        return ""

    def _merge_request_diffs_to_str(self, diffs: Iterable[MergeRequestDiff]) -> str:
        """
        Convert merge request diffs to a string.

        Args:
            diffs: The merge request diffs

        Returns:
            The merge request diffs as a string
        """
        return "\n".join([mr_diff.diff.decode() for mr_diff in diffs if mr_diff.diff])

    def _add_unable_to_process_job_note(self):
        """
        Add a note to the discussion that the job could not be processed.
        """
        self._create_or_update_comment(
            "❌ We were unable to process the job and fix the pipeline. **Please check the logs** and try again."
        )

    def _add_repair_plan_applied_note(self):
        """
        Add a note to the discussion that the repair plan was applied.
        """
        self._create_or_update_comment(
            jinja2_formatter(PIPELINE_FIXER_REPAIR_PLAN_APPLIED_TEMPLATE, job_name=self.job_name)
        )

    def _add_manual_fix_note(self, troubleshooting: list[dict]):
        """
        Add a note to the discussion that the pipeline needs manual fix.

        Args:
            troubleshooting: The troubleshooting details to be added to the note
        """
        note_message = jinja2_formatter(
            PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE,
            troubleshooting_details=troubleshooting,
            job_name=self.job_name,
            bot_name=BOT_NAME,
            show_warning=True,
        )
        self._create_or_update_comment(note_message)

    def _add_plan_review_note(self, state: dict, manual_fix_details: list[TroubleshootingDetail]):
        """
        Add a note to the discussion that the plan is ready for review.

        Args:
            plan_tasks: The plan tasks to be added to the note
        """
        manual_fix_template = ""
        if manual_fix_details:
            manual_fix_template = jinja2_formatter(
                PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE,
                troubleshooting_details=manual_fix_details,
                job_name=self.job_name,
                bot_name=BOT_NAME,
                show_warning=False,
            )
        note_message = jinja2_formatter(
            PIPELINE_FIXER_REVIEW_PLAN_TEMPLATE,
            plan_tasks=state.get("plan_tasks", []),
            manual_fix_template=manual_fix_template,
        )
        self._create_or_update_comment(note_message)

    def _add_no_plan_to_execute_note(self):
        """
        Add a note to the merge request to inform the user that the plan could not be executed.
        """
        self._create_or_update_comment("ℹ️ No pending automatic repair plan to apply.")

    def _add_workflow_step_note(
        self,
        step_name: Literal["troubleshoot", "plan_and_execute", "apply_format_code", "commit_changes"],
        planning: bool = False,
    ):
        """
        Add a note to the discussion that the workflow step is in progress.

        Args:
            step_name: The name of the step
            planning: Whether the step is planning or executing
        """
        if step_name == "troubleshoot":
            note_message = f"🔍 Reviewing `{self.job_name}` job logs and recent changes — *in progress* ..."
        elif step_name == "plan_and_execute":
            if planning:
                note_message = f"🛠️ Drafting repair plan to fix `{self.job_name}` job — *in progress* ..."
            else:
                note_message = f"🛠️ Applying repair plan to fix `{self.job_name}` job — *in progress* ..."
        elif step_name == "apply_format_code":
            note_message = "🎨 Formatting code — *in progress* ..."
        elif step_name == "commit_changes":
            note_message = "💾 Committing code changes — *in progress* ..."

        self._create_or_update_comment(note_message)

    def _create_or_update_comment(self, note_message: str):
        """
        Create or update a comment on the issue.
        """
        if self._comment_id is not None:
            self.client.update_merge_request_discussion_note(
                self.repo_id, self.merge_request_id, self.discussion_id, self._comment_id, note_message
            )
        else:
            self._comment_id = self.client.create_merge_request_discussion_note(
                self.repo_id,
                self.merge_request_id,
                note_message,
                discussion_id=self.discussion_id,
                mark_as_resolved=True,
            )
