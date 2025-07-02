import logging
import re
from collections.abc import Iterable

from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig

from automation.agents.pipeline_fixer.agent import PipelineFixerAgent
from automation.agents.pipeline_fixer.conf import settings as pipeline_fixer_settings
from automation.agents.pipeline_fixer.templates import PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE
from codebase.base import ClientType, MergeRequestDiff
from codebase.clients import RepoClient
from codebase.managers.base import BaseManager
from core.constants import BOT_NAME

logger = logging.getLogger("daiv.managers")


class PipelineFixerManager(BaseManager):
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
        super().__init__(RepoClient.create_instance(), repo_id, ref)
        self.merge_request_id = merge_request_id
        self.job_id = job_id
        self.job_name = job_name
        self.discussion_id = discussion_id

    @classmethod
    async def process_job(
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
            await manager._process_job()
        except Exception:
            logger.exception("Error processing pipeline fix for job '%s[%s]:%d'.", repo_id, ref, merge_request_id)
            manager._add_unable_to_process_job_note()

    async def _process_job(self):
        """
        Process pipeline fix for a job.
        """
        log_trace = self._clean_logs(self.client.job_log_trace(self.repo_id, self.job_id))
        diffs = self.client.get_merge_request_diff(self.repo_id, self.merge_request_id)

        config = RunnableConfig(
            tags=[pipeline_fixer_settings.NAME, str(self.client.client_slug)],
            metadata={"merge_request_id": self.merge_request_id, "job_id": self.job_id},
            configurable={
                "source_repo_id": self.repo_id,
                "source_ref": self.ref,
                "job_name": self.job_name,
                "discussion_id": self.discussion_id,
            },
        )

        pipeline_fixer = await PipelineFixerAgent(store=self._file_changes_store).agent

        result = await pipeline_fixer.ainvoke(
            {"diff": self._merge_request_diffs_to_str(diffs), "job_logs": log_trace, "need_manual_fix": False}, config
        )

        if file_changes := await self._get_file_changes():
            await self._commit_changes(file_changes=file_changes)
            self._add_pipeline_fixed_note()

        elif result and result.get("need_manual_fix", False) and (troubleshooting := result.get("troubleshooting")):
            self._add_manual_fix_note(troubleshooting)

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
        note_message = (
            "❌ We were unable to process the job and fix the pipeline. **Please check the logs** and try again."
        )
        if self.discussion_id:
            self.client.create_merge_request_discussion_note(
                self.repo_id, self.merge_request_id, note_message, discussion_id=self.discussion_id
            )
            self.client.resolve_merge_request_discussion(self.repo_id, self.merge_request_id, self.discussion_id)
        else:
            self.client.comment_merge_request(self.repo_id, self.merge_request_id, note_message)

    def _add_pipeline_fixed_note(self):
        """
        Add a note to the discussion that the pipeline was fixed.
        """
        note_message = "✅ Pipeline fix applied. 🔎 Please **review the changes** before merging."
        if self.discussion_id:
            self.client.create_merge_request_discussion_note(
                self.repo_id, self.merge_request_id, note_message, discussion_id=self.discussion_id
            )
            self.client.resolve_merge_request_discussion(self.repo_id, self.merge_request_id, self.discussion_id)
        else:
            self.client.comment_merge_request(self.repo_id, self.merge_request_id, note_message)

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
        )
        if self.discussion_id:
            self.client.create_merge_request_discussion_note(
                self.repo_id, self.merge_request_id, note_message, discussion_id=self.discussion_id
            )
            self.client.resolve_merge_request_discussion(self.repo_id, self.merge_request_id, self.discussion_id)
        else:
            self.client.comment_merge_request(self.repo_id, self.merge_request_id, note_message)
