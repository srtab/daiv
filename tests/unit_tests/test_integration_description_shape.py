"""Coverage for the integration suite's description-shape checker.

The suite's LLM judge grades semantic correctness against a reference output, so it cannot see
whether a description padded itself with a bullet section or ran to 300 words. These checks are
what hold the prose-first shape, and they live in the fast suite because
``make integration-tests`` deselects everything outside the ``diff_to_metadata`` marker.
"""

import os
import subprocess  # noqa: S404
import sys
from pathlib import Path

import pytest

from tests.integration_tests.description_shape import prose_word_count, shape_violations, validate_expectation


class TestKeyChangesSection:
    def test_a_bullet_section_on_a_single_concern_change_is_a_violation(self):
        description = "Adds a /health endpoint.\n\n**Key Changes:**\n- Added the route.\n- Wired the router."

        assert shape_violations(description, {"no_key_changes": True})

    def test_prose_alone_satisfies_it(self):
        description = "Adds an unauthenticated /health endpoint so the load balancer can probe the API."

        assert shape_violations(description, {"no_key_changes": True}) == []

    def test_a_bare_bullet_list_counts_even_without_the_heading(self):
        """Dropping the heading and keeping the bullets is the obvious way to satisfy the letter of
        the rule while still restating the diff."""
        description = "Adds a /health endpoint.\n\n- Added the route.\n- Wired the router."

        assert shape_violations(description, {"no_key_changes": True})

    def test_a_hyphenated_sentence_is_not_a_bullet(self):
        description = "Adds a /health endpoint - unauthenticated, for load-balancer probes."

        assert shape_violations(description, {"no_key_changes": True}) == []


class TestWordBudget:
    def test_over_budget_is_a_violation(self):
        assert shape_violations(" ".join(["word"] * 40), {"max_words": 30})

    def test_at_budget_passes(self):
        assert shape_violations(" ".join(["word"] * 30), {"max_words": 30}) == []

    def test_markdown_punctuation_is_not_counted_as_words(self):
        """A bullet's ``-`` and the ``**`` around a heading are punctuation, not prose. Heading
        *text* still counts — a reviewer reads it — so only the syntax is stripped."""
        description = "**Bold** word\n- one two"

        assert shape_violations(description, {"max_words": 4}) == []
        assert shape_violations(description, {"max_words": 3})


class TestNotes:
    def test_missing_notes_is_a_violation_when_expected(self):
        description = "Adds a /health endpoint."

        assert shape_violations(description, {"has_notes": True})

    def test_present_notes_satisfies_it(self):
        description = "Adds a /health endpoint.\n\n**Notes:** the test suite was not run."

        assert shape_violations(description, {"has_notes": True}) == []

    def test_notes_present_on_a_clean_run_is_a_violation(self):
        """A run with nothing to caveat must not invent a Notes section — a 'nothing to report'
        line is exactly the padding this redesign removes."""
        description = "Adds a /health endpoint.\n\n**Notes:** nothing to report."

        assert shape_violations(description, {"no_notes": True})

    def test_no_notes_on_a_clean_run_passes(self):
        assert shape_violations("Adds a /health endpoint.", {"no_notes": True}) == []


class TestExpectations:
    def test_an_empty_expectation_checks_nothing(self):
        assert shape_violations("anything at all, at any length", {}) == []

    def test_every_violation_is_reported_not_just_the_first(self):
        """A failure message naming one problem sends you round the loop once per problem."""
        description = "**Key Changes:**\n- " + " ".join(["word"] * 50)

        assert len(shape_violations(description, {"no_key_changes": True, "max_words": 10, "has_notes": True})) == 3

    def test_an_unknown_expectation_key_is_rejected(self):
        """A typo in cases.jsonl would otherwise silently check nothing."""
        with pytest.raises(ValueError, match="max_word"):
            shape_violations("text", {"max_word": 10})


class TestNotesHeadingForms:
    """The prompt asks for ``**Notes:**``, but a model that reaches for a markdown heading or a
    bare label must not slip a 'nothing to report' line past ``no_notes``."""

    def test_a_markdown_heading_counts(self):
        assert shape_violations("Adds an endpoint.\n\n## Notes\nSuite not run.", {"no_notes": True})

    def test_a_bare_label_counts(self):
        assert shape_violations("Adds an endpoint.\n\nNotes: suite not run.", {"no_notes": True})

    def test_a_bold_label_with_the_colon_outside_counts(self):
        assert shape_violations("Adds an endpoint.\n\n**Notes**: suite not run.", {"no_notes": True})

    def test_every_form_satisfies_has_notes_too(self):
        for description in (
            "Adds an endpoint.\n\n## Notes\nSuite not run.",
            "Adds an endpoint.\n\nNotes: suite not run.",
            "Adds an endpoint.\n\n**Notes**: suite not run.",
            "Adds an endpoint.\n\n**Notes:** suite not run.",
        ):
            assert shape_violations(description, {"has_notes": True}) == [], description

    def test_the_word_note_inside_a_sentence_is_not_a_section(self):
        """``no_notes`` must not fire on prose that happens to use the word."""
        description = "Raises the timeout. Note that the worker still exits on SIGTERM."

        assert shape_violations(description, {"no_notes": True}) == []


class TestWordCountingIdentifiers:
    def test_a_snake_case_identifier_is_one_word(self):
        """Stripping ``_`` to a space would score `TASK_TIMEOUT_SECONDS` as three words, making the
        budget stricter for a Python codebase than for a JS one."""
        assert prose_word_count("Raises `TASK_TIMEOUT_SECONDS` from 60 to 300.") == 6

    def test_emphasis_markers_still_do_not_count(self):
        assert prose_word_count("**bold** _italic_ `code`") == 3

    def test_a_backticked_dotted_path_is_one_word(self):
        assert prose_word_count("Calls `ApiClient.get` once.") == 3


class TestSiblingFieldLeak:
    """A description that restates the commit message, branch, or title renders as visible garbage
    at the bottom of the MR page. Checked on every case, never opt-in: there is no diff for which
    it is correct."""

    def test_a_restated_commit_message_is_a_violation(self):
        description = "Guards against a deleted user.\n\nCommit message: `482 fix: handle deleted users`"

        assert shape_violations(description, {})

    def test_a_restated_branch_is_a_violation(self):
        description = "Guards against a deleted user.\n\nBranch: fix/guard-null-user"

        assert shape_violations(description, {})

    def test_a_restated_title_is_a_violation(self):
        description = "Guards against a deleted user.\n\nTitle: Guard against a deleted user"

        assert shape_violations(description, {})

    def test_prose_mentioning_a_commit_is_not_a_leak(self):
        """``no_key_changes`` cases talk about commits and branches legitimately."""
        description = "Reverts the commit that raised the timeout, restoring the branch to its prior behavior."

        assert shape_violations(description, {}) == []

    def test_the_leak_is_reported_alongside_other_violations(self):
        description = "**Key Changes:**\n- one\n\nCommit message: `fix: x`"

        assert len(shape_violations(description, {"no_key_changes": True})) == 2


class TestOrderedLists:
    """Numbering the same restated hunks is the same padding, and a model reaches for `1.` as
    readily as `-` once the heading is gone."""

    def test_a_numbered_list_counts_as_a_bullet_list(self):
        description = "Adds a /health endpoint.\n\n1. Added the route.\n2. Wired the router."

        assert shape_violations(description, {"no_key_changes": True})

    def test_a_parenthesised_numbered_list_counts(self):
        description = "Adds a /health endpoint.\n\n1) Added the route.\n2) Wired the router."

        assert shape_violations(description, {"no_key_changes": True})

    def test_a_numbered_sentence_reference_is_not_a_list(self):
        assert shape_violations("Fixes the bug reported in 1. of the issue.", {"no_key_changes": True}) == []


class TestSectionLabelsInsideListItems:
    """Both section checks are line-anchored, and a bullet is the form a model reaches for when
    the prompt asked for bullets two paragraphs earlier."""

    def test_a_bulleted_notes_label_counts_as_a_notes_section(self):
        assert shape_violations("Adds it.\n\n- Notes: suite not run.", {"no_notes": True})

    def test_a_bulleted_notes_label_satisfies_has_notes(self):
        """The worse half: this used to report the caveat as missing on a description that had it."""
        assert shape_violations("Adds it.\n\n- Notes: suite not run.", {"has_notes": True}) == []

    def test_a_bulleted_field_leak_is_detected(self):
        assert shape_violations("Guards it.\n\n- Commit message: `fix: x`", {})


class TestKeyChangesIsLineAnchored:
    def test_the_phrase_inside_a_sentence_is_not_a_heading(self):
        """`_NOTES_HEADING` and `_FIELD_LEAK` were anchored; this one was not."""
        assert shape_violations("The key changes here are internal only.", {"no_key_changes": True}) == []

    def test_a_real_heading_is_still_caught(self):
        assert shape_violations("Adds it.\n\n**Key Changes:**\n- a\n- b", {"no_key_changes": True})


class TestExpectationValidation:
    """Called from ``load_cases``, so a malformed block fails at collection rather than after one
    paid model call per case per model."""

    def test_contradictory_notes_expectations_are_rejected(self):
        with pytest.raises(ValueError, match="contradictory"):
            validate_expectation({"has_notes": True, "no_notes": True})

    def test_a_string_budget_is_rejected(self):
        """It used to reach the comparison and raise TypeError mid-test instead."""
        with pytest.raises(ValueError, match="max_words must be an int"):
            validate_expectation({"max_words": "70"})

    def test_a_bool_budget_is_rejected(self):
        with pytest.raises(ValueError, match="max_words must be an int"):
            validate_expectation({"max_words": True})

    def test_a_non_bool_flag_is_rejected(self):
        with pytest.raises(ValueError, match="has_notes must be a bool"):
            validate_expectation({"has_notes": "yes"})

    def test_a_well_formed_block_passes(self):
        validate_expectation({"no_key_changes": True, "max_words": 70, "no_notes": True})


class TestTheCredentialGuardScope:
    """``testpaths`` points bare ``pytest`` at all of ``tests/``, so a session-wide raise from the
    integration conftest took the unit suite down with it — 4889 tests collected, none run."""

    @staticmethod
    def _run(args: list[str], *, key: str | None) -> subprocess.CompletedProcess:
        env = {**os.environ, "LANGCHAIN_TRACING_V2": "false"}
        env.pop("OPENROUTER_API_KEY", None)
        if key is not None:
            env["OPENROUTER_API_KEY"] = key
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "--no-cov", "-q", "--collect-only", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=Path(__file__).resolve().parents[2],
        )

    def test_a_wider_run_still_collects_without_the_key(self):
        args = ["tests/unit_tests/test_integration_description_shape.py", "tests/integration_tests"]
        result = self._run(args, key=None)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_integration_only_run_refuses_without_the_key(self):
        """Otherwise every test skips and pytest exits 0 — how this suite sat unrunnable and green."""
        result = self._run(["tests/integration_tests"], key=None)

        assert result.returncode != 0
        assert "OPENROUTER_API_KEY" in result.stdout + result.stderr

    def test_an_integration_only_run_collects_with_the_key(self):
        result = self._run(["tests/integration_tests"], key="dummy")

        assert result.returncode == 0, result.stdout + result.stderr
