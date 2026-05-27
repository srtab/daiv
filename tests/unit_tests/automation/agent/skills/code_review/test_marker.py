# Locking tests for the code-review skill's marker contract. The script lives
# under a hyphenated path (``skills/code-review/scripts/marker.py``) and is
# invoked as a subprocess from the sandbox, so it isn't importable via the
# normal package path. Load the module by file path and exercise the functions
# directly — the contract that matters is byte-stable output across reruns
# (anchors, JSON payloads, parse paths), which is exactly what these tests pin.
import importlib.util
import json
import sys

import pytest

from daiv.settings.components import PROJECT_DIR

_MARKER_PATH = PROJECT_DIR / "automation" / "agent" / "skills" / "code-review" / "scripts" / "marker.py"
_SPEC = importlib.util.spec_from_file_location("daiv_marker_under_test", _MARKER_PATH)
marker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(marker)


class TestComputeAnchor:
    def test_long_line_uses_target_alone(self):
        # A 32-char line is well above the 16-char threshold — the script must
        # ignore ``next_line`` and hash the target only. Pin the exact 8 hex
        # so a future refactor of separator handling can't silently shift it.
        target = "self.client = build_real_client()"
        assert marker.compute_anchor(target, next_line="anything") == marker.compute_anchor(target, next_line=None)

    def test_short_line_includes_next_for_disambiguation(self):
        # 4 chars — under the threshold. With a next line, anchor differs from
        # the no-next case (otherwise every short line collides).
        with_next = marker.compute_anchor("x = 1", next_line="y = 2")
        without_next = marker.compute_anchor("x = 1", next_line=None)
        assert with_next != without_next

    def test_separator_only_line_triggers_disambiguator(self):
        # ``});`` matches the separator regex. The disambiguator must kick in
        # even though the line is long enough — same rule as short lines.
        with_next = marker.compute_anchor("});", next_line="return")
        without_next = marker.compute_anchor("});", next_line=None)
        assert with_next != without_next

    def test_blank_next_line_treated_as_missing(self):
        # An empty / whitespace-only next line shouldn't contribute to the hash —
        # otherwise the agent passing ``--next ""`` vs omitting the flag would
        # produce different anchors for the same finding.
        assert marker.compute_anchor("x = 1", next_line="") == marker.compute_anchor("x = 1", next_line=None)
        assert marker.compute_anchor("x = 1", next_line="  ") == marker.compute_anchor("x = 1", next_line=None)

    def test_output_is_8_hex(self):
        anchor = marker.compute_anchor("self.client = build_real_client()", next_line=None)
        assert len(anchor) == 8
        assert all(c in "0123456789abcdef" for c in anchor)


class TestBuildMarker:
    def test_inline_payload_field_order_is_stable(self):
        # The JSON key order is load-bearing: parse_marker / dedup compare by
        # parsed dict, but if humans grep for the marker line a stable order
        # matters too. Lock the exact serialized form.
        line = marker.build_marker(
            "inline", sha="abc123", archetype="remove_dead_lines", file="x.py", line=42, anchor="deadbeef"
        )
        assert line == (
            '<!-- daiv-cr {"v":1,"kind":"inline","archetype":"remove_dead_lines",'
            '"file":"x.py","line":42,"anchor":"deadbeef","sha":"abc123"} -->'
        )

    def test_summary_payload_is_minimal(self):
        assert marker.build_marker("summary", sha="abc") == '<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc"} -->'

    def test_reply_payload_is_minimal(self):
        assert marker.build_marker("reply", sha="abc") == '<!-- daiv-cr {"v":1,"kind":"reply","sha":"abc"} -->'

    def test_inline_missing_required_fields_raises(self):
        with pytest.raises(SystemExit, match="archetype"):
            marker.build_marker("inline", sha="abc", archetype=None, file="x.py", line=1, anchor="a" * 8)

    def test_unknown_kind_raises(self):
        with pytest.raises(SystemExit, match="unknown kind"):
            marker.build_marker("nonsense", sha="abc")


class TestParseMarker:
    def test_roundtrips_inline(self):
        line = marker.build_marker(
            "inline", sha="abc", archetype="remove_dead_lines", file="x.py", line=42, anchor="deadbeef"
        )
        parsed = marker.parse_marker(line)
        assert parsed == {
            "v": 1,
            "kind": "inline",
            "archetype": "remove_dead_lines",
            "file": "x.py",
            "line": 42,
            "anchor": "deadbeef",
            "sha": "abc",
        }

    def test_ignores_lines_without_prefix(self):
        assert marker.parse_marker("just a normal comment") is None
        assert marker.parse_marker("<!-- some other comment -->") is None

    def test_ignores_unknown_version(self):
        # Forward-compat: future v=2 markers are dropped silently so v=1 code
        # doesn't dedup against them and accidentally suppress posts.
        line = '<!-- daiv-cr {"v":2,"kind":"inline","file":"x.py"} -->'
        assert marker.parse_marker(line) is None

    def test_only_reads_first_line(self):
        # The marker contract is "single physical line, no embedded newlines".
        # A body with the marker on line 1 + prose after must still parse.
        body = '<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc"} -->\n\nbody text'
        assert marker.parse_marker(body) == {"v": 1, "kind": "summary", "sha": "abc"}

    def test_corrupt_payload_emits_stderr_warning(self, capsys):
        # If a daiv-posted note was truncated/edited and the JSON no longer
        # parses, we surface the corruption rather than silently dropping —
        # otherwise the same finding would re-post next run.
        result = marker.parse_marker("<!-- daiv-cr {not valid json -->")
        assert result is None
        captured = capsys.readouterr()
        assert "corrupt daiv-cr marker" in captured.err


class TestParseNotes:
    @staticmethod
    def _note(body, *, note_id=1, author_username="daiv", system=False, resolved=False):
        return {
            "id": note_id,
            "body": body,
            "author": {"username": author_username},
            "system": system,
            "resolved": resolved,
        }

    def test_emits_inline_fingerprint(self):
        marker_line = marker.build_marker(
            "inline", sha="abc", archetype="remove_dead_lines", file="x.py", line=1, anchor="deadbeef"
        )
        discussions = [{"id": "d1", "notes": [self._note(marker_line)]}]
        out = marker.parse_notes(discussions)
        assert out["inline_fingerprints"] == [["inline", "remove_dead_lines", "x.py", "deadbeef"]]
        assert out["summary"] is None
        assert out["pending_replies"] == []

    def test_captures_summary_once(self):
        summary_line = marker.build_marker("summary", sha="abc")
        discussions = [
            {"id": "d1", "notes": [self._note(summary_line, note_id=10)]},
            {"id": "d2", "notes": [self._note(summary_line, note_id=20)]},
        ]
        out = marker.parse_notes(discussions)
        # The first summary wins; the second is ignored. This is the SKILL.md
        # contract: exactly one summary note per MR.
        assert out["summary"] == {"discussion_id": "d1", "note_id": 10}

    def test_pending_reply_when_user_follows_up(self):
        # daiv posted finding → user replied → thread unresolved.
        marker_line = marker.build_marker(
            "inline", sha="abc", archetype="remove_dead_lines", file="x.py", line=1, anchor="deadbeef"
        )
        discussions = [
            {
                "id": "d1",
                "notes": [
                    self._note(marker_line, note_id=1, author_username="daiv"),
                    self._note("not sure about this", note_id=2, author_username="user1"),
                ],
            }
        ]
        out = marker.parse_notes(discussions)
        assert len(out["pending_replies"]) == 1
        assert out["pending_replies"][0]["discussion_id"] == "d1"

    def test_resolved_threads_skip_pending_replies(self):
        # A resolved thread is closed conversation; no reply needed even if the
        # last note is from a human.
        marker_line = marker.build_marker(
            "inline", sha="abc", archetype="remove_dead_lines", file="x.py", line=1, anchor="deadbeef"
        )
        discussions = [
            {
                "id": "d1",
                "notes": [
                    self._note(marker_line, note_id=1, author_username="daiv", resolved=True),
                    self._note("noted, will fix", note_id=2, author_username="user1", resolved=True),
                ],
            }
        ]
        out = marker.parse_notes(discussions)
        assert out["pending_replies"] == []
        # The fingerprint still ships — resolved doesn't drop the marker from dedup.
        assert out["inline_fingerprints"] == [["inline", "remove_dead_lines", "x.py", "deadbeef"]]

    def test_threads_without_daiv_marker_ignored(self):
        # A pure-human discussion is invisible to the dedup state.
        discussions = [{"id": "d1", "notes": [self._note("user-only thread", author_username="user1")]}]
        out = marker.parse_notes(discussions)
        assert out == {"inline_fingerprints": [], "summary": None, "pending_replies": []}


class TestCli:
    def test_parse_notes_rejects_non_array_stdin(self, monkeypatch, capsys):
        # The script is called by the agent piping a JSON object instead of an
        # array — must exit non-zero with a useful stderr message, not crash.
        monkeypatch.setattr(sys, "stdin", _StringIOStdin('{"not": "an array"}'))
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes"])
        rc = marker.main()
        assert rc == 1
        assert "expected a JSON array" in capsys.readouterr().err

    def test_parse_notes_rejects_malformed_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _StringIOStdin("not json at all"))
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes"])
        rc = marker.main()
        assert rc == 1
        assert "invalid JSON" in capsys.readouterr().err

    def test_parse_notes_happy_path(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _StringIOStdin("[]"))
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes"])
        rc = marker.main()
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"inline_fingerprints": [], "summary": None, "pending_replies": []}


class _StringIOStdin:
    # ``sys.stdin`` replacement that returns a fixed string. ``json.load`` needs
    # a ``.read()`` method, and pytest's capsys doesn't replace stdin for us.
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
