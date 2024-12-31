from __future__ import annotations

import base64
import io
import logging
import tarfile
import textwrap
import uuid
from typing import TYPE_CHECKING

import httpx
from langchain.tools import BaseTool
from langchain_core.prompts.string import jinja2_formatter
from pydantic import BaseModel, Field

from automation.utils import file_changes_namespace
from codebase.base import FileChange, FileChangeAction
from codebase.clients import RepoClient
from core.conf import settings
from core.config import RepositoryConfig

from .schemas import RunCodeInput, RunCommandInput, RunCommandResponse

if TYPE_CHECKING:
    from langchain.callbacks.manager import CallbackManagerForToolRun
    from langgraph.store.memory import BaseStore

logger = logging.getLogger("daiv.tools")


class RunSandboxCommandsTool(BaseTool):
    name: str = "run_sandbox_commands"
    description: str = textwrap.dedent(
        """\
        Run a list of commands on the repository. The commands will be run in the same order as they are provided. All the changes made by the commands will be considered to be committed.
        """  # noqa: E501
    )
    args_schema: type[BaseModel] = RunCommandInput

    source_repo_id: str = Field(..., description="The repository to run the commands on.")
    source_ref: str = Field(..., description="The branch or commit to run the commands on.")

    api_wrapper: RepoClient = Field(..., default_factory=RepoClient.create_instance)

    def _run(
        self, commands: list[str], intent: str, store: BaseStore, run_manager: CallbackManagerForToolRun | None = None
    ) -> str:
        """
        Run commands in the sandbox.

        Args:
            commands: The commands to run in the sandbox.
            intent: A description of why you're running these commands.
            store: The store to save the file changes to.

        Returns:
            The results of the commands to feed the agent knowledge.
        """
        logger.debug("[%s] Running commands in sandbox: %s (intent: %s)", self.name, commands, intent)

        with (
            self.api_wrapper.get_repository_archive(self.source_repo_id, self.source_ref) as tarstream,
            tarfile.open(fileobj=tarstream, mode="r:*") as tar,
        ):
            workdir = self._extract_workdir(tar)

            if store.search(file_changes_namespace(self.source_repo_id, self.source_ref), limit=1):
                # If there's already file changes stored, we need to update the tar archive with them.
                logger.debug("[%s] Updating tar archive with file changes", self.name)
                tar_archive = self._copy_tar_with_file_changes(tar, store, workdir)
            else:
                # If there's no file changes stored, we can use the original tar archive.
                logger.debug("[%s] Using original tar archive", self.name)
                tar_archive = base64.b64encode(tarstream.getvalue()).decode()

            response = httpx.post(
                f"{settings.SANDBOX_URL}run/commands/",
                json={
                    "run_id": str(uuid.uuid4()),
                    "base_image": RepositoryConfig.get_config(self.source_repo_id).commands.base_image,
                    "commands": commands,
                    "workdir": workdir,
                    "archive": tar_archive,
                },
                headers={"X-API-KEY": settings.SANDBOX_API_KEY},
                timeout=settings.SANDBOX_TIMEOUT,
            )

        response.raise_for_status()
        return self._treat_response(response, store)

    def _treat_response(self, response: httpx.Response, store: BaseStore) -> str:
        """
        Treat the response from the sandbox.

        Args:
            response: The response from the sandbox.
            store: The store to save the file changes to.

        Returns:
            The results of the commands to feed the agent knowledge.
        """
        resp = RunCommandResponse(**response.json())

        if resp.archive:
            with io.BytesIO(resp.archive) as archive, tarfile.open(fileobj=archive) as tar:
                for member in tar.getmembers():
                    if member.isfile() and (extracted_file := tar.extractfile(member)):
                        store.put(
                            file_changes_namespace(self.source_repo_id, self.source_ref),
                            member.name,
                            {
                                "data": FileChange(
                                    file_path=member.name,
                                    action=FileChangeAction.UPDATE,
                                    content=extracted_file.read().decode(),
                                )
                            },
                        )

        return jinja2_formatter(
            textwrap.dedent(
                """\
                {% for result in results %}
                ### `{{ result.command }}` (exit code: `{{ result.exit_code }}`)
                ```bash
                {{ result.output }}
                ```
                {% endfor %}
                """
            ),
            results=resp.results,
        )

    def _extract_workdir(self, source_tar: tarfile.TarFile) -> str:
        """
        Extract the workdir from the tar archive.

        Args:
            source_tar: The tar archive to extract the workdir from.

        Returns:
            The workdir.
        """
        # GitLab returns a tar archive with the root folder name, so we need to
        # extract the first level folder name to use it as the base workdir.
        first_level_folders = {member.name.split("/")[0] for member in source_tar.getmembers() if member.isdir()}

        if len(first_level_folders) != 1:
            raise ValueError(
                "Unexpected number of first level folders in the archive. "
                f"Expected 1, got {len(first_level_folders)}: {first_level_folders}"
            )
        return first_level_folders.pop()

    def _copy_tar_with_file_changes(self, source_tar: tarfile.TarFile, store: BaseStore, workdir: str) -> str:
        """
        Copy the tar archive and update it to reflect the registered file changes.

        Args:
            source_tar: The tar archive to copy and update.
            store: The store to get the file changes from.
            workdir: The workdir to use in the sandbox.

        Returns:
            The updated tar archive.
        """
        updated_tar_buffer = io.BytesIO()

        with tarfile.open(fileobj=updated_tar_buffer, mode="w:gz") as new_tar:
            for member in source_tar.getmembers():
                if member.isdir():
                    new_tar.addfile(member)
                elif member.isfile():
                    if file_change := store.get(
                        file_changes_namespace(self.source_repo_id, self.source_ref),
                        member.name.removeprefix(f"{workdir}/"),
                    ):
                        if file_change.value["data"].action == FileChangeAction.DELETE:
                            continue

                        updated_member = tarfile.TarInfo(name=member.name)
                        updated_member.size = len(file_change.value["data"].content)
                        new_tar.addfile(updated_member, io.BytesIO(file_change.value["data"].content.encode("utf-8")))
                    else:
                        new_tar.addfile(member, source_tar.extractfile(member))

            for item in store.search(
                file_changes_namespace(self.source_repo_id, self.source_ref),
                filter={"action": FileChangeAction.CREATE},
                limit=100,
            ):
                new_member = tarfile.TarInfo(name=item.value["data"].file_path)
                new_member.size = len(item.value["data"].content)
                new_tar.addfile(new_member, io.BytesIO(item.value["data"].content.encode("utf-8")))

        updated_tar_buffer.seek(0)

        return base64.b64encode(updated_tar_buffer.getvalue()).decode()


class RunSandboxCodeTool(BaseTool):
    name: str = "run_sandbox_code"
    description: str = textwrap.dedent(
        """\
        Evaluates python code in a sandbox environment. The environment is long running and exists across multiple executions. You must send the whole script every time and print your outputs. Script should be pure python code that can be evaluated. It should be in python format NOT markdown. The code should NOT be wrapped in backticks.

        Use cases:
        - Running a script to obtain current datetime.
        - Running a script to do calculations.
        """  # noqa: E501
    )
    args_schema: type[BaseModel] = RunCodeInput

    def _run(
        self,
        python_code: str,
        dependencies: list[str],
        intent: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        """
        Run python code in the sandbox.

        Args:
            code: The python code to run.
            dependencies: The dependencies to install before running the code.
            intent: A description of why you're running this code.

        Returns:
            The results of the commands to feed the agent knowledge.
        """
        logger.debug(
            "[%s] Running code in sandbox: %s (intent: %s) => %s", self.name, dependencies, intent, python_code
        )

        response = httpx.post(
            f"{settings.SANDBOX_URL}run/code/",
            json={"run_id": str(uuid.uuid4()), "code": python_code, "dependencies": dependencies, "language": "python"},
            headers={"X-API-KEY": settings.SANDBOX_API_KEY},
            timeout=settings.SANDBOX_TIMEOUT,
        )

        response.raise_for_status()
        return response.json()["output"]
