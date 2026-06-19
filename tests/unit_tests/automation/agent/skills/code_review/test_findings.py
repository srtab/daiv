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

    def test_whitespace_only_required_field_dropped(self):
        valid, dropped = findings.validate([_f(title="   "), _f(rationale="\t\n")])
        assert valid == []
        assert dropped == 2

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
        # Fix archetypes key on (file, line, archetype) only, so two detectors flagging the
        # same concrete fix on one line still collapse cross-detector — strongest bar wins.
        a = _f(archetype="remove_dead_lines", detector="structure", bar="question", title="weak")
        b = _f(archetype="remove_dead_lines", detector="correctness", bar="defect", title="strong")
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

    def test_same_key_same_bar_keeps_first_seen(self):
        # Equal bar must NOT override: strict precedence keeps the first-seen finding so the
        # posted comment is stable across reruns. A `>` -> `>=` regression would flip this.
        # Fix archetype so the two detectors share a key (prose archetypes key on detector).
        a = _f(archetype="remove_dead_lines", detector="correctness", bar="defect", title="first")
        b = _f(archetype="remove_dead_lines", detector="structure", bar="defect", title="second")
        out = findings.dedupe([a, b])
        assert len(out) == 1
        assert out[0]["title"] == "first"

    def test_distinct_detectors_on_same_prose_line_kept(self):
        # `discussion`/`question` are catch-alls (review-workflow.md: "discussion for
        # everything else"), so different detectors on the same line are distinct findings.
        # The detector enters the key for prose archetypes, so all survive.
        disc_a = _f(archetype="discussion", detector="security", line=7, title="a")
        disc_b = _f(archetype="discussion", detector="structure", line=7, title="b")
        q_a = _f(archetype="question", detector="correctness", line=7, title="qa")
        q_b = _f(archetype="question", detector="performance", line=7, title="qb")
        out = findings.dedupe([disc_a, disc_b, q_a, q_b])
        assert {f["title"] for f in out} == {"a", "b", "qa", "qb"}

    def test_same_detector_same_prose_line_still_collapses(self):
        # A single detector repeating itself on one line still dedups (strongest bar wins),
        # so prose-keying-on-detector doesn't let a detector double-post the same spot.
        a = _f(archetype="discussion", detector="security", bar="structural", title="weak")
        b = _f(archetype="discussion", detector="security", bar="defect", title="strong")
        out = findings.dedupe([a, b])
        assert len(out) == 1
        assert out[0]["title"] == "strong"

    def test_custom_rules_source_preserved_against_collision(self):
        # A custom-rules violation and a security concern can both land on one line as
        # `discussion`. The custom-rules `source` must NOT vanish when another detector
        # shares the line — both survive because prose keys on detector.
        sec = _f(archetype="discussion", detector="security", bar="defect", title="sec")
        rule = _f(
            archetype="discussion",
            detector="custom-rules",
            bar="structural",
            title="rule",
            source="review-rules.md: payments calls need a timeout",
        )
        out = findings.dedupe([sec, rule])
        assert len(out) == 2
        by_detector = {f["detector"]: f for f in out}
        assert by_detector["custom-rules"]["source"] == "review-rules.md: payments calls need a timeout"

    def test_out_of_enum_bar_does_not_crash(self):
        # dedupe is module-public; a caller that skips validate (or BARS/_BAR_RANK drift) must
        # not trigger a KeyError on a bar outside _BAR_RANK — it sorts lowest instead.
        a = _f(bar="nonsense", title="a")
        b = _f(bar="nonsense", title="b")
        out = findings.dedupe([a, b])
        assert len(out) == 1
        assert out[0]["title"] == "a"


class TestMerge:
    def test_merge_happy_path(self):
        # _f(detector="bogus") fails validation -> counted in `dropped` (1).
        # _f(bar="question") shares the dedup key (file, line, archetype) with
        # _f() and is collapsed by dedupe -> NOT counted in `dropped`. So one
        # distinct valid finding survives: candidates == 1 (candidates = len(deduped)).
        result = findings.merge([_f(), _f(bar="question"), _f(detector="bogus")])
        assert result == {"findings": [_f()], "candidates": 1, "dropped": 1, "merged": 1}

    def test_distinct_prose_findings_on_same_line_both_survive(self):
        # Two genuinely different findings from different detectors on the same
        # (file, line, "discussion") must BOTH survive: `discussion` is a catch-all, so
        # collapsing them would silently drop one. Prose archetypes key on `detector`.
        a = _f(detector="security", title="sqli")
        b = _f(detector="performance", title="n+1")
        result = findings.merge([a, b])
        assert result["candidates"] == 2
        assert result["dropped"] == 0
        assert result["merged"] == 0
        assert {f["title"] for f in result["findings"]} == {"sqli", "n+1"}

    def test_same_detector_fix_archetype_collision_still_merges(self):
        # The merge still collapses true duplicates: two detectors flagging the same fix
        # archetype on one line reduce to one, surfaced via `merged` (not a schema `dropped`).
        a = _f(archetype="remove_dead_lines", detector="security", title="dead")
        b = _f(archetype="remove_dead_lines", detector="performance", title="dead-too")
        result = findings.merge([a, b])
        assert result["candidates"] == 1
        assert result["dropped"] == 0
        assert result["merged"] == 1

    def test_suggestion_field_survives_merge(self):
        f = _f(archetype="remove_dead_lines", suggestion="del x")
        result = findings.merge([f])
        assert result["findings"][0]["suggestion"] == "del x"

    def test_merge_empty_input(self):
        # A clean diff yields zero findings. The skill still runs merge in some paths and reads
        # candidates/dropped/merged for its Step 7 status line, so an empty array must reduce to
        # all-zero counts rather than crash.
        assert findings.merge([]) == {"findings": [], "candidates": 0, "dropped": 0, "merged": 0}


class TestReadFindingsFromFiles:
    def test_reads_object_files(self, tmp_path):
        p1 = tmp_path / "a.json"
        p1.write_text(json.dumps({"findings": [_f()]}), encoding="utf-8")
        p2 = tmp_path / "b.json"
        p2.write_text(json.dumps({"findings": [_f(bar="question")]}), encoding="utf-8")
        raw, skipped = findings.read_findings_from_files([str(p1), str(p2)])
        assert len(raw) == 2
        assert skipped == 0

    def test_tolerates_bare_array(self, tmp_path):
        p = tmp_path / "a.json"
        p.write_text(json.dumps([_f()]), encoding="utf-8")
        raw, skipped = findings.read_findings_from_files([str(p)])
        assert raw == [_f()]
        assert skipped == 0

    def test_skips_missing_file(self, tmp_path, capsys):
        p = tmp_path / "a.json"
        p.write_text(json.dumps({"findings": [_f()]}), encoding="utf-8")
        raw, skipped = findings.read_findings_from_files([str(tmp_path / "nope.json"), str(p)])
        assert len(raw) == 1
        assert skipped == 1
        assert "missing" in capsys.readouterr().err

    def test_skips_invalid_json(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        good = tmp_path / "g.json"
        good.write_text(json.dumps({"findings": [_f()]}), encoding="utf-8")
        raw, skipped = findings.read_findings_from_files([str(bad), str(good)])
        assert len(raw) == 1
        assert skipped == 1
        assert "unreadable" in capsys.readouterr().err

    def test_skips_non_list_findings_value(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"findings": "not a list"}), encoding="utf-8")
        good = tmp_path / "g.json"
        good.write_text(json.dumps({"findings": [_f()]}), encoding="utf-8")
        raw, skipped = findings.read_findings_from_files([str(bad), str(good)])
        assert len(raw) == 1
        assert skipped == 1
        assert "no 'findings' array" in capsys.readouterr().err


class TestMergeCli:
    def test_cli_merges_files(self, tmp_path, monkeypatch, capsys):
        p = tmp_path / "a.json"
        p.write_text(json.dumps({"findings": [_f()]}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["findings.py", "merge", str(p)])
        assert findings.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["candidates"] == 1
        assert out["skipped"] == 0

    def test_cli_no_paths_returns_zeros(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["findings.py", "merge"])
        assert findings.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"findings": [], "candidates": 0, "dropped": 0, "merged": 0, "skipped": 0}

    def test_cli_partial_skip_exits_zero_and_reports_skipped(self, tmp_path, monkeypatch, capsys):
        # One valid findings JSON + one .txt file (simulating a loop-stopped detector that emitted
        # an error message as plain text via DeferredOutputMiddleware). The CLI must exit 0 (partial
        # data is still usable), include the readable findings, AND report skipped == 1.
        good = tmp_path / "cr-correctness-abc.json"
        good.write_text(json.dumps({"findings": [_f()]}), encoding="utf-8")
        bad = tmp_path / "cr-security-xyz.txt"
        bad.write_text("ERROR: stopped after calling 'grep' 6 times in a row", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["findings.py", "merge", str(good), str(bad)])
        assert findings.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["candidates"] == 1
        assert out["skipped"] == 1

    def test_cli_all_files_skipped_returns_error(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "nope.json"
        monkeypatch.setattr(sys, "argv", ["findings.py", "merge", str(missing)])
        assert findings.main() == 1
        captured = capsys.readouterr()
        assert captured.out == ""  # no JSON emitted on the error path
        assert "were skipped" in captured.err


class TestSchemaSingleSource:
    def test_constants_derived_from_schema(self):
        # Guards against anyone replacing the schema-driven derivation with hardcoded values.
        schema = json.loads((_FINDINGS_PATH.parent / "finding.schema.json").read_text(encoding="utf-8"))
        props = schema["properties"]
        assert tuple(props["detector"]["enum"]) == findings.DETECTORS
        assert tuple(props["bar"]["enum"]) == findings.BARS
        assert tuple(props["archetype"]["enum"]) == findings.ARCHETYPES
        assert tuple(schema["required"]) == findings.REQUIRED_FIELDS

    def test_bar_rank_orders_defect_highest(self):
        # Independent guard on the explicit ranking: defect outranks structural outranks question.
        # A typo'd rank that inverted severity would be caught here.
        assert findings._BAR_RANK["defect"] > findings._BAR_RANK["structural"] > findings._BAR_RANK["question"]

    def test_bar_rank_is_explicit_and_covers_every_bar(self):
        # Severity ranking is declared EXPLICITLY in findings.py, not derived from the bar enum's
        # array position — so reordering the enum in finding.schema.json cannot silently invert
        # dedup severity. Every bar must carry an explicit, distinct rank: a new bar has to be
        # ranked, not silently defaulted to the lowest (findings.py raises at import otherwise).
        assert set(findings._BAR_RANK) == set(findings.BARS)
        assert len(set(findings._BAR_RANK.values())) == len(findings.BARS)

    def test_valid_finding_has_every_required_schema_field(self):
        schema = json.loads((_FINDINGS_PATH.parent / "finding.schema.json").read_text(encoding="utf-8"))
        sample = _f()
        assert findings.is_valid(sample)
        for field in schema["required"]:
            assert field in sample
