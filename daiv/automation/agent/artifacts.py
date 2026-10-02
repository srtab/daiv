"""What the ``publish_artifact`` tool needs from wherever its files are kept.

The agent stores nothing itself: the run layer hands ``create_daiv_agent`` an :class:`ArtifactStore`
(``sessions.artifacts.RunArtifactStore`` for executor runs), and without one the agent has no ``publish_artifact`` tool.
Standard library only: ``sessions.artifacts`` imports this module at ``django.setup()``.
"""

from __future__ import annotations

from typing import Protocol

PUBLISH_ARTIFACT_TOOL_NAME = "publish_artifact"


class ArtifactError(ValueError):
    """A publish request the agent can act on: bad file name, empty or oversized file, run budget spent."""


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

    async def astore(self, *, thread_id: str, filename: str, content: bytes, title: str = "") -> str:
        """Keep ``content`` and return the tool's success result; raise :class:`ArtifactError` to reject it."""
        ...
