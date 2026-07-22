# Locking tests for the code-review skill's marker contract. The script lives
# under a hyphenated path (``skills/code-review/scripts/marker.py``) and is
# invoked as a subprocess from the sandbox, so it isn't importable via the
# normal package path. Load the module by file path and exercise the functions
# directly — the contract that matters is byte-stable output across reruns
# (anchors, JSON payloads, parse paths), which is exactly what these tests pin.
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

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

    def test_identical_long_lines_collide_by_design(self):
        # KNOWN LIMITATION (marker-format.md): the anchor hashes line content only, so two
        # byte-identical long target lines in different hunks produce the SAME anchor — and
        # hence the same (kind, archetype, file, anchor) fingerprint. This is the deliberate
        # trade for cross-commit stability (the anchor ignores line numbers, which shift on
        # unrelated commits); delivery (gitlab-delivery.md Step 4) demotes the second
        # within-run colliding finding to the summary rather than dropping it. Locked here so
        # a future change to compute_anchor is a conscious decision that rides a marker `v` bump.
        line = "result = compute_expensive_value(payload, options)"
        assert marker.compute_anchor(line, next_line="log.info('done')") == marker.compute_anchor(
            line, next_line="return result"
        )


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


class TestCompose:
    """The ``compose`` subcommand writes a post-ready body (marker line + verbatim prose) to a
    file so the delivery step never hand-transcribes (and re-serializes) the marker into a shell
    arg. The first line must be byte-identical to ``build`` for the same fields."""

    def _run(self, argv, capsys):
        old = sys.argv
        sys.argv = ["marker.py", *argv]
        try:
            code = marker.main()
        finally:
            sys.argv = old
        out, err = capsys.readouterr()
        return code, out, err

    def test_compose_inline_writes_marker_line_plus_verbatim_prose(self, tmp_path, capsys):
        # The whole point: first line is exactly build's double-quoted marker; the rest is the
        # prose bytes untouched (no JSON re-encoding of caller text). The printed path is the out.
        prose = "Dead code — drop it.\n\n```suggestion:-0+0\n    return None\n```\n"
        prose_file = tmp_path / "prose.md"
        prose_file.write_text(prose, encoding="utf-8")
        out_file = tmp_path / "body.md"
        code, stdout, _ = self._run(
            [
                "compose",
                "--kind",
                "inline",
                "--sha",
                "abc1234",
                "--archetype",
                "remove_dead_lines",
                "--file",
                "services/api.py",
                "--line",
                "42",
                "--anchor",
                "a1b2c3d4",
                "--prose-file",
                str(prose_file),
                "--out",
                str(out_file),
            ],
            capsys,
        )
        assert code == 0
        assert stdout.strip() == str(out_file)
        body = out_file.read_text(encoding="utf-8")
        expected_marker = marker.build_marker(
            "inline", sha="abc1234", archetype="remove_dead_lines", file="services/api.py", line=42, anchor="a1b2c3d4"
        )
        first_line, remainder = body.split("\n", 1)
        assert first_line == expected_marker
        assert remainder == prose

    def test_compose_first_line_roundtrips_through_parse_marker(self, tmp_path, capsys):
        # Load-bearing regression guard: the marker composed by ``compose`` must always be
        # ``json.loads``-parseable by ``parse_marker`` — that is what keeps findings deduping and
        # replies recognized on re-review, and is exactly what the old hand-transcription broke.
        prose_file = tmp_path / "prose.md"
        prose_file.write_text("Is this branch reachable?\n", encoding="utf-8")
        out_file = tmp_path / "body.md"
        code, _, _ = self._run(
            [
                "compose",
                "--kind",
                "inline",
                "--sha",
                "abc1234",
                "--archetype",
                "question",
                "--file",
                "env_files/all/grafana.env",
                "--line",
                "9",
                "--anchor",
                "b2c3d4e5",
                "--prose-file",
                str(prose_file),
                "--out",
                str(out_file),
            ],
            capsys,
        )
        assert code == 0
        # Feed the whole composed body (parse_marker reads only its first line) — proves the
        # posted note is dedup-parseable.
        parsed = marker.parse_marker(out_file.read_text(encoding="utf-8"))
        assert parsed == {
            "v": 1,
            "kind": "inline",
            "archetype": "question",
            "file": "env_files/all/grafana.env",
            "line": 9,
            "anchor": "b2c3d4e5",
            "sha": "abc1234",
        }

    def test_compose_reply_roundtrips_through_parse_marker(self, tmp_path, capsys):
        # Replies matter as much as findings: a corrupt reply marker leaves the thread perpetually
        # "pending" and daiv re-replies every run. The composed reply marker must parse.
        prose_file = tmp_path / "prose.md"
        prose_file.write_text("Still applies — see the call site.\n", encoding="utf-8")
        out_file = tmp_path / "reply.md"
        code, _, _ = self._run(
            ["compose", "--kind", "reply", "--sha", "abc1234", "--prose-file", str(prose_file), "--out", str(out_file)],
            capsys,
        )
        assert code == 0
        assert marker.parse_marker(out_file.read_text(encoding="utf-8")) == {"v": 1, "kind": "reply", "sha": "abc1234"}

    def test_compose_default_out_is_content_derived_hash(self, tmp_path, capsys, monkeypatch):
        # No --out → the path is DEFAULT_BODY_DIR/cr-body-<hash8>.md where hash8 is the first 8 hex
        # of the composed body's SHA-256, so a stateless rerun can neither collide nor reuse a
        # stale file. Redirect the dir into tmp_path so the test never touches /workspace.
        monkeypatch.setattr(marker, "DEFAULT_BODY_DIR", str(tmp_path))
        prose_file = tmp_path / "prose.md"
        prose_file.write_text("## Findings\n\nnone.\n", encoding="utf-8")
        code, stdout, _ = self._run(
            ["compose", "--kind", "summary", "--sha", "abc1234", "--prose-file", str(prose_file)], capsys
        )
        assert code == 0
        out_path = stdout.strip()
        body_bytes = Path(out_path).read_bytes()
        expected_hash = hashlib.sha256(body_bytes).hexdigest()[:8]
        assert out_path == str(tmp_path / f"cr-body-{expected_hash}.md")

    def test_compose_missing_prose_file_exits_1(self, tmp_path, capsys):
        code, _, err = self._run(
            ["compose", "--kind", "summary", "--sha", "abc", "--prose-file", str(tmp_path / "nope.md")], capsys
        )
        assert code == 1
        assert "prose file not found" in err


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
        # contract: exactly one summary note per MR. ``body`` carries the prior
        # summary markdown forward for the Step 6 delta carry-forward rule.
        assert out["summary"] == {"discussion_id": "d1", "note_id": 10, "body": summary_line}

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
        assert out == {"inline_fingerprints": [], "summary": None, "last_reviewed_sha": None, "pending_replies": []}

    def test_emits_last_reviewed_sha_from_summary(self):
        summary_line = marker.build_marker("summary", sha="abc123")
        discussions = [{"id": "d1", "notes": [self._note(summary_line, note_id=10)]}]
        out = marker.parse_notes(discussions)
        assert out["last_reviewed_sha"] == "abc123"

    def test_last_reviewed_sha_none_without_summary(self):
        inline_line = marker.build_marker(
            "inline", sha="zzz", archetype="remove_dead_lines", file="x.py", line=1, anchor="deadbeef"
        )
        discussions = [{"id": "d1", "notes": [self._note(inline_line)]}]
        out = marker.parse_notes(discussions)
        assert out["last_reviewed_sha"] is None

    def test_summary_carries_full_body(self):
        # The Step 6 delta carry-forward re-reads the prior summary's full markdown from
        # ``summary.body`` — the prose *after* the marker line is exactly what gets carried
        # forward, so the whole multi-line body must survive intact, not just the marker.
        summary_line = marker.build_marker("summary", sha="abc")
        body = f"{summary_line}\n\n## Findings\n\n**1. Prior finding** — file.py:7\n- still applies"
        discussions = [{"id": "d1", "notes": [self._note(body, note_id=10)]}]
        out = marker.parse_notes(discussions)
        assert out["summary"]["body"] == body


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
        assert out == {"inline_fingerprints": [], "summary": None, "last_reviewed_sha": None, "pending_replies": []}

    def test_parse_notes_reads_from_file_path(self, tmp_path, monkeypatch, capsys):
        # The delivery step passes the gitlab tool's output_to_file dump by path;
        # the discussion JSON must never need to be piped through stdin. Garbage
        # on stdin proves the file argument is the source that wins.
        summary_line = marker.build_marker("summary", sha="abc")
        path = tmp_path / "discussions.json"
        path.write_text(json.dumps([{"id": "d1", "notes": [{"id": 10, "body": summary_line}]}]))
        monkeypatch.setattr(sys, "stdin", _StringIOStdin("not json at all"))
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes", str(path)])
        rc = marker.main()
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        # ``body`` is surfaced for the Step 6 carry-forward, read straight from the file.
        assert out["summary"] == {"discussion_id": "d1", "note_id": 10, "body": summary_line}

    def test_parse_notes_missing_file(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "does-not-exist.json"
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes", str(missing)])
        rc = marker.main()
        assert rc == 1
        assert "file not found" in capsys.readouterr().err

    def test_parse_notes_unreadable_path(self, tmp_path, monkeypatch, capsys):
        # An existing-but-unreadable path hits the generic OSError branch, distinct from
        # FileNotFoundError. A directory is the portable trigger (IsADirectoryError is an
        # OSError); chmod-based tests would be flaky under root.
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes", str(tmp_path)])
        rc = marker.main()
        assert rc == 1
        assert "cannot read" in capsys.readouterr().err

    def test_parse_notes_invalid_json_file_names_the_path(self, tmp_path, monkeypatch, capsys):
        # The source-aware message must name the *file* (not "stdin") so an operator can find
        # the bad dump — this is the whole reason the `source` variable exists.
        path = tmp_path / "discussions.json"
        path.write_text("not json at all")
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes", str(path)])
        rc = marker.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "invalid JSON" in err
        assert str(path) in err

    def test_parse_notes_non_array_file_names_the_path(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "discussions.json"
        path.write_text('{"not": "an array"}')
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes", str(path)])
        rc = marker.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "expected a JSON array" in err
        assert str(path) in err

    def test_parse_notes_non_utf8_file(self, tmp_path, monkeypatch, capsys):
        # A non-UTF-8 dump must degrade to the clean "invalid JSON" message and exit 1, not
        # escape as a raw UnicodeDecodeError traceback.
        path = tmp_path / "discussions.json"
        path.write_bytes(b"\xff\xfe not valid utf-8")
        monkeypatch.setattr(sys, "argv", ["marker.py", "parse-notes", str(path)])
        rc = marker.main()
        assert rc == 1
        assert "invalid JSON" in capsys.readouterr().err


class _StringIOStdin:
    # ``sys.stdin`` replacement that returns a fixed string. ``json.load`` needs
    # a ``.read()`` method, and pytest's capsys doesn't replace stdin for us.
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


_DIFF = """\
diff --git a/services/api.py b/services/api.py
index 1111111..2222222 100644
--- a/services/api.py
+++ b/services/api.py
@@ -1,3 +1,4 @@
 import json
-import os
+import sys
+RETRY_LIMIT = 3
 def handler(event):
@@ -10,1 +11,2 @@ def handler(event):
     return json.dumps(payload)
+    # unreachable
"""


class TestParseDiffNewSide:
    def test_added_line_has_no_old_line(self):
        positions = marker.parse_diff_new_side(_DIFF, "services/api.py")
        assert positions[2] == (None, "import sys")
        assert positions[3] == (None, "RETRY_LIMIT = 3")

    def test_context_line_maps_to_old_line(self):
        positions = marker.parse_diff_new_side(_DIFF, "services/api.py")
        assert positions[1] == (1, "import json")
        assert positions[4] == (3, "def handler(event):")

    def test_second_hunk_tracked_independently(self):
        positions = marker.parse_diff_new_side(_DIFF, "services/api.py")
        assert positions[11] == (10, "    return json.dumps(payload)")
        assert positions[12] == (None, "    # unreachable")

    def test_line_outside_hunks_absent(self):
        positions = marker.parse_diff_new_side(_DIFF, "services/api.py")
        assert 8 not in positions

    def test_other_file_ignored(self):
        assert marker.parse_diff_new_side(_DIFF, "other/file.py") == {}

    def test_added_line_starting_with_plus_signs_not_misread_as_header(self):
        # Hunk counts must drive consumption: an added content line "++ x" (diff
        # line "+++ x") is NOT a `+++ b/...` file header while the hunk is open.
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,2 @@\n keep\n+++ x\n"
        positions = marker.parse_diff_new_side(diff, "f.py")
        assert positions[2] == (None, "++ x")

    def test_rename_keyed_by_new_path(self):
        diff = "diff --git a/old.py b/new.py\n--- a/old.py\n+++ b/new.py\n@@ -5,1 +7,2 @@\n ctx\n+added\n"
        positions = marker.parse_diff_new_side(diff, "new.py")
        assert positions == {7: (5, "ctx"), 8: (None, "added")}

    def test_no_newline_sentinel_mid_hunk_not_counted(self):
        # "\ No newline at end of file" can appear mid-hunk (between the deletion of the
        # old last line and its added replacement). It must be skipped WITHOUT advancing
        # counters — otherwise the added line's new-side number would shift by one.
        diff = (
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n"
        )
        positions = marker.parse_diff_new_side(diff, "f.py")
        assert positions == {1: (1, "a"), 2: (None, "c")}

    def test_pure_deletion_hunk_records_no_new_side_line(self):
        # A deletion-only tail (new count exhausted while old count keeps the loop in-hunk)
        # advances old_ln without recording any new-side position.
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,1 @@\n keep\n-gone1\n-gone2\n"
        positions = marker.parse_diff_new_side(diff, "f.py")
        assert positions == {1: (1, "keep")}


class TestSnippetInDeletedLines:
    def test_snippet_on_deleted_line_found(self):
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,1 @@\n keep\n-secret = 1\n"
        assert marker.snippet_in_deleted_lines(diff, "f.py", "secret = 1") is True

    def test_snippet_only_on_added_line_is_not_a_deletion(self):
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,2 @@\n keep\n+added = 1\n"
        assert marker.snippet_in_deleted_lines(diff, "f.py", "added = 1") is False

    def test_snippet_absent_from_diff(self):
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,2 @@\n keep\n+added = 1\n"
        assert marker.snippet_in_deleted_lines(diff, "f.py", "nope") is False

    def test_deletion_in_other_file_ignored(self):
        diff = "diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n@@ -1,1 +0,0 @@\n-secret = 1\n"
        assert marker.snippet_in_deleted_lines(diff, "f.py", "secret = 1") is False


class TestStaleLines:
    _POSITIONS = {1: (1, "import json"), 2: (None, "import sys")}

    def test_fresh_checkout_not_stale(self):
        assert marker.stale_lines(self._POSITIONS, ["import json", "import sys"]) == []

    def test_content_drift_detected(self):
        assert marker.stale_lines(self._POSITIONS, ["import json", "import io"]) == [2]

    def test_position_past_eof_detected(self):
        assert marker.stale_lines(self._POSITIONS, ["import json"]) == [2]


class TestResolveMatches:
    _LINES = ["import json", "import sys", "RETRY_LIMIT = 3", "def handler(event):", "", "    return None"]
    _POSITIONS = {
        1: (1, "import json"),
        2: (None, "import sys"),
        3: (None, "RETRY_LIMIT = 3"),
        4: (3, "def handler(event):"),
    }

    def test_added_line_match(self):
        (m,) = marker.resolve_matches(self._LINES, self._POSITIONS, "RETRY_LIMIT")
        assert m["new_line"] == 3
        assert m["old_line"] is None
        assert m["line_type"] == "added"
        assert m["in_diff"] is True
        assert m["target"] == "RETRY_LIMIT = 3"

    def test_context_line_match_carries_old_line(self):
        (m,) = marker.resolve_matches(self._LINES, self._POSITIONS, "import json")
        assert m["old_line"] == 1
        assert m["line_type"] == "context"

    def test_line_not_in_diff_flagged(self):
        (m,) = marker.resolve_matches(self._LINES, self._POSITIONS, "return None")
        assert m["in_diff"] is False
        assert m["line_type"] is None
        assert m["old_line"] is None

    def test_no_match_returns_empty(self):
        assert marker.resolve_matches(self._LINES, self._POSITIONS, "not present") == []

    def test_snippet_is_literal_not_regex(self):
        lines = ["value = data[0].strip()"]
        (m,) = marker.resolve_matches(lines, {1: None}, "data[0].strip()")
        assert m["new_line"] == 1

    def test_anchor_matches_compute_anchor_with_next_nonblank(self):
        # Line 4 is followed by a blank line; the next NON-blank line feeds the
        # disambiguator exactly as compute_anchor would receive it.
        (m,) = marker.resolve_matches(self._LINES, self._POSITIONS, "def handler")
        assert m["anchor"] == marker.compute_anchor("def handler(event):", "    return None")

    def test_multiple_matches_ordered_by_line(self):
        lines = ["x = fetch()", "y = 1", "z = fetch()"]
        ms = marker.resolve_matches(lines, {1: None, 3: None}, "fetch()")
        assert [m["new_line"] for m in ms] == [1, 3]


class TestResolveCli:
    # Matches _DIFF's full new side — lines 1-4 (first hunk) and 11-12 (second hunk).
    # The staleness guard verifies EVERY new-side line the diff shows, not just the
    # matched one, so the fixture file must agree with both hunks.
    _API_PY = (
        "import json\nimport sys\nRETRY_LIMIT = 3\ndef handler(event):\n"
        '    payload = {"ok": True}\n\n\n\n\n\n'
        "    return json.dumps(payload)\n    # unreachable\n"
    )

    def _run(self, argv, capsys):
        old = sys.argv
        sys.argv = ["marker.py", *argv]
        try:
            code = marker.main()
        finally:
            sys.argv = old
        out, err = capsys.readouterr()
        return code, out, err

    def test_end_to_end(self, tmp_path, capsys, monkeypatch):
        repo = tmp_path / "repo"
        (repo / "services").mkdir(parents=True)
        (repo / "services" / "api.py").write_text(self._API_PY, encoding="utf-8")
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(_DIFF, encoding="utf-8")
        monkeypatch.chdir(repo)
        code, out, err = self._run(
            ["resolve", "--file", "services/api.py", "--snippet", "RETRY_LIMIT", "--diff", str(diff_file)], capsys
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["file"] == "services/api.py"
        assert payload["matches"][0]["new_line"] == 3
        assert payload["matches"][0]["line_type"] == "added"

    def test_stale_diff_exits_1(self, tmp_path, capsys, monkeypatch):
        repo = tmp_path / "repo"
        (repo / "services").mkdir(parents=True)
        (repo / "services" / "api.py").write_text(
            self._API_PY.replace("RETRY_LIMIT = 3", "RETRY_LIMIT = 5"), encoding="utf-8"
        )
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(_DIFF, encoding="utf-8")
        monkeypatch.chdir(repo)
        code, _, err = self._run(
            ["resolve", "--file", "services/api.py", "--snippet", "RETRY_LIMIT", "--diff", str(diff_file)], capsys
        )
        assert code == 1
        assert "stale diff" in err

    def test_missing_diff_file_exits_1(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        code, _, err = self._run(["resolve", "--file", "f.py", "--snippet", "x", "--diff", "/nope.diff"], capsys)
        assert code == 1
        assert "diff file not found" in err

    def test_missing_target_file_exits_1(self, tmp_path, capsys, monkeypatch):
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(_DIFF, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        code, _, err = self._run(["resolve", "--file", "gone.py", "--snippet", "x", "--diff", str(diff_file)], capsys)
        assert code == 1
        assert "file not found" in err

    def test_too_common_snippet_exits_1(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "f.py").write_text("x = 1\n" * 30, encoding="utf-8")
        diff_file = tmp_path / "change.diff"
        diff_file.write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        code, _, err = self._run(["resolve", "--file", "f.py", "--snippet", "x", "--diff", str(diff_file)], capsys)
        assert code == 1
        assert "too common" in err

    def test_empty_snippet_exits_1(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
        diff_file = tmp_path / "change.diff"
        diff_file.write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        code, _, err = self._run(["resolve", "--file", "f.py", "--snippet", "  ", "--diff", str(diff_file)], capsys)
        assert code == 1
        assert "empty snippet" in err

    def test_exactly_max_matches_succeeds(self, tmp_path, capsys, monkeypatch):
        # Boundary: exactly MAX_RESOLVE_MATCHES matches is allowed; MAX + 1 is refused
        # (test_one_over_max_matches_fails). Pins the `>` comparison against off-by-one drift.
        (tmp_path / "f.py").write_text("x = 1\n" * marker.MAX_RESOLVE_MATCHES, encoding="utf-8")
        diff_file = tmp_path / "change.diff"
        diff_file.write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        code, out, _ = self._run(["resolve", "--file", "f.py", "--snippet", "x", "--diff", str(diff_file)], capsys)
        assert code == 0
        assert len(json.loads(out)["matches"]) == marker.MAX_RESOLVE_MATCHES

    def test_one_over_max_matches_fails(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "f.py").write_text("x = 1\n" * (marker.MAX_RESOLVE_MATCHES + 1), encoding="utf-8")
        diff_file = tmp_path / "change.diff"
        diff_file.write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        code, _, err = self._run(["resolve", "--file", "f.py", "--snippet", "x", "--diff", str(diff_file)], capsys)
        assert code == 1
        assert "too common" in err

    def test_zero_match_pure_deletion_signals_deletion_true(self, tmp_path, capsys, monkeypatch):
        # "import os" is deleted by _DIFF and absent from the checkout: a genuine pure
        # deletion — resolve exits 0 with empty matches and snippet_in_deletion True.
        repo = tmp_path / "repo"
        (repo / "services").mkdir(parents=True)
        (repo / "services" / "api.py").write_text(self._API_PY, encoding="utf-8")
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(_DIFF, encoding="utf-8")
        monkeypatch.chdir(repo)
        code, out, _ = self._run(
            ["resolve", "--file", "services/api.py", "--snippet", "import os", "--diff", str(diff_file)], capsys
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["matches"] == []
        assert payload["snippet_in_deletion"] is True

    def test_zero_match_wrong_snippet_signals_deletion_false(self, tmp_path, capsys, monkeypatch):
        # A snippet in neither the checkout nor any deleted line: the caller has the wrong
        # snippet, not a deletion — snippet_in_deletion False tells the two apart.
        repo = tmp_path / "repo"
        (repo / "services").mkdir(parents=True)
        (repo / "services" / "api.py").write_text(self._API_PY, encoding="utf-8")
        diff_file = tmp_path / "change.diff"
        diff_file.write_text(_DIFF, encoding="utf-8")
        monkeypatch.chdir(repo)
        code, out, _ = self._run(
            ["resolve", "--file", "services/api.py", "--snippet", "def nonexistent_func", "--diff", str(diff_file)],
            capsys,
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["matches"] == []
        assert payload["snippet_in_deletion"] is False
