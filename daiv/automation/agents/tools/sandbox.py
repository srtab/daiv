from __future__ import annotations

import base64
import io
import json
import logging
import subprocess  # noqa: S404
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import tool
from langgraph.typing import StateT  # noqa: TC002
from unidiff import PatchSet

from automation.utils import has_file_changes, register_file_change
from codebase.base import FileChangeAction
from codebase.context import RuntimeCtx  # noqa: TC001
from core.conf import settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import RunCommandsRequest, RunCommandsResponse, StartSessionRequest

if TYPE_CHECKING:
    from langgraph.runtime import Runtime
    from langgraph.store.base import BaseStore


logger = logging.getLogger("daiv.tools")

BASH_TOOL_NAME = "bash"
FORMAT_CODE_TOOL_NAME = "format_code"

NUM_LEADING_SLASH = 1


async def _update_store_and_ctx(patch: str, store: BaseStore, repository_root_dir: Path):
    """
    Update the store and the repository root directory with the changes from the patch.

    Args:
        patch (str): The patch with the file changes.
        store (BaseStore): The store to save the file changes to.
        repository_root_dir (Path): The root directory of the repository.
    """
    patch_set = PatchSet.from_string(patch)
    file_changes = []

    # We need to populate the file changes before applying the patch to ensure that we have the old file content.
    for patched_file in patch_set:
        source_path = patched_file.source_file.split("/", NUM_LEADING_SLASH)[-1]
        target_path = patched_file.target_file.split("/", NUM_LEADING_SLASH)[-1]

        fs_file_path = repository_root_dir / patched_file.path

        if patched_file.is_added_file:
            file_changes.append({
                "action": FileChangeAction.CREATE,
                "new_file_content": None,
                "new_file_path": target_path,
            })
        elif patched_file.is_removed_file:
            file_changes.append({
                "action": FileChangeAction.DELETE,
                "old_file_content": fs_file_path.read_text(),
                "old_file_path": source_path,
                "new_file_content": "",
            })
        elif patched_file.is_modified_file:
            file_changes.append({
                "action": FileChangeAction.UPDATE,
                "old_file_content": fs_file_path.read_text(),
                "old_file_path": source_path,
                "new_file_content": None,
            })
        elif patched_file.is_rename:
            file_changes.append({
                "action": FileChangeAction.MOVE,
                "old_file_content": fs_file_path.read_text(),
                "old_file_path": source_path,
                "new_file_content": None,
                "new_file_path": target_path,
            })
        else:
            continue

    # Apply the patch to the repository root directory.
    apply_result = subprocess.run(  # noqa: S603 No risk of command injection
        ["/usr/bin/git", "apply", "--reject", "--whitespace=nowarn"],
        input=patch,
        cwd=repository_root_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    if apply_result.returncode != 0:
        raise RuntimeError(f"git apply failed: {apply_result.stdout}")

    # Register the file changes to the store .
    for file_change in file_changes:
        if file_change["action"] == FileChangeAction.UPDATE:
            file_change["new_file_content"] = (repository_root_dir / file_change["old_file_path"]).read_text()
        elif file_change["action"] in [FileChangeAction.CREATE, FileChangeAction.MOVE]:
            file_change["new_file_content"] = (repository_root_dir / file_change["new_file_path"]).read_text()

        await register_file_change(store, **file_change)


@tool(BASH_TOOL_NAME, parse_docstring=True)
async def bash_tool(commands: list[str], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Run a list of Bash commands in a persistent shell session rooted at the repository's root. Use this tool to apply the changes required by an execution plan (formatters, codegen, package manager ops). All writes must remain inside the repository.

    PURPOSE
    - Execute project-native CLIs that perform writes (e.g., formatters with --fix, code generators, package manager install/update/remove).
    - Do not use it for file I/O tasks that the companion tools handle directly.

    SESSION & EXECUTION
    - Persistent shell session across tool calls; commands run in order; pipelines/compound commands allowed.
    - Start in repo root. Prefer absolute paths. Avoid `cd` unless the plan explicitly requires it.
    - Run only the commands explicitly requested by the plan.

    OUTPUT CONTRACT
    - Success path: each command returns `exit_code` and raw output (stdout+stderr merged), truncated to 2000 chars.
    - Patch with the changes made by the executed commands.
    - Stop on first non-zero exit: execution halts; ONLY the failed command's `exit_code` and raw output are returned.
    - Output is unnormalized (may include ANSI). Add flags like `--no-color` if needed.

    WRITE SCOPE & BOUNDARIES
    - Writes must stay strictly within the repository root; do not touch parent dirs, `$HOME`, or follow symlinks that exit the repo.
    - **No Git commands** (no add/commit/checkout/rebase/push).
    - Default to **non-interactive**: add flags/env to avoid prompts (`-y/--yes`, `CI=1`, `--no-progress`, etc.).
    - Avoid high-impact/system-level actions:
    - No system package managers or global installs (`apt-get`, `yum`, `brew`, etc.).
    - No Docker builds/pushes or container/image manipulation.
    - No unscoped destructive ops (e.g., `rm -rf` outside targeted paths).
    - No DB schema changes/migrations/seeds.
    - No editing secrets/credentials (e.g., `.env`) or CI settings.
    - Network access is allowed for project package managers as required by the plan.

    PACKAGE MANAGEMENT POLICY
    - **Always** use the project's native package manager to add/update/remove dependencies; let it regenerate lockfiles. **Never** edit lockfiles by hand.
    - Examples: npm/pnpm/yarn/bun; pip/poetry/pip-tools/uv; cargo; go; bundler; composer; etc.
    - Prefer flags that keep output stable/parseable (e.g., `--json`, `--quiet`) when available.

    PREFERRED COMPANION TOOLS (USE INSTEAD OF SHELL EQUIVALENTS)
    - Reads/search/listing: `glob` (discovery), `grep` (content search), `ls` (directory metadata), `read` (file contents).
    - Writes/FS changes: `write` (create file), `edit` (replace old content with new), `delete` (remove file), `rename` (rename file).
    - Therefore, **avoid** shell substitutes like `touch`, `echo > file`, `sed -i`, `rm`, `mv`, `cp` when the companion tools can perform the same operation.
    - Fall back to Bash only when the companion tools cannot achieve the goal or when invoking project-native CLIs that must perform writes.

    WHEN TO USE
    - Apply formatter fixes (`eslint --fix`, `ruff --fix`, `black -w`);
    - Run code generators/scaffolding;
    - Manage dependencies via the project's package manager.

    WHEN NOT TO USE
    - Any file creation/edit/rename/delete that the `write`/`edit`/`rename`/`delete` tools can do.
    - Raw reads/search/listing that `glob`/`grep`/`ls`/`read` can handle.
    - Any operation outside the repository root or involving Git/system-level changes.

    Args:
        commands: The list of commands to execute.

    Returns:
        str: The output of the commands.
    """  # noqa: E501
    logger.info("[%s] Running bash commands: %s", bash_tool.name, commands)

    repo_working_dir = Path(runtime.context.repo.working_dir)
    response = await _run_bash_commands(commands, repo_working_dir, runtime.state["session_id"])

    if response is None:
        return (
            "error: Failed to run commands. Verify that the commands are valid. "
            "If the commands are valid, maybe the bash tool is not working properly."
        )

    if response.patch:
        try:
            await _update_store_and_ctx(response.patch, runtime.store, repo_working_dir)
        except Exception:
            logger.exception("Error updating store and ctx.")
            return "error: Failed to persist the changes. The bash tool is not working properly."

    return response.model_dump_json(exclude_none=True)


@tool(BASH_TOOL_NAME, parse_docstring=True)
async def read_only_bash_tool(commands: list[str], runtime: ToolRuntime[RuntimeCtx]) -> str:
    """
    Run a list of Bash commands in a shell session rooted at the repository's root. Designed for a planning agent to probe a repository and turn the findings into an execution plan for a later agent. All changes made by the commands are not persisted/committed to the repository.

    PURPOSE
    - Execute project CLIs (linters, type checkers, test discovery, i18n generators, build analyzers, etc.) to collect signals that inform a plan.
    - Use this to inspect and diagnose the repository, not to change it.

    SESSION & COMMANDS
    - The shell session persists across multiple tool calls and retains the working directory.
    - Commands run in order. Compound commands, pipelines, and redirections are allowed (e.g., `cmd1 && cmd2`, `cmd | jq ...`, `> /dev/null`).
    - Start in the repo root. Prefer absolute paths. Avoid `cd` unless your plan requires it.

    READ-ONLY & SAFETY
    - Changes are NOT persisted. Treat the environment as read-only. Use this tool to inspect and diagnose the repository, not to change it.
    - Avoid heavy/destructive operations (e.g., installs, container builds, mass formatting). Prefer dry-run/diagnostic flags when available (`--check`, `--dry-run`, `--no-color`, `--no-write`).
    - Network access is allowed; be judicious.

    OUTPUT CONTRACT
    - Success path: each command returns `exit_code` and raw output (stdout+stderr merged), truncated to 2000 chars.
    - Stop on first non-zero exit: execution halts; ONLY the failed command's `exit_code` and raw output are returned.
    - Output is unnormalized (may include ANSI). Add flags like `--no-color` if needed.

    PREFERRED COMPANION TOOLS (USE THESE FIRST)
    - Use structured tools instead of shell equivalents whenever feasible:
    - `glob` for file pattern expansion (instead of `find`/shell globs for discovery logic).
    - `grep` tool for content search (instead of the shell `grep` command).
    - `ls` tool for directory listings/metadata (instead of shell `ls`).
    - `read` tool for file contents (instead of `cat`/`head`/`tail`).
    - Fall back to Bash only when these tools cannot achieve the goal or when invoking project-native CLIs (e.g., `django-admin`, `eslint`, `ruff`, `pytest`, `tsc`, `mypy`, `npm`, `poetry`).

    WHEN TO USE
    - To run project CLIs in inspection/diagnostic modes and extract actionable signals (e.g., missing translations, lint/type errors, failing tests, dependency issues).

    WHEN NOT TO USE
    - For raw file reading, listing, or searching that the `glob`/`grep`/`ls`/`read` tools can handle.
    - For operations intended to modify the repository that are meant to be persisted/committed to the repository.

    PRACTICAL TIPS
    - Reduce noise to avoid truncation: prefer `--quiet`, `--format json`, `--reporter`, `--no-color`, and scoped paths.
    - Validate prerequisites early (e.g., `tool --version`) before long-running checks.

    EXAMPLES
    - Django i18n probe: `django-admin makemessages` → parse missing/obsolete entries; add locale/file actions to the plan.
    - Lint scan (no autofix): `eslint . --format json --max-warnings=0` or `ruff check --output-format json` → summarize rules/files for the plan.
    - Type checks: `tsc --noEmit`, `mypy --no-color-output` → capture error counts and hotspots to sequence fixes.

    Args:
        commands: The list of commands to execute.

    Returns:
        str: The output of the commands.
    """  # noqa: E501
    logger.info("[%s] Running read-only bash commands: %s", read_only_bash_tool.name, commands)

    repo_working_dir = Path(runtime.context.repo.working_dir)
    response = await _run_bash_commands(commands, repo_working_dir, runtime.state["session_id"], extract_patch=False)

    if response is None:
        return (
            "error: Failed to run commands. Verify that the commands are valid. "
            "If the commands are valid, maybe the bash tool is not working properly."
        )

    return json.dumps([result.model_dump(mode="json") for result in response.results])


@tool(FORMAT_CODE_TOOL_NAME, parse_docstring=True)
async def format_code_tool(runtime: ToolRuntime[RuntimeCtx], force: bool = False) -> str:
    """
    Applies code formatting and linting fixes to the repository to resolve style and linting issues introduced by recent changes.

    **Usage rules:**
     - This tool runs the repository's configured formatting tool (e.g., `ruff format`, `black`, `prettier`, etc.).
     - Use this tool after making code changes to ensure compliance with the project's code style guidelines.
     - You must use your `edit`, `write`, `delete`, `rename` tools at least once in the conversation before formatting the code. This tool will error if you attempt to format the code without changing any files.
     - The tool will return the output of the formatting command if it fails with the details of the error for you to fix. If the formatting command succeeds, the tool will return a success message.
     - If the `force` parameter is set to `True`, the tool will format the code even if no changes were made to the repository.

    Args:
        force: Whether to force the formatting of the code.

    Returns:
        str: The JSON string of the results of the format code command execution.
    """  # noqa: E501
    logger.info("[%s] Formatting code (force: %s)", format_code_tool.name, force)

    if not force and not await has_file_changes(runtime.store):
        return "warning: No changes were made to any files, skipping formatting the code."

    repo_working_dir = Path(runtime.context.repo.working_dir)
    response = await _run_bash_commands(
        runtime.context.config.sandbox.format_code, repo_working_dir, runtime.state["session_id"]
    )
    if response is None:
        return "error: Failed to format code. The format code tool is not working properly."

    # Return only the last failed result, respecting the fail fast flag.
    if response.results[-1].exit_code != 0:
        return response.results[-1].model_dump_json()

    if response.patch:
        try:
            await _update_store_and_ctx(response.patch, runtime.store, repo_working_dir)
        except Exception:
            logger.exception("[%s] Error updating store and ctx.", format_code_tool.name)
            return "error: Failed to format code. The format code tool is not working properly."

    return "success: Code formatted."


async def _run_bash_commands(
    commands: list[str], repo_dir: Path, session_id: str, extract_patch: bool = True
) -> RunCommandsResponse | None:
    """
    Run bash commands in the sandbox session.

    Args:
        commands: The list of commands to execute.
        repo_dir: The repository directory.
        session_id: The sandbox session ID.
        extract_patch: Whether to extract the patch with the changes made by the executed commands.

    Returns:
        The response from running the commands.
    """
    tar_archive = io.BytesIO()

    with tarfile.open(fileobj=tar_archive, mode="w:gz") as tar:
        tar.add(repo_dir, arcname=repo_dir.name)

    try:
        response = await DAIVSandboxClient().run_commands(
            session_id,
            RunCommandsRequest(
                commands=commands,
                workdir=repo_dir.name,
                archive=base64.b64encode(tar_archive.getvalue()).decode(),
                fail_fast=True,
                extract_patch=extract_patch,
            ),
        )
    except httpx.RequestError:
        logger.exception("Unexpected error calling sandbox API.")
        return None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            logger.error("Bad request calling sandbox API: %s", e.response.text)
            return None

        logger.exception("Status code %s calling sandbox API: %s", e.response.status_code, e.response.text)
        return None
    finally:
        tar_archive.close()

    return response


class SandboxState(AgentState):
    """
    Schema for the sandbox state.
    """

    session_id: str
    """
    The sandbox session ID.
    """


class SandboxMiddleware(AgentMiddleware):
    """
    Middleware to start a sandbox session before running the commands.

    This middleware starts a sandbox session before agent starts the execution loop and
    closes it after the agent finishes the execution loop. It also adds the sandbox tools to the agent.

    Example:
        ```python
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[SandboxMiddleware()],
        )
        ```
    """

    name = "sandbox_middleware"

    state_schema = SandboxState

    def __init__(self, *, read_only_bash: bool = False, include_format_code: bool = False):
        """
        Initialize the middleware.
        """
        assert settings.SANDBOX_API_KEY is not None, "SANDBOX_API_KEY is not set"

        super().__init__()

        self.tools = []
        self.read_only_bash = read_only_bash

        if read_only_bash:
            self.tools.append(read_only_bash_tool)
        else:
            self.tools.append(bash_tool)

        if include_format_code:
            self.tools.append(format_code_tool)

    async def abefore_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, list] | None:
        """
        Start a sandbox session before the agent start the execution loop.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, list] | None: The state updates with the sandbox session.
        """
        session_id = await DAIVSandboxClient().start_session(
            StartSessionRequest(
                base_image=runtime.context.config.sandbox.base_image, extract_patch=not self.read_only_bash
            )
        )
        return {"session_id": session_id}

    async def aafter_agent(self, state: StateT, runtime: Runtime[RuntimeCtx]) -> dict[str, list] | None:
        """
        Close the sandbox session after the agent finishes the execution loop.

        Args:
            state (StateT): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context.

        Returns:
            dict[str, list] | None: The state updates with the closed sandbox session.
        """
        await DAIVSandboxClient().close_session(state["session_id"])
