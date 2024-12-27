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
            # GitLab returns a tar archive with the root folder name, so we need to
            # extract the first level folder name to use it as the base workdir.
            first_level_folders = {member.name.split("/")[0] for member in tar.getmembers() if member.isdir()}
            if len(first_level_folders) != 1:
                raise ValueError(
                    "Unexpected number of first level folders in the archive. "
                    f"Expected 1, got {len(first_level_folders)}: {first_level_folders}"
                )
            workdir = first_level_folders.pop()

            response = httpx.post(
                f"{settings.SANDBOX_URL}run/commands/",
                json={
                    "run_id": str(uuid.uuid4()),
                    "base_image": RepositoryConfig.get_config(self.source_repo_id).commands.base_image,
                    "commands": commands,
                    "workdir": workdir,
                    "archive": base64.b64encode(tarstream.getvalue()).decode(),
                },
                headers={"X-API-KEY": settings.SANDBOX_API_KEY},
                timeout=settings.SANDBOX_TIMEOUT,
            )

        response.raise_for_status()
        resp = RunCommandResponse(**response.json())

        if resp.archive:
            with io.BytesIO(resp.archive) as archive, tarfile.open(fileobj=archive) as tar:
                for member in tar.getmembers():
                    if member.isfile() and (extracted_file := tar.extractfile(member)):
                        store.put(
                            ("file_changes", self.source_repo_id, self.source_ref),
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
