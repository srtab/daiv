# Locking tests for the code-review skill's finding contract. Like marker.py,
# findings.py lives under a hyphenated path and is invoked as a subprocess in
# the sandbox, so it isn't importable via the package path — load it by file
# path and exercise the functions directly.
import importlib.util
import json
import sys

from daiv.settings.components import PROJECT_DIR

_FINDINGS_PATH = PROJECT_DIR / "automation" / "agent" / "skills" / "code-review" / "scripts" / "findings.py"
_SPEC = importlib.util.spec_from_file_location("daiv_findings_under_test", _FINDINGS_PATH)
findings = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(findings)


def _f(**over):
    base = {
        "detector": "correctness",
        "file": "x.py",
        "line": 10,
        "bar": "defect",
        "archetype": "discussion",
        "title": "t",
        "rationale": "r",
    }
    base.update(over)
    return base


class TestValidate:
    def test_wellformed_kept(self):
        valid, dropped = findings.validate([_f()])
        assert dropped == 0
        assert valid == [_f()]

    def test_missing_required_field_dropped(self):
        valid, dropped = findings.validate([_f(title=None), _f(rationale="")])
        assert valid == []
        assert dropped == 2

    def test_unknown_enum_dropped(self):
        bad = [_f(detector="nonsense"), _f(bar="nitpick"), _f(archetype="reformat")]
        valid, dropped = findings.validate(bad)
        assert valid == []
        assert dropped == 3

    def test_non_int_or_bad_line_dropped(self):
        bad = [_f(line="12"), _f(line=True), _f(line=0), _f(line=-3)]
        valid, dropped = findings.validate(bad)
        assert valid == []
        assert dropped == 4

    def test_non_dict_dropped(self):
        valid, dropped = findings.validate(["not a dict", 5, None])
        assert valid == []
        assert dropped == 3

    def test_custom_rules_without_source_dropped(self):
        f = _f(detector="custom-rules", source=None)
        valid, dropped = findings.validate([f])
        assert valid == []
        assert dropped == 1

    def test_custom_rules_with_source_kept(self):
        f = _f(detector="custom-rules", source="review-rules.md: payments calls need a timeout")
        valid, dropped = findings.validate([f])
        assert valid == [f]
        assert dropped == 0

    def test_non_custom_rules_without_source_kept(self):
        # source is only required for custom-rules; built-in detectors don't need it
        valid, dropped = findings.validate([_f(detector="correctness")])
        assert dropped == 0


class TestDedupe:
    def test_same_key_collapses_to_strongest_bar(self):
        a = _f(detector="structure", bar="question", title="weak")
        b = _f(detector="correctness", bar="defect", title="strong")
        out = findings.dedupe([a, b])
        assert len(out) == 1
        assert out[0]["title"] == "strong"

    def test_distinct_keys_kept(self):
        a = _f(file="a.py", line=1)
        b = _f(file="a.py", line=2)
        c = _f(file="a.py", line=1, archetype="question")
        assert len(findings.dedupe([a, b, c])) == 3

    def test_first_seen_order_preserved(self):
        a = _f(file="b.py", line=5, title="first")
        b = _f(file="a.py", line=9, title="second")
        out = findings.dedupe([a, b])
        assert [x["title"] for x in out] == ["first", "second"]


class TestMerge:
    def test_merge_happy_path(self):
        # _f(detector="bogus") fails validation -> counted in `dropped` (1).
        # _f(bar="question") shares the dedup key (file, line, archetype) with
        # _f() and is collapsed by dedupe -> NOT counted in `dropped`. So one
        # distinct valid finding survives: candidates == len(findings) == 1.
        result = findings.merge([_f(), _f(bar="question"), _f(detector="bogus")])
        assert result == {"findings": [_f()], "candidates": 1, "dropped": 1}


class TestMergeCli:
    def test_cli_rejects_non_array(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _StringIOStdin('{"not": "array"}'))
        monkeypatch.setattr(sys, "argv", ["findings.py", "merge"])
        assert findings.main() == 1
        assert "expected a JSON array" in capsys.readouterr().err

    def test_cli_rejects_malformed(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _StringIOStdin("not json"))
        monkeypatch.setattr(sys, "argv", ["findings.py", "merge"])
        assert findings.main() == 1
        assert "invalid JSON" in capsys.readouterr().err

    def test_cli_happy(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _StringIOStdin(json.dumps([_f()])))
        monkeypatch.setattr(sys, "argv", ["findings.py", "merge"])
        assert findings.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"findings": [_f()], "candidates": 1, "dropped": 0}


class _StringIOStdin:
    # ``sys.stdin`` replacement that returns a fixed string. ``json.load`` needs
    # a ``.read()`` method, and pytest's capsys doesn't replace stdin for us.
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
