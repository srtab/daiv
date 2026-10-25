from typing import TYPE_CHECKING

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
