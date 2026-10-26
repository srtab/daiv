from typing import TYPE_CHECKING

from plugins.runtime import ResolvedPlugin

if TYPE_CHECKING:
    from pathlib import Path


def make_tree(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return tmp_path


def resolved_plugin(parent: Path, name: str = "acme", files: dict[str, str | bytes] | None = None) -> ResolvedPlugin:
    host_root = parent / name
    host_root.mkdir(parents=True, exist_ok=True)
    make_tree(host_root, files or {})
    return ResolvedPlugin(name=name, host_root=host_root, skills=(), agents=())
