import re
from collections.abc import Iterable

from langchain_core.runnables import RunnableConfig

from automation.agents.pipeline_fixer.agent import PipelineFixerAgent
from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from codebase.base import ClientType, FileChange, MergeRequestDiff
from codebase.clients import AllRepoClient, RepoClient
from core.config import RepositoryConfig


class PipelineFixerManager:
    """
    Manages the pipeline fix process.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str, thread_id: str):
        self.client = client
        self.repo_id = repo_id
        self.ref = ref
        self.thread_id = thread_id
        self.repo_config = RepositoryConfig.get_config(repo_id)

    @classmethod
    def process_job(cls, repo_id: str, ref: str, merge_request_id: int, job_id: int, job_name: str):
        """
        Process pipeline fix for a job.

        Args:
            repo_id: The repository ID
            ref: The source reference
            merge_request_id: The merge request ID
            job_id: The job ID to process
        """
        client = RepoClient.create_instance()
        manager = cls(client, repo_id, ref, f"{repo_id}#{merge_request_id}:{job_id}")
        manager._process_job(merge_request_id, job_id, job_name)

    def _process_job(self, merge_request_id: int, job_id: int, job_name: str):
        """
        Process pipeline fix for a job.

        Args:
            merge_request_id: The merge request ID
            job_id: The job ID to process
        """
        log_trace = self._clean_logs(self.client.job_log_trace(self.repo_id, job_id))
        diffs = self.client.get_merge_request_diff(self.repo_id, merge_request_id)

        pipeline_fixer = PipelineFixerAgent(
            repo_client=self.client, source_repo_id=self.repo_id, source_ref=self.ref, job_id=job_id
        )
        category = pipeline_fixer.agent.invoke(
            {"job_logs": log_trace, "diff": self._merge_request_diffs_to_str(diffs)},
            RunnableConfig(configurable={"thread_id": self.thread_id}),
        )
        if category.category == "codebase":
            self.client.create_merge_request_discussion_note(
                self.repo_id,
                merge_request_id,
                f"""The pipeline is failing due to a codebase related issue and needs to be fixed. Here some details:

<dl>
    <dt>Job name:</dt>
    <dd>{job_name}</dd>
    <dt>Root cause:</dt>
    <dd>{category.root_cause}</dd>
</dl>

<details>
<summary>Expand extracted logs</summary>

```
{log_trace}
```
</details>""",
            )
            # self._commit_changes(file_changes=pipeline_fixer.get_files_to_commit())

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

    def _commit_changes(self, *, file_changes: list[FileChange]):
        """
        Commit changes to the merge request.
        """
        pr_describer = PullRequestDescriberAgent()
        changes_description = pr_describer.agent.invoke(
            {"changes": file_changes, "branch_name_convention": self.repo_config.branch_name_convention},
            RunnableConfig(configurable={"thread_id": self.thread_id}),
        )

        self.client.commit_changes(self.repo_id, self.ref, changes_description.commit_message, file_changes)
