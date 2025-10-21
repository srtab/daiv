from __future__ import annotations

import base64
import io
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from langchain.agents.middleware._execution import BaseExecutionPolicy

from codebase.context import get_runtime_ctx
from core.sandbox import close_sandbox_session, run_sandbox_commands, start_sandbox_session
from core.sandbox.schemas import RunCommandsRequest

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore


@dataclass
class DAIVSandboxExecutionPolicy(BaseExecutionPolicy):
    """
    ExecutionPolicy that manages a remote persistent sandbox session using the
    daiv-sandbox service instead of spawning a local shell process.

    Notes:
        - The required BaseExecutionPolicy.spawn() method is not used in this policy and
          intentionally raises NotImplementedError to prevent accidental local execution.
    """

    # Cached base64-encoded tar.gz archive of the working repository for the current run
    _archive_b64: str | None = field(default=None, init=False, repr=False)
    # Cached workdir name (repository directory name mounted in the sandbox)
    _workdir: str | None = field(default=None, init=False, repr=False)

    # BaseExecutionPolicy requires spawn(), but this policy executes remotely
    def spawn(self, *, workspace, env, command):  # type: ignore[override]
        raise NotImplementedError("DAIVSandboxExecutionPolicy does not spawn local processes")

    async def ensure_session(self, store: BaseStore) -> None:
        """Ensure a sandbox session exists and cache archive/workdir for the run."""
        await start_sandbox_session(self._build_start_request(), store)
        # Lazily build archive and workdir once per agent run
        if self._archive_b64 is None or self._workdir is None:
            self._archive_b64, self._workdir = self._build_repo_snapshot()

    async def close_session(self, store: BaseStore) -> None:
        await close_sandbox_session(store)

    async def run_single_command(self, store: BaseStore, command: str, *, extract_patch: bool = True):
        """Run a single command in the sandbox and optionally request a patch."""
        await self.ensure_session(store)
        assert self._archive_b64 is not None and self._workdir is not None
        request = RunCommandsRequest(
            commands=[command],
            workdir=self._workdir,
            archive=self._archive_b64,
            extract_patch=extract_patch,
            fail_fast=True,
        )
        return await run_sandbox_commands(request, store)

    def _build_start_request(self):
        from core.sandbox.schemas import StartSessionRequest

        ctx = get_runtime_ctx()
        return StartSessionRequest(base_image=ctx.config.sandbox.base_image)

    @staticmethod
    def _tar_repo_dir_to_b64() -> tuple[str, str]:
        """Create a gzipped tar of the repository directory and return (b64, workdir)."""
        ctx = get_runtime_ctx()
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            tar.add(ctx.repo_dir, arcname=ctx.repo_dir.name)
        return base64.b64encode(tar_buf.getvalue()).decode(), ctx.repo_dir.name

    def _build_repo_snapshot(self) -> tuple[str, str]:
        return self._tar_repo_dir_to_b64()
