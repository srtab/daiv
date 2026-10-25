import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from skills.constants import FRONTMATTER_RE

from plugins.conf import settings
from plugins.constants import MAX_DESCRIPTION_CHARS, MAX_PLUGIN_PATH_CHARS, MAX_VERSION_CHARS, PLUGIN_NAME_RE

MANIFEST_DIR = ".claude-plugin"
COMPONENT_DIRS = ("skills", "commands", "agents")
UNSUPPORTED_SOURCE_REASON = "Source outside this repository is not supported yet."
LONG_PATH_REASON = "The plugin's path in this repository is too long."


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class PluginCandidate:
    name: str
    plugin_path: str
    description: str
    version: str
    supported: bool
    reason: str


@dataclass(frozen=True)
class RepoInspection:
    marketplace_name: str
    candidates: tuple[PluginCandidate, ...]


@dataclass(frozen=True)
class ItemRef:
    name: str
    description: str
    path: str
    model: str | None = None


@dataclass(frozen=True)
class Skipped:
    component: str
    detail: str


@dataclass(frozen=True)
class PluginContents:
    name: str
    description: str
    version: str
    skills: tuple[ItemRef, ...]
    commands: tuple[ItemRef, ...]
    agents: tuple[ItemRef, ...]
    skipped: tuple[Skipped, ...]

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> PluginContents:
        return cls(
            name=data["name"],
            description=data["description"],
            version=data["version"],
            skills=tuple(ItemRef(**item) for item in data["skills"]),
            commands=tuple(ItemRef(**item) for item in data["commands"]),
            agents=tuple(ItemRef(**item) for item in data["agents"]),
            skipped=tuple(Skipped(**item) for item in data["skipped"]),
        )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ManifestError(f"{path.name} is not valid JSON.") from e


def _read_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ManifestError(f"{path.name} must contain a JSON object.")
    return data


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _description(value: Any) -> str:
    return _text(value)[:MAX_DESCRIPTION_CHARS]


def _version(value: Any) -> str:
    return _text(value)[:MAX_VERSION_CHARS]


def _parse_markdown(path: Path) -> tuple[dict, str]:
    """Return (frontmatter, body); raises ValueError on undecodable text or invalid YAML."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("not valid UTF-8") from e
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise ValueError("invalid frontmatter") from e
    return (meta if isinstance(meta, dict) else {}), text[match.end() :]


def _normalise_source(source: Any) -> str | None:
    """Return the in-repo plugin path for a relative source, or None when it is not supported."""
    if not isinstance(source, str) or not (source == "." or source.startswith("./")):
        return None
    parts = [part for part in PurePosixPath(source).parts if part != "."]
    if ".." in parts or any(part.startswith("/") for part in parts):
        return None
    return "/".join(parts)


def inspect_repo(root: Path) -> RepoInspection:
    manifest_dir = root / MANIFEST_DIR
    marketplace = _read_json_object(manifest_dir / "marketplace.json")
    has_marketplace = (manifest_dir / "marketplace.json").is_file()
    has_plugin_json = (manifest_dir / "plugin.json").is_file()
    has_components = any((root / name).is_dir() for name in COMPONENT_DIRS)

    if not (has_marketplace or has_plugin_json or has_components):
        raise ManifestError("This repository is not a Claude Code plugin or marketplace.")

    if not has_marketplace:
        manifest = _read_json_object(manifest_dir / "plugin.json")
        candidate = PluginCandidate(
            name=_text(manifest.get("name")) or root.name,
            plugin_path="",
            description=_description(manifest.get("description")),
            version=_version(manifest.get("version")),
            supported=True,
            reason="",
        )
        return RepoInspection(marketplace_name="", candidates=(candidate,))

    entries = marketplace.get("plugins")
    candidates = [
        _candidate_from_entry(root, entry)
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
    ]
    return RepoInspection(marketplace_name=_text(marketplace.get("name")), candidates=tuple(candidates))


def _candidate_from_entry(root: Path, entry: dict) -> PluginCandidate:
    name = _text(entry.get("name"))
    description = _description(entry.get("description"))
    version = _version(entry.get("version"))
    plugin_path = _normalise_source(entry.get("source"))
    if plugin_path is None:
        return PluginCandidate(name, "", description, version, False, UNSUPPORTED_SOURCE_REASON)
    if len(plugin_path) > MAX_PLUGIN_PATH_CHARS:
        return PluginCandidate(name, plugin_path, description, version, False, LONG_PATH_REASON)

    try:
        manifest = _read_json_object(root / plugin_path / MANIFEST_DIR / "plugin.json")
    except ManifestError:
        manifest = {}
    return PluginCandidate(
        name=_text(manifest.get("name")) or name,
        plugin_path=plugin_path,
        description=description or _description(manifest.get("description")),
        version=version or _version(manifest.get("version")),
        supported=True,
        reason="",
    )


def _check_limits(plugin_dir: Path) -> None:
    file_count = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(plugin_dir):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.is_symlink():
                continue
            size = path.stat().st_size
            file_count += 1
            total_bytes += size
            if file_count > settings.MAX_PLUGIN_FILES:
                raise ManifestError(f"Plugin has more than {settings.MAX_PLUGIN_FILES} files.")
            if size > settings.MAX_FILE_BYTES:
                raise ManifestError(f"File {path.relative_to(plugin_dir).as_posix()} exceeds the size limit.")
            if total_bytes > settings.MAX_PLUGIN_BYTES:
                raise ManifestError("Plugin exceeds the total size limit.")


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _inside(plugin_dir: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(plugin_dir.resolve())


def _skill_dirs(plugin_dir: Path, manifest: dict) -> list[Path]:
    roots = [plugin_dir / "skills"]
    roots += [plugin_dir / ref for ref in _as_list(manifest.get("skills")) if isinstance(ref, str)]

    found: dict[Path, None] = {}
    for base in roots:
        if not base.is_dir() or not _inside(plugin_dir, base):
            continue
        if (base / "SKILL.md").is_file() and base != plugin_dir / "skills":
            found[base] = None
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file() and _inside(plugin_dir, child):
                found[child] = None
    return list(found)


def _read_skills(plugin_dir: Path, manifest: dict) -> tuple[list[ItemRef], list[Skipped]]:
    skills: list[ItemRef] = []
    skipped: list[Skipped] = []
    for skill_dir in _skill_dirs(plugin_dir, manifest):
        try:
            meta, _ = _parse_markdown(skill_dir / "SKILL.md")
        except ValueError as e:
            skipped.append(Skipped("skill", f"{skill_dir.name}: {e}"))
            continue
        description = _description(meta.get("description"))
        if not description:
            skipped.append(Skipped("skill", f"{skill_dir.name}: no description"))
            continue
        name = _text(meta.get("name")) or skill_dir.name
        if not PLUGIN_NAME_RE.match(name):
            skipped.append(Skipped("skill", f"{name}: invalid name"))
            continue
        skills.append(ItemRef(name, description, (skill_dir / "SKILL.md").relative_to(plugin_dir).as_posix()))
    return skills, skipped


def _read_commands(plugin_dir: Path, skill_names: set[str]) -> tuple[list[ItemRef], list[Skipped]]:
    commands: list[ItemRef] = []
    skipped: list[Skipped] = []
    commands_dir = plugin_dir / "commands"
    if not commands_dir.is_dir():
        return commands, skipped
    for path in sorted(commands_dir.rglob("*.md")):
        if not path.is_file() or not _inside(plugin_dir, path):
            continue
        stem = path.stem
        try:
            meta, body = _parse_markdown(path)
        except ValueError as e:
            skipped.append(Skipped("command", f"{stem}: {e}"))
            continue
        if not PLUGIN_NAME_RE.match(stem):
            skipped.append(Skipped("command", f"{stem}: invalid name"))
            continue
        if stem in skill_names:
            skipped.append(Skipped("command", f"{stem}: same name as a skill"))
            continue
        description = _description(meta.get("description"))
        if not description:
            description = _description(next((line for line in body.splitlines() if line.strip()), ""))
        commands.append(ItemRef(stem, description, path.relative_to(plugin_dir).as_posix()))
    return commands, skipped


def _read_agents(plugin_dir: Path) -> tuple[list[ItemRef], list[Skipped]]:
    agents: list[ItemRef] = []
    skipped: list[Skipped] = []
    agents_dir = plugin_dir / "agents"
    if not agents_dir.is_dir():
        return agents, skipped
    for path in sorted(agents_dir.glob("*.md")):
        if not path.is_file() or not _inside(plugin_dir, path):
            continue
        try:
            meta, _ = _parse_markdown(path)
        except ValueError as e:
            skipped.append(Skipped("agent", f"{path.name}: {e}"))
            continue
        name = _text(meta.get("name"))
        description = _description(meta.get("description"))
        if not name or not description:
            skipped.append(Skipped("agent", f"{path.name}: missing name or description"))
            continue
        if not PLUGIN_NAME_RE.match(name):
            skipped.append(Skipped("agent", f"{name}: invalid name"))
            continue
        agents.append(
            ItemRef(name, description, path.relative_to(plugin_dir).as_posix(), _text(meta.get("model")) or None)
        )
    return agents, skipped


def _hook_events(value: Any, plugin_dir: Path) -> list[str]:
    if isinstance(value, str):
        path = plugin_dir / value
        if not path.is_file() or not _inside(plugin_dir, path):
            return []
        try:
            return _hook_events(_read_json(path), plugin_dir)
        except ManifestError:
            return []
    if isinstance(value, list):
        return [event for item in value for event in _hook_events(item, plugin_dir)]
    if isinstance(value, dict):
        inner = value.get("hooks")
        return [str(key) for key in (inner if isinstance(inner, dict) else value)]
    return []


def _declared(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ", ".join(value)
    return json.dumps(value, sort_keys=True)


def _server_count(value: Any, plugin_dir: Path) -> int:
    if isinstance(value, str):
        path = plugin_dir / value
        if path.is_file() and _inside(plugin_dir, path):
            try:
                return _server_count(_read_json(path), plugin_dir)
            except ManifestError:
                return 1
        return 1
    if isinstance(value, list):
        return sum(_server_count(item, plugin_dir) for item in value)
    if isinstance(value, dict):
        inner = value.get("mcpServers")
        return len(inner) if isinstance(inner, dict) else len(value)
    return 0


def _read_skipped_components(plugin_dir: Path, manifest: dict) -> list[Skipped]:
    skipped: list[Skipped] = []

    events: list[str] = []
    declared_hooks = manifest.get("hooks")
    hooks_file = plugin_dir / "hooks" / "hooks.json"
    if hooks_file.is_file():
        events += _hook_events(str(hooks_file.relative_to(plugin_dir)), plugin_dir)
    if declared_hooks is not None:
        events += _hook_events(declared_hooks, plugin_dir)
    if events:
        skipped.append(Skipped("hooks", ", ".join(dict.fromkeys(events))))
    elif declared_hooks is not None:
        skipped.append(Skipped("hooks", _declared(declared_hooks)))
    elif hooks_file.is_file():
        skipped.append(Skipped("hooks", "hooks/hooks.json"))

    servers = 0
    mcp_file = plugin_dir / ".mcp.json"
    if mcp_file.is_file():
        servers += _server_count(".mcp.json", plugin_dir)
    if manifest.get("mcpServers") is not None:
        servers += _server_count(manifest["mcpServers"], plugin_dir)
    if servers:
        skipped.append(Skipped("mcp_servers", f"{servers} server(s)"))

    for key, component in (("outputStyles", "output_styles"), ("lspServers", "lsp_servers")):
        if manifest.get(key) is not None:
            skipped.append(Skipped(component, _declared(manifest[key])))
    return skipped


def read_plugin(root: Path, plugin_path: str, *, fallback_name: str) -> PluginContents:
    plugin_dir = root / plugin_path
    if not plugin_dir.is_dir() or not _inside(root, plugin_dir):
        raise ManifestError(f"Plugin directory '{plugin_path}' does not exist in this repository.")

    _check_limits(plugin_dir)
    manifest = _read_json_object(plugin_dir / MANIFEST_DIR / "plugin.json")

    name = _text(manifest.get("name")) or fallback_name
    if not PLUGIN_NAME_RE.match(name):
        raise ManifestError(f"Invalid plugin name '{name}'.")

    skills, skipped = _read_skills(plugin_dir, manifest)
    commands, command_skipped = _read_commands(plugin_dir, {skill.name for skill in skills})
    agents, agent_skipped = _read_agents(plugin_dir)
    skipped += command_skipped + agent_skipped + _read_skipped_components(plugin_dir, manifest)

    return PluginContents(
        name=name,
        description=_description(manifest.get("description")),
        version=_version(manifest.get("version")),
        skills=tuple(skills),
        commands=tuple(commands),
        agents=tuple(agents),
        skipped=tuple(skipped),
    )
