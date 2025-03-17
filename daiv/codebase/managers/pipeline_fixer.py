import re
from collections.abc import Iterable

from django.conf import settings

from langchain_core.prompts.string import jinja2_formatter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver

from automation.agents.pipeline_fixer.agent import PipelineFixerAgent
from automation.agents.pipeline_fixer.conf import settings as pipeline_fixer_settings
from automation.agents.pipeline_fixer.templates import PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE
from codebase.base import ClientType, MergeRequestDiff
from codebase.clients import AllRepoClient, RepoClient
from codebase.managers.base import BaseManager
from core.constants import BOT_NAME
from core.utils import generate_uuid


class PipelineFixerManager(BaseManager):
    """
    Manages the pipeline fix process.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str, **kwargs):
        super().__init__(client, repo_id, ref)
        self.thread_id = kwargs["thread_id"]

    @classmethod
    def process_job(cls, repo_id: str, ref: str, merge_request_id: int, job_id: int, job_name: str):
        """
        Process pipeline fix for a job.

        Args:
            repo_id: The repository ID
            ref: The source reference
            merge_request_id: The merge request ID
            job_id: The job ID to process
            job_name: The job name
        """
        client = RepoClient.create_instance()

        # Create a unique thread ID to give the agent different threads (memory) by job name, so that we can consult
        # previous log traces and avoid applying the same fix to the same job name multiple times.
        thread_id = generate_uuid(f"{repo_id}{merge_request_id}{job_name}")

        manager = cls(client, repo_id, ref, thread_id=thread_id)
        manager._process_job(merge_request_id, job_id, job_name)

    def _process_job(self, merge_request_id: int, job_id: int, job_name: str):
        """
        Process pipeline fix for a job.

        Args:
            merge_request_id: The merge request ID
            job_id: The job ID to process
            job_name: The job name
        """
        log_trace = self._clean_logs(self.client.job_log_trace(self.repo_id, job_id))
        diffs = self.client.get_merge_request_diff(self.repo_id, merge_request_id)

        config = RunnableConfig(
            tags=[pipeline_fixer_settings.NAME, str(self.client.client_slug)],
            metadata={"merge_request_id": merge_request_id, "job_id": job_id},
            configurable={
                "thread_id": self.thread_id,
                "source_repo_id": self.repo_id,
                "source_ref": self.ref,
                "job_name": job_name,
            },
        )

        with PostgresSaver.from_conn_string(settings.DB_URI) as checkpointer:
            pipeline_fixer = PipelineFixerAgent(checkpointer=checkpointer, store=self._file_changes_store)

            current_state = pipeline_fixer.agent.get_state(config)

            result = pipeline_fixer.agent.invoke(
                {
                    "diff": self._merge_request_diffs_to_str(diffs),
                    "job_logs": log_trace,
                    "previous_job_logs": current_state.values.get("job_logs", None),
                    "iteration": current_state.values.get("iteration", 0),
                    "need_manual_fix": False,
                },
                config,
            )

            if file_changes := self._get_file_changes():
                self._commit_changes(file_changes=file_changes, thread_id=self.thread_id)

            elif result and result.get("need_manual_fix", False) and (troubleshooting := result.get("troubleshooting")):
                self.client.comment_merge_request(
                    self.repo_id,
                    merge_request_id,
                    jinja2_formatter(
                        PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE,
                        troubleshooting_details=troubleshooting,
                        job_name=job_name,
                        bot_name=BOT_NAME,
                    ),
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
