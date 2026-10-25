import json

import pytest
from plugins.conf import settings
from plugins.manifest import ItemRef, ManifestError, PluginCandidate, PluginContents, Skipped, inspect_repo, read_plugin

from .helpers import make_tree

SKILL = "---\nname: {name}\ndescription: {desc}\n---\nBody\n"


def marketplace(*plugins: dict, name: str = "mkt") -> str:
    return json.dumps({"name": name, "plugins": list(plugins)})


class TestInspectRepo:
    def test_superpowers_layout_is_single_plugin_marketplace(self, tmp_path):
        make_tree(
            tmp_path,
            {
                ".claude-plugin/marketplace.json": marketplace({"name": "superpowers", "source": "./"}),
                ".claude-plugin/plugin.json": json.dumps({"name": "superpowers", "version": "1.2.3"}),
                "skills/a/SKILL.md": SKILL.format(name="a", desc="d"),
            },
        )
        inspection = inspect_repo(tmp_path, repo_slug="acme/repo")
        assert inspection.marketplace_name == "mkt"
        assert len(inspection.candidates) == 1
        candidate = inspection.candidates[0]
        assert candidate.name == "superpowers"
        assert candidate.plugin_path == ""
        assert candidate.supported is True
        assert candidate.version == "1.2.3"

    def test_dipcode_layout_lists_two_plugins(self, tmp_path):
        make_tree(
            tmp_path,
            {
                ".claude-plugin/marketplace.json": marketplace(
                    {"name": "devkit", "source": "./devkit", "description": "Dev"},
                    {"name": "management", "source": "./management/"},
                ),
                "devkit/skills/a/SKILL.md": SKILL.format(name="a", desc="d"),
                "management/commands/c.md": "Do it\n",
            },
        )
        candidates = inspect_repo(tmp_path, repo_slug="acme/repo").candidates
        assert [(c.name, c.plugin_path, c.supported) for c in candidates] == [
            ("devkit", "devkit", True),
            ("management", "management", True),
        ]
        assert candidates[0].description == "Dev"

    def test_external_source_unsupported(self, tmp_path):
        make_tree(
            tmp_path,
            {
                ".claude-plugin/marketplace.json": marketplace(
                    {"name": "ext", "source": {"source": "github", "repo": "a/b"}},
                    {"name": "url", "source": "https://example.com/x.git"},
                )
            },
        )
        for candidate in inspect_repo(tmp_path, repo_slug="acme/repo").candidates:
            assert candidate.supported is False
            assert candidate.reason == "Source outside this repository is not supported yet."

    def test_dotdot_source_unsupported(self, tmp_path):
        make_tree(tmp_path, {".claude-plugin/marketplace.json": marketplace({"name": "x", "source": "./../x"})})
        candidate = inspect_repo(tmp_path, repo_slug="acme/repo").candidates[0]
        assert candidate.supported is False
        assert candidate.reason

    def test_bare_plugin_without_manifests(self, tmp_path):
        make_tree(tmp_path, {"skills/a/SKILL.md": SKILL.format(name="a", desc="d")})
        candidates = inspect_repo(tmp_path, repo_slug="acme/repo").candidates
        assert len(candidates) == 1
        assert candidates[0].name == "repo"
        assert candidates[0].plugin_path == ""
        assert candidates[0].supported is True

    def test_nameless_plugin_named_after_the_repository(self, tmp_path):
        make_tree(tmp_path, {".claude-plugin/plugin.json": "{}"})
        (candidate,) = inspect_repo(tmp_path, repo_slug="Group/My_Plugin.Kit").candidates
        assert candidate.name == "my-plugin-kit"

    def test_long_candidate_description_and_version_are_capped(self, tmp_path):
        make_tree(
            tmp_path,
            {
                ".claude-plugin/marketplace.json": marketplace(
                    {"name": "a", "source": "./a", "description": "d" * 2000, "version": "1" * 100},
                    {"name": "b", "source": "./b"},
                ),
                "b/.claude-plugin/plugin.json": json.dumps({"description": "e" * 2000, "version": "2" * 100}),
            },
        )
        a, b = inspect_repo(tmp_path, repo_slug="acme/repo").candidates
        assert (a.description, a.version) == ("d" * 1024, "1" * 64)
        assert (b.description, b.version) == ("e" * 1024, "2" * 64)

    def test_long_single_plugin_description_and_version_are_capped(self, tmp_path):
        manifest = {"name": "p", "description": "d" * 2000, "version": "1" * 100}
        make_tree(tmp_path, {".claude-plugin/plugin.json": json.dumps(manifest)})
        candidate = inspect_repo(tmp_path, repo_slug="acme/repo").candidates[0]
        assert (len(candidate.description), len(candidate.version)) == (1024, 64)

    def test_overlong_plugin_path_unsupported(self, tmp_path):
        long_path = "p" * 256
        make_tree(
            tmp_path,
            {
                ".claude-plugin/marketplace.json": marketplace(
                    {"name": "long", "source": f"./{long_path}"}, {"name": "fits", "source": f"./{'q' * 255}"}
                )
            },
        )
        long, fits = inspect_repo(tmp_path, repo_slug="acme/repo").candidates
        assert long.supported is False
        assert long.reason == "The plugin's path in this repository is too long."
        assert fits.supported is True

    def test_not_a_plugin_raises(self, tmp_path):
        make_tree(tmp_path, {"README.md": "hi"})
        with pytest.raises(ManifestError, match="This repository is not a Claude Code plugin or marketplace."):
            inspect_repo(tmp_path, repo_slug="acme/repo")


class TestReadPlugin:
    def test_skill_without_description_skipped_not_fatal(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "skills/good/SKILL.md": SKILL.format(name="good", desc="ok"),
                "skills/bad/SKILL.md": "---\nname: bad\n---\nBody\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert [s.name for s in contents.skills] == ["good"]
        assert Skipped("skill", "bad: no description") in contents.skipped

    def test_non_utf8_skill_skipped_not_fatal(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "skills/good/SKILL.md": SKILL.format(name="good", desc="ok"),
                "skills/bin/SKILL.md": b"---\nname: bin\ndescription: \xff\xfe\n---\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert [s.name for s in contents.skills] == ["good"]
        assert any(s.component == "skill" and s.detail.startswith("bin:") for s in contents.skipped)

    def test_skill_item_ref(self, tmp_path):
        make_tree(tmp_path, {"pl/skills/dir-name/SKILL.md": "---\ndescription: hello\n---\nBody\n"})
        contents = read_plugin(tmp_path, "pl", fallback_name="p")
        assert contents.skills == (ItemRef(name="dir-name", description="hello", path="skills/dir-name/SKILL.md"),)

    def test_command_description_falls_back_to_first_line(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "commands/review.md": "\n\nReview the code\nmore\n",
                "commands/sub/deploy.md": "---\ndescription: Ship it\n---\nBody\n",
                "commands/long.md": "x" * 2000,
            },
        )
        by_name = {c.name: c for c in read_plugin(tmp_path, "", fallback_name="p").commands}
        assert by_name["review"].description == "Review the code"
        assert by_name["review"].path == "commands/review.md"
        assert by_name["deploy"].description == "Ship it"
        assert by_name["deploy"].path == "commands/sub/deploy.md"
        assert len(by_name["long"].description) == 1024

    def test_command_clashing_with_skill_skipped(self, tmp_path):
        make_tree(tmp_path, {"skills/foo/SKILL.md": SKILL.format(name="foo", desc="d"), "commands/foo.md": "Run foo\n"})
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert contents.commands == ()
        assert Skipped("command", "foo: same name as a skill") in contents.skipped

    def test_duplicate_item_names_keep_the_first(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "skills/one/SKILL.md": SKILL.format(name="dup", desc="first"),
                "skills/two/SKILL.md": SKILL.format(name="dup", desc="second"),
                "commands/a/deploy.md": "First\n",
                "commands/b/deploy.md": "Second\n",
                "agents/a.md": "---\nname: rev\ndescription: first\n---\nP\n",
                "agents/b.md": "---\nname: rev\ndescription: second\n---\nP\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert [(s.name, s.description) for s in contents.skills] == [("dup", "first")]
        assert [(c.name, c.path) for c in contents.commands] == [("deploy", "commands/a/deploy.md")]
        assert [(a.name, a.description) for a in contents.agents] == [("rev", "first")]
        for component, name in (("skill", "dup"), ("command", "deploy"), ("agent", "rev")):
            assert Skipped(component, f"{name}: duplicate name") in contents.skipped

    def test_invalid_item_names_skipped(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "skills/dir/SKILL.md": SKILL.format(name="Bad Skill", desc="d"),
                "agents/a.md": "---\nname: Bad_Agent\ndescription: d\n---\nP\n",
                "commands/Bad_Cmd.md": "Run\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert (contents.skills, contents.agents, contents.commands) == ((), (), ())
        assert Skipped("skill", "Bad Skill: invalid name") in contents.skipped
        assert Skipped("agent", "Bad_Agent: invalid name") in contents.skipped
        assert Skipped("command", "Bad_Cmd: invalid name") in contents.skipped

    def test_agent_model_read(self, tmp_path):
        description = "Use for review.\n<example>\nuser: hi\n</example>"
        make_tree(
            tmp_path,
            {
                "agents/rev.md": (
                    "---\nname: rev\ndescription: |-\n  Use for review.\n  <example>\n  user: hi\n"
                    "  </example>\nmodel: openrouter:z-ai/glm-5\n---\nPrompt\n"
                ),
                "agents/plain.md": "---\nname: plain\ndescription: Plain\n---\nPrompt\n",
                "agents/nodesc.md": "---\nname: nodesc\n---\nPrompt\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        by_name = {a.name: a for a in contents.agents}
        assert by_name["rev"].model == "openrouter:z-ai/glm-5"
        assert by_name["rev"].description == description
        assert by_name["plain"].model is None
        assert "nodesc" not in by_name
        assert Skipped("agent", "nodesc.md: missing name or description") in contents.skipped

    def test_agent_claude_code_model_alias_not_stored_and_recorded(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "agents/a.md": "---\nname: a\ndescription: d\nmodel: sonnet\n---\nP\n",
                "agents/b.md": "---\nname: b\ndescription: d\nmodel: Inherit\n---\nP\n",
                "agents/c.md": "---\nname: c\ndescription: d\nmodel: openrouter:z-ai/glm-5\n---\nP\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        models = [(a.name, a.model) for a in contents.agents]
        assert models == [("a", None), ("b", None), ("c", "openrouter:z-ai/glm-5")]
        assert [s for s in contents.skipped if s.component == "agent"] == [
            Skipped("agent", "a: model 'sonnet' not used"),
            Skipped("agent", "b: model 'Inherit' not used"),
        ]

    def test_agent_tools_list_recorded_not_fatal(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "agents/a.md": "---\nname: a\ndescription: d\ntools: Read, Grep\n---\nP\n",
                "agents/b.md": "---\nname: b\ndescription: d\ntools:\n  - Read\n---\nP\n",
                "agents/c.md": "---\nname: c\ndescription: d\n---\nP\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert [a.name for a in contents.agents] == ["a", "b", "c"]
        assert [s for s in contents.skipped if s.component == "agent"] == [
            Skipped("agent", "a: tools list not applied"),
            Skipped("agent", "b: tools list not applied"),
        ]

    def test_hooks_and_mcp_recorded_as_skipped(self, tmp_path):
        make_tree(
            tmp_path,
            {
                "hooks/hooks.json": json.dumps({"hooks": {"SessionStart": [{"hooks": []}]}}),
                ".mcp.json": json.dumps({"mcpServers": {"a": {}, "b": {}}}),
                ".claude-plugin/plugin.json": json.dumps({
                    "name": "p",
                    "outputStyles": "./styles",
                    "lspServers": {"x": {}},
                }),
            },
        )
        skipped = read_plugin(tmp_path, "", fallback_name="p").skipped
        assert Skipped("hooks", "SessionStart") in skipped
        assert Skipped("mcp_servers", "2 server(s)") in skipped
        assert {s.component for s in skipped} >= {"output_styles", "lsp_servers"}

    def test_plugin_json_inline_hooks_recorded(self, tmp_path):
        make_tree(
            tmp_path, {".claude-plugin/plugin.json": json.dumps({"name": "p", "hooks": {"PreToolUse": [], "Stop": []}})}
        )
        assert Skipped("hooks", "PreToolUse, Stop") in read_plugin(tmp_path, "", fallback_name="p").skipped

    def test_custom_skill_paths_from_plugin_json(self, tmp_path):
        make_tree(
            tmp_path,
            {
                ".claude-plugin/plugin.json": json.dumps({"name": "p", "skills": ["./extra/one", "./more"]}),
                "extra/one/SKILL.md": SKILL.format(name="one", desc="d"),
                "more/two/SKILL.md": SKILL.format(name="two", desc="d"),
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="x")
        assert sorted(s.name for s in contents.skills) == ["one", "two"]
        assert contents.name == "p"

    def test_long_descriptions_and_version_are_capped(self, tmp_path):
        manifest = {"name": "p", "description": "d" * 2000, "version": "1" * 100}
        make_tree(
            tmp_path,
            {
                ".claude-plugin/plugin.json": json.dumps(manifest),
                "skills/s/SKILL.md": SKILL.format(name="s", desc="s" * 2000),
                "commands/c.md": f"---\ndescription: {'c' * 2000}\n---\nBody\n",
                "agents/a.md": f"---\nname: a\ndescription: {'a' * 2000}\n---\nPrompt\n",
            },
        )
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert (contents.description, contents.version) == ("d" * 1024, "1" * 64)
        assert contents.skills[0].description == "s" * 1024
        assert contents.commands[0].description == "c" * 1024
        assert contents.agents[0].description == "a" * 1024

    def test_custom_command_paths_replace_default_directory(self, tmp_path):
        make_tree(
            tmp_path,
            {
                ".claude-plugin/plugin.json": json.dumps({"name": "p", "commands": ["./extra/cmds/", "./one.md"]}),
                "extra/cmds/a.md": "Run a\n",
                "extra/cmds/sub/b.md": "Run b\n",
                "extra/cmds/notes.txt": "x",
                "one.md": "Run one\n",
                "commands/default.md": "Run default\n",
            },
        )
        commands = {c.name: c.path for c in read_plugin(tmp_path, "", fallback_name="p").commands}
        assert commands == {"a": "extra/cmds/a.md", "b": "extra/cmds/sub/b.md", "one": "one.md"}

    def test_custom_command_path_as_string(self, tmp_path):
        make_tree(
            tmp_path,
            {".claude-plugin/plugin.json": json.dumps({"name": "p", "commands": "./cmds"}), "cmds/go.md": "Go\n"},
        )
        assert [c.name for c in read_plugin(tmp_path, "", fallback_name="p").commands] == ["go"]

    def test_custom_agent_paths_replace_default_directory(self, tmp_path):
        agent = "---\nname: {name}\ndescription: d\n---\nP\n"
        make_tree(
            tmp_path,
            {
                ".claude-plugin/plugin.json": json.dumps({"name": "p", "agents": ["./custom/rev.md", "./more/"]}),
                "custom/rev.md": agent.format(name="rev"),
                "more/dir-agent.md": agent.format(name="dir-agent"),
                "agents/default.md": agent.format(name="default"),
            },
        )
        agents = {a.name: a.path for a in read_plugin(tmp_path, "", fallback_name="p").agents}
        assert agents == {"rev": "custom/rev.md"}

    def test_custom_component_paths_outside_plugin_ignored(self, tmp_path):
        manifest = {"name": "p", "commands": ["../outside", "/etc"], "agents": "../outside/a.md"}
        make_tree(
            tmp_path,
            {
                "pl/.claude-plugin/plugin.json": json.dumps(manifest),
                "outside/c.md": "Run c\n",
                "outside/a.md": "---\nname: a\ndescription: d\n---\nP\n",
            },
        )
        contents = read_plugin(tmp_path, "pl", fallback_name="p")
        assert (contents.commands, contents.agents) == ((), ())

    def test_commands_object_form_recorded_as_skipped(self, tmp_path):
        manifest = {"name": "p", "commands": {"about": {"content": "Explain."}}}
        make_tree(tmp_path, {".claude-plugin/plugin.json": json.dumps(manifest), "commands/default.md": "Run\n"})
        contents = read_plugin(tmp_path, "", fallback_name="p")
        assert contents.commands == ()
        assert Skipped("command", "plugin.json object form not supported") in contents.skipped

    def test_name_falls_back(self, tmp_path):
        make_tree(tmp_path, {"skills/a/SKILL.md": SKILL.format(name="a", desc="d")})
        assert read_plugin(tmp_path, "", fallback_name="from-entry").name == "from-entry"

    def test_invalid_plugin_name_raises(self, tmp_path):
        make_tree(tmp_path, {".claude-plugin/plugin.json": json.dumps({"name": "Bad Name"})})
        with pytest.raises(ManifestError):
            read_plugin(tmp_path, "", fallback_name="ok")

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(ManifestError):
            read_plugin(tmp_path, "nope", fallback_name="ok")

    def test_path_escape_raises(self, tmp_path):
        (tmp_path / "repo").mkdir()
        with pytest.raises(ManifestError):
            read_plugin(tmp_path / "repo", "..", fallback_name="ok")

    def test_file_count_limit_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MAX_PLUGIN_FILES", 2)
        make_tree(tmp_path, {"skills/a/SKILL.md": SKILL.format(name="a", desc="d"), "x": "1", "y": "2"})
        with pytest.raises(ManifestError):
            read_plugin(tmp_path, "", fallback_name="p")

    def test_file_size_limit_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MAX_FILE_BYTES", 10)
        make_tree(tmp_path, {"big.txt": "x" * 11})
        with pytest.raises(ManifestError):
            read_plugin(tmp_path, "", fallback_name="p")

    def test_total_size_limit_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MAX_PLUGIN_BYTES", 10)
        make_tree(tmp_path, {"a.txt": "x" * 6, "b.txt": "x" * 6})
        with pytest.raises(ManifestError):
            read_plugin(tmp_path, "", fallback_name="p")

    def test_git_directory_ignored_in_limits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MAX_PLUGIN_FILES", 1)
        make_tree(
            tmp_path,
            {".git/objects/a": "1", ".git/objects/b": "2", "skills/a/SKILL.md": SKILL.format(name="a", desc="d")},
        )
        assert read_plugin(tmp_path, "", fallback_name="p").skills

    def test_contents_json_round_trip(self, tmp_path):
        contents = PluginContents(
            name="p",
            description="d",
            version="1",
            skills=(ItemRef("s", "sd", "skills/s/SKILL.md"),),
            commands=(ItemRef("c", "cd", "commands/c.md"),),
            agents=(ItemRef("a", "ad", "agents/a.md", model="opus"),),
            skipped=(Skipped("hooks", "Stop"),),
        )
        payload = json.loads(json.dumps(contents.to_json()))
        assert PluginContents.from_json(payload) == contents

    def test_candidate_is_frozen(self):
        candidate = PluginCandidate("a", "", "", "", True, "")
        with pytest.raises(AttributeError):
            candidate.name = "b"
