from dataclasses import dataclass
from typing import TYPE_CHECKING

from skills.constants import PLUGINS_PATH

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class PluginSkillRef:
    name: str
    description: str
    path: str
    plugin_root: str


@dataclass(frozen=True)
class PluginAgentRef:
    name: str
    description: str
    path: str
    model: str | None


@dataclass(frozen=True)
class ResolvedPlugin:
    name: str
    commit: str
    host_root: Path
    skills: tuple[PluginSkillRef, ...]
    agents: tuple[PluginAgentRef, ...]

    @property
    def workspace_root(self) -> str:
        return f"{PLUGINS_PATH}/{self.name}"
