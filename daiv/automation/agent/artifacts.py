"""What the artifact tools (``publish_artifact``, ``fetch_artifact``) need from wherever their files are kept.

The agent stores nothing itself: the run layer hands ``create_daiv_agent`` an :class:`ArtifactStore`
(``sessions.artifacts.RunArtifactStore`` for executor runs), and without one the agent has no artifact tools.
Standard library only: ``sessions.artifacts`` imports this module at ``django.setup()``.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol

PUBLISH_ARTIFACT_TOOL_NAME = "publish_artifact"
FETCH_ARTIFACT_TOOL_NAME = "fetch_artifact"


class ArtifactError(ValueError):
    """A request the agent can act on: no run to attach to, bad file name, empty or oversized file, run budget spent,
    no such artifact in the session."""


class ArtifactFile(NamedTuple):
    filename: str
    content: bytes


class ArtifactStore(Protocol):
    @property
    def max_bytes(self) -> int:
        """The largest file it keeps, in bytes; the tool caps the workspace download at it."""
        ...

    @property
    def per_run_max(self) -> int:
        """How many files one run may publish."""
        ...

    async def aaccepts(self, thread_id: str) -> bool:
        """Whether a publish on ``thread_id`` has a run to attach to; asked before the file is read."""
        ...

    async def astore(
        self, *, thread_id: str, filename: str, content: bytes, title: str = "", artifact_id: str = ""
    ) -> str:
        """Keep ``content`` and return the tool's success result; raise :class:`ArtifactError` to reject it.

        With ``artifact_id``, ``content`` replaces the file of that artifact of the session instead, keeping its URL.
        """
        ...

    async def aread(self, *, thread_id: str, artifact_id: str) -> ArtifactFile:
        """The current file of artifact ``artifact_id`` of the session; raise :class:`ArtifactError` if it has none."""
        ...
