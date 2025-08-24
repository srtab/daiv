from __future__ import annotations

import base64
import io
import json
import logging
import tarfile
from typing import TYPE_CHECKING, Annotated, Any

import httpx
from langchain_core.tools import ToolException, tool
from langgraph.prebuilt import InjectedStore

from automation.utils import register_file_change
from codebase.base import FileChangeAction
from codebase.context import get_repository_ctx
from core.conf import settings

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.store.base import BaseStore

logger = logging.getLogger("daiv.tools")


MAX_OUTPUT_LENGTH = 10000


async def _update_store_and_ctx(archive: bytes, store: BaseStore, repository_root_dir: Path):
    """
    Update the store with the file changes from the archive and the repository root directory with the changed files.

    Args:
        archive (bytes): The archive with the file changes.
        store (BaseStore): The store to save the file changes to.
        repository_root_dir (Path): The root directory of the repository.
    """
    with io.BytesIO(archive) as archive, tarfile.open(fileobj=archive) as tar:
        for member in tar.getmembers():
            if member.isfile() and (extracted_file := tar.extractfile(member)):
                fs_file_path = repository_root_dir / member.name

                new_file_content = extracted_file.read().decode()

                # Register the file change in the store, if already exists, it will be updated.
                await register_file_change(
                    store=store,
                    action=FileChangeAction.UPDATE if fs_file_path.exists() else FileChangeAction.CREATE,
                    old_file_content=fs_file_path.read_text(),
                    old_file_path=member.name,
                    new_file_content=new_file_content,
                )

                # Update the file content in the repository root directory.
                fs_file_path.write_text(new_file_content)

                # FIXME: deal with file deletions


@tool("bash", parse_docstring=True, response_format="content_and_artifact")
async def bash_tool(commands: list[str], store: Annotated[Any, InjectedStore()]) -> tuple[str, dict[str, Any] | None]:
    """
    Executes a given list of bash commands in a persistent shell session relative to the repository root directory, ensuring proper handling and security measures.

    **Usage rules:**
     - The commands are executed in the order they are provided.
     - When a command fails, the session is terminated and the results are returned.
     - Results are returned with output (truncated to 10000 characters) and exit code for each successful command.
     - Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the plan explicitly requests it. `pytest /foo/bar/tests cd /foo/bar && pytest tests`.
     - IMPORTANT: Only execute commands that are directly requested.
     - VERY IMPORTANT: You MUST avoid using search commands like `find` and `grep`. Instead use `grep` or `glob` to search. You MUST avoid read tools like `cat`, `head`, `tail`, and `ls`, and use `read` and `ls` to read files.

    **Common operations:**
     - When the plan includes package management operations:
         - **ALWAYS** use the project's package manager native commands to add / update / remove packages to ensure the lock file (if present) is regenerated automatically. Do **NOT** edit lock files by hand.

    Args:
        commands: The list of commands to execute.

    Returns:
        str: The output of the commands.
    """  # noqa: E501
    logger.debug("[%s] Running commands: %s", bash_tool.name, commands)

    assert settings.SANDBOX_API_KEY is not None, "SANDBOX_API_KEY is not set"

    ctx = get_repository_ctx()

    tar_archive = io.BytesIO()
    with tarfile.open(fileobj=tar_archive, mode="w:gz") as tar:
        tar.add(ctx.repo_dir, arcname=ctx.repo_dir.name)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.SANDBOX_URL}run/commands/",
                json={
                    "session_id": ctx.session_id,
                    "base_image": ctx.config.commands.base_image,
                    "commands": commands,
                    "workdir": ctx.repo_dir.name,
                    "archive": base64.b64encode(tar_archive.getvalue()).decode(),
                    "fail_fast": True,
                },
                headers={"X-API-KEY": settings.SANDBOX_API_KEY.get_secret_value()},
                timeout=settings.SANDBOX_TIMEOUT,
            )
    except httpx.RequestError as e:
        raise ToolException(e) from e
    finally:
        tar_archive.close()

    if response.status_code != 200:
        logger.error("[%s] Error running commands: %s", bash_tool.name, response.content)

        if response.status_code == 400:
            return (
                "error: Failed to run commands. Verify that the commands are valid. "
                "If the commands are valid, maybe the bash tool is not working properly."
            ), None
        return "error: Failed to run commands. The bash tool is not working properly.", None

    sandbox_response = response.json()

    if sandbox_response["archive"]:
        try:
            await _update_store_and_ctx(sandbox_response["archive"], store, ctx.repo_dir)
        except Exception:
            logger.exception("[%s] Error updating store and ctx.", bash_tool.name)
            return "error: Failed to finalize the commands.", None

    truncated_results = [
        {"command": result["command"], "output": result["output"][:MAX_OUTPUT_LENGTH], "exit_code": result["exit_code"]}
        for result in sandbox_response["results"]
    ]

    return json.dumps(truncated_results), truncated_results[-1]
