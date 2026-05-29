# Locking tests for the code-review skill's rule-source resolver. Loaded by file
# path like marker.py / findings.py. Uses tmp_path to build fake repo roots.
import importlib.util
import json
import sys

from daiv.settings.components import PROJECT_DIR

_RULES_PATH = PROJECT_DIR / "automation" / "agent" / "skills" / "code-review" / "scripts" / "rules.py"
_SPEC = importlib.util.spec_from_file_location("daiv_rules_under_test", _RULES_PATH)
rules = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rules)


class TestResolve:
    def test_no_sources_has_rules_false(self, tmp_path):
        out = rules.resolve(str(tmp_path))
        assert out["has_rules"] is False
        assert out["found"] == []
        assert out["unreadable"] == []
        assert out["rules_document"] == ""

    def test_unreadable_file_reported_not_silently_absent(self, tmp_path):
        # A present-but-undecodable rule source must surface in `unreadable` (so the skill can
        # warn) rather than masquerade as absent — and must not crash resolve. UnicodeDecodeError
        # is a ValueError, not an OSError, so the old `except OSError` let it escape.
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "review-rules.md").write_bytes(b"\xff\xfe not utf-8")
        out = rules.resolve(str(tmp_path))
        assert out["unreadable"] == [".agents/review-rules.md"]
        assert out["found"] == []
        assert out["has_rules"] is False

    def test_unreadable_supplementary_source_reported(self, tmp_path):
        # The supplementary branch is structurally separate from the primary one; an undecodable
        # AGENTS.md must land in `unreadable` too, not be silently skipped.
        (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8")
        out = rules.resolve(str(tmp_path))
        assert out["unreadable"] == ["AGENTS.md"]
        assert out["found"] == []
        assert out["has_rules"] is False

    def test_mixed_readable_and_unreadable_sources(self, tmp_path):
        # `found` and `unreadable` populate independently: one unreadable source must not suppress
        # a readable one, and a readable primary still yields has_rules True.
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "review-rules.md").write_text("No logging of request bodies.")
        (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8")
        out = rules.resolve(str(tmp_path))
        assert out["found"] == [".agents/review-rules.md"]
        assert out["unreadable"] == ["AGENTS.md"]
        assert out["has_rules"] is True

    def test_broken_symlink_reported_as_unreadable(self, tmp_path):
        # A dangling symlink resolves to nothing, so Path.exists() is False — but it's a committed
        # file the maintainer expects read, so report it as unreadable rather than silently absent
        # (the same silent-skip class as a permission/encoding failure).
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "review-rules.md").symlink_to(tmp_path / "missing-target.md")
        out = rules.resolve(str(tmp_path))
        assert out["unreadable"] == [".agents/review-rules.md"]
        assert out["found"] == []
        assert out["has_rules"] is False

    def test_primary_only(self, tmp_path):
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "review-rules.md").write_text("No logging of request bodies.")
        out = rules.resolve(str(tmp_path))
        assert out["has_rules"] is True
        assert out["found"] == [".agents/review-rules.md"]
        assert "authoritative" in out["rules_document"]
        assert "No logging of request bodies." in out["rules_document"]

    def test_primary_precedes_supplementary(self, tmp_path):
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "review-rules.md").write_text("PRIMARY RULE")
        (tmp_path / "AGENTS.md").write_text("SUPPLEMENTARY NOTE")
        out = rules.resolve(str(tmp_path))
        assert out["found"] == [".agents/review-rules.md", "AGENTS.md"]
        assert out["rules_document"].index("PRIMARY RULE") < out["rules_document"].index("SUPPLEMENTARY NOTE")

    def test_supplementary_only(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Use camelCase for exported functions.")
        out = rules.resolve(str(tmp_path))
        assert out["has_rules"] is True
        assert out["found"] == ["AGENTS.md"]
        assert "Supplementary" in out["rules_document"]

    def test_empty_or_whitespace_files_ignored(self, tmp_path):
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "review-rules.md").write_text("   \n\t\n")
        out = rules.resolve(str(tmp_path))
        assert out["has_rules"] is False
        assert out["found"] == []

    def test_custom_context_file_name(self, tmp_path):
        (tmp_path / "GUIDELINES.md").write_text("Domain rule here.")
        out = rules.resolve(str(tmp_path), context_file="GUIDELINES.md")
        assert out["found"] == ["GUIDELINES.md"]

    def test_context_file_equal_to_memory_not_duplicated(self, tmp_path):
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "AGENTS.md").write_text("note")
        out = rules.resolve(str(tmp_path), context_file=".agents/AGENTS.md")
        assert out["found"] == [".agents/AGENTS.md"]


class TestCli:
    def test_resolve_cli_happy(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "review-rules.md").write_text("rule")
        monkeypatch.setattr(sys, "argv", ["rules.py", "resolve", "--root", str(tmp_path)])
        assert rules.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["has_rules"] is True
        assert out["found"] == [".agents/review-rules.md"]
