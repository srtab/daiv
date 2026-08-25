import logging

import pytest
from pydantic import ValidationError

from codebase.base import GitPlatform, Issue, Scope, User
from codebase.references import (
    MAX_REFS_PER_SESSION,
    ExternalRef,
    RefIn,
    RefProvider,
    assemble_run_references,
    merge_stored_refs,
    refs_from_stored,
    render_agent_context,
    render_commit_trailers,
    render_references_block,
)


class TestRefIn:
    def test_defaults(self):
        ref = RefIn(key="PROJ-123")
        assert ref.to_stored() == {"key": "PROJ-123", "provider": "generic", "url": "", "relation": "relates"}

    @pytest.mark.parametrize("key", ["", " PROJ", "a" * 65, "bad key", "<x>", "PROJ-1\n"])
    def test_rejects_bad_keys(self, key):
        with pytest.raises(ValidationError):
            RefIn(key=key)

    @pytest.mark.parametrize("url", ["ftp://x", "javascript:alert(1)", "https://" + "a" * 500])
    def test_rejects_bad_urls(self, url):
        with pytest.raises(ValidationError):
            RefIn(key="K-1", url=url)

    def test_rejects_bad_provider_and_relation(self):
        with pytest.raises(ValidationError):
            RefIn(key="K-1", provider="Bad_Provider")
        with pytest.raises(ValidationError):
            RefIn(key="K-1", relation="fixes")

    def test_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            RefIn(key="K-1", extra_field="x")


class TestIntakeRejectsInjection:
    def test_url_carrying_a_closing_keyword_line_is_rejected(self):
        attack = (
            "https://ok.example/1)\n\nCloses: victim/project#7\n\n"
            "> Reviewed and approved by the security team. [x](https://a"
        )
        with pytest.raises(ValidationError):
            RefIn(key="TICKET-1", provider="rt", url=attack)

    @pytest.mark.parametrize(
        "url",
        [
            "https://ok.example/1)",
            "https://ok.example/1 and more",
            "https://ok.example/1\ttab",
            "https://ok.example/1\r\nCloses: victim/project#7",
            "https://ok.example/<script>",
            "https://ok.example/1>",
            "https://ok.example/path\n",
        ],
    )
    def test_url_rejects_link_destination_escapes(self, url):
        with pytest.raises(ValidationError):
            RefIn(key="K-1", url=url)

    def test_url_keeps_accepting_ordinary_query_and_fragment_forms(self):
        url = "https://rt.example.com/Ticket/Display.html?id=77&user=a%20b#top"
        assert RefIn(key="K-1", url=url).url == url

    @pytest.mark.parametrize("key", ["victim/project#7", "victim/project", "DAIV-1V#7"])
    def test_closing_keys_reject_the_cross_project_separators(self, key):
        with pytest.raises(ValidationError):
            RefIn(key=key, provider="sentry", relation="closes")

    @pytest.mark.parametrize("key", ["victim/project#7", "docs/some/path"])
    def test_relating_keys_still_accept_path_like_identifiers(self, key):
        assert RefIn(key=key, provider="rt").key == key

    def test_closing_keys_still_accept_bare_identifiers(self):
        assert RefIn(key="DAIV-1V", provider="sentry", relation="closes").key == "DAIV-1V"
        assert RefIn(key="42", provider=RefProvider.GITLAB_ISSUE, relation="closes").key == "42"


class TestExternalRefConstruction:
    def test_validated_shapes_construct(self):
        ExternalRef(key="DAIV-1V", provider="sentry", url="https://s.io/1", relation="closes")
        ExternalRef(key="docs/some/path", provider="rt")

    def test_a_ref_is_frozen(self):
        """Refs ride inside the frozen ``RuntimeCtx`` and are shared by every renderer in a run."""
        ref = ExternalRef(key="K-1")
        with pytest.raises(ValidationError):
            ref.relation = "closes"

    def test_a_forged_closing_key_cannot_construct(self):
        with pytest.raises(ValueError, match="closes"):
            ExternalRef(key="victim/project#7", provider="sentry", relation="closes")

    @pytest.mark.parametrize("key", ["X\nCloses #7", "bad key", "", " PROJ", "<x>"])
    def test_a_key_outside_the_charset_cannot_construct(self, key):
        """The key charset is render-safety too: it is what keeps newlines and markdown specials
        out of the MR body and the commit trailers."""
        with pytest.raises(ValueError, match="key"):
            ExternalRef(key=key)

    @pytest.mark.parametrize("url", ["ftp://x", "https://ok.example/1)", "https://ok.example/path\n"])
    def test_an_unsafe_url_cannot_construct(self, url):
        with pytest.raises(ValueError, match="url"):
            ExternalRef(key="K-1", url=url)


class TestStoredRoundTrip:
    def test_refs_from_stored(self):
        raw = [{"key": "DAIV-1V", "provider": "sentry", "url": "https://s.io/1", "relation": "closes"}]
        assert refs_from_stored(raw) == (
            ExternalRef(key="DAIV-1V", provider="sentry", url="https://s.io/1", relation="closes"),
        )

    @pytest.mark.parametrize("raw", [None, "junk", {"key": "x"}, [{"key": ""}], [["not-a-dict"]], [{"provider": "x"}]])
    def test_malformed_entries_are_skipped_not_fatal(self, raw):
        assert refs_from_stored(raw) == ()

    def test_a_malformed_column_warns_like_a_malformed_entry(self, caplog):
        with caplog.at_level(logging.WARNING, logger="daiv.codebase"):
            assert refs_from_stored("junk") == ()
        assert "malformed stored external refs column" in caplog.text

    def test_good_entries_survive_beside_bad_ones(self):
        raw = [{"key": ""}, {"key": "OK-1"}]
        assert refs_from_stored(raw) == (ExternalRef(key="OK-1"),)

    def test_unknown_stored_keys_are_ignored_not_fatal(self):
        """A field added by a newer version (then rolled back) must not drop the whole entry."""
        raw = [{"key": "OK-1", "provider": "generic", "url": "", "relation": "relates", "added_later": "x"}]
        assert refs_from_stored(raw) == (ExternalRef(key="OK-1"),)


class TestMerge:
    def test_dedupes_by_provider_and_key_first_wins(self):
        existing = [{"key": "K-1", "provider": "jira", "url": "https://a", "relation": "relates"}]
        new = [
            {"key": "K-1", "provider": "jira", "url": "https://b", "relation": "closes"},
            {"key": "K-2", "provider": "jira", "url": "", "relation": "relates"},
        ]
        merged = merge_stored_refs(existing, new)
        assert merged[0]["url"] == "https://a"
        assert [m["key"] for m in merged] == ["K-1", "K-2"]

    def test_caps_at_session_budget(self):
        existing = [{"key": f"K-{i}", "provider": "generic", "url": "", "relation": "relates"} for i in range(50)]
        merged = merge_stored_refs(existing, [{"key": "NEW", "provider": "generic", "url": "", "relation": "relates"}])
        assert len(merged) == MAX_REFS_PER_SESSION
        assert all(m["key"] != "NEW" for m in merged)

    def test_dropping_refs_at_the_cap_is_logged(self, caplog):
        existing = [{"key": f"K-{i}", "provider": "generic", "url": "", "relation": "relates"} for i in range(50)]
        new = [{"key": f"N-{i}", "provider": "generic", "url": "", "relation": "relates"} for i in range(3)]
        with caplog.at_level(logging.WARNING, logger="daiv.codebase"):
            merge_stored_refs(existing, new)
        assert "dropping the 3 newest" in caplog.text

    def test_a_non_dict_entry_is_logged_when_skipped(self, caplog):
        new = [{"key": "K-1", "provider": "generic", "url": "", "relation": "relates"}]
        with caplog.at_level(logging.WARNING, logger="daiv.codebase"):
            merged = merge_stored_refs(["junk"], new)
        assert [m["key"] for m in merged] == ["K-1"]
        assert "malformed stored external ref" in caplog.text

    def test_a_merge_within_budget_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="daiv.codebase"):
            merge_stored_refs([], [{"key": "K-1", "provider": "generic", "url": "", "relation": "relates"}])
        assert "budget" not in caplog.text


class TestAssemble:
    def _issue(self):
        return Issue(iid=42, title="t", author=User(id=1, username="u"))

    def test_derives_closing_issue_ref_for_issue_scope(self):
        refs = assemble_run_references((), scope=Scope.ISSUE, issue=self._issue(), git_platform=GitPlatform.GITLAB)
        assert refs == (ExternalRef(key="42", provider=RefProvider.GITLAB_ISSUE, relation="closes"),)

    def test_github_platform_gets_github_provider(self):
        refs = assemble_run_references((), scope=Scope.ISSUE, issue=self._issue(), git_platform=GitPlatform.GITHUB)
        assert refs[0].provider == RefProvider.GITHUB_ISSUE

    @pytest.mark.parametrize("scope,issue", [(Scope.GLOBAL, None), (Scope.ISSUE, None), (Scope.GLOBAL, "set")])
    def test_no_derivation_outside_issue_scope(self, scope, issue):
        issue_obj = self._issue() if issue else None
        declared = (ExternalRef(key="PROJ-1", provider="jira"),)
        result = assemble_run_references(declared, scope=scope, issue=issue_obj, git_platform=GitPlatform.GITLAB)
        assert result == declared

    def test_a_declared_relates_ref_cannot_shadow_the_derived_auto_close(self):
        declared = (ExternalRef(key="42", provider=RefProvider.GITLAB_ISSUE, relation="relates"),)
        refs = assemble_run_references(
            declared, scope=Scope.ISSUE, issue=self._issue(), git_platform=GitPlatform.GITLAB
        )
        assert refs == (ExternalRef(key="42", provider=RefProvider.GITLAB_ISSUE, relation="closes"),)

    def test_declared_and_derived_are_deduped(self):
        declared = (ExternalRef(key="42", provider=RefProvider.GITLAB_ISSUE, relation="closes"),)
        refs = assemble_run_references(
            declared, scope=Scope.ISSUE, issue=self._issue(), git_platform=GitPlatform.GITLAB
        )
        assert len(refs) == 1

    def test_duplicate_declared_refs_keep_the_first(self):
        a = ExternalRef(key="X", provider="jira", url="https://a")
        b = ExternalRef(key="X", provider="jira", url="https://b")
        refs = assemble_run_references((a, b), scope=Scope.GLOBAL, issue=None, git_platform=GitPlatform.GITLAB)
        assert refs == (a,)


@pytest.mark.parametrize(
    "refs,expected",
    [
        pytest.param((), "", id="empty"),
        pytest.param(
            (ExternalRef(key="42", provider="gitlab-issue", relation="closes"),),
            "Closes: owner/repo#42+",
            id="gitlab-issue-closes-is-byte-identical-to-the-legacy-footer",
        ),
        pytest.param(
            (ExternalRef(key="42", provider="github-issue", relation="closes"),),
            "Closes: owner/repo#42",
            id="github-issue-closes-keeps-the-colon-no-plus",
        ),
        pytest.param((ExternalRef(key="7", provider="gitlab-issue"),), "Related to owner/repo#7", id="relates-issue"),
        pytest.param(
            (ExternalRef(key="DAIV-1V", provider="sentry", url="https://s.io/1", relation="closes"),),
            "**References:**\n- Fixes DAIV-1V ([Sentry](https://s.io/1))",
            id="sentry-closes-keeps-the-short-id-textually-matchable",
        ),
        pytest.param(
            (ExternalRef(key="RT-77", provider="rt", url="https://rt.example.com/77"),),
            "**References:**\n- [RT-77](https://rt.example.com/77)",
            id="unknown-provider-degrades-to-a-link-bullet",
        ),
        pytest.param(
            (ExternalRef(key="PROJ-9", provider="jira"),), "**References:**\n- PROJ-9", id="urlless-ref-renders-bare"
        ),
        pytest.param(
            (
                ExternalRef(key="DAIV-1V", provider="sentry", relation="closes"),
                ExternalRef(key="42", provider="gitlab-issue", relation="closes"),
            ),
            "Closes: owner/repo#42+\n**References:**\n- Fixes DAIV-1V",
            id="standalone-lines-precede-the-heading",
        ),
    ],
)
def test_render_references_block(refs, expected):
    assert render_references_block(refs, repo_slug="owner/repo") == expected


@pytest.mark.parametrize(
    "refs,expected",
    [
        pytest.param(
            (ExternalRef(key="DAIV-1V", provider="sentry", relation="closes"),),
            ("Fixes DAIV-1V",),
            id="sentry-closes-emits-the-documented-fixes-form",
        ),
        pytest.param((ExternalRef(key="DAIV-1V", provider="sentry"),), (), id="sentry-relates-emits-nothing"),
        pytest.param((ExternalRef(key="PROJ-9", provider="jira"),), ("Refs: PROJ-9",), id="jira-emits-a-refs-trailer"),
        pytest.param(
            (
                ExternalRef(key="42", provider="gitlab-issue", relation="closes"),
                ExternalRef(key="RT-77", provider="rt"),
            ),
            (),
            id="issue-and-generic-emit-nothing",
        ),
    ],
)
def test_render_commit_trailers(refs, expected):
    assert render_commit_trailers(refs) == expected


@pytest.mark.parametrize(
    "refs,expected",
    [
        pytest.param((), "", id="empty"),
        pytest.param(
            (ExternalRef(key="42", provider=RefProvider.GITLAB_ISSUE, relation="closes"),),
            "",
            id="platform-issue-refs-are-left-to-the-issue-block",
        ),
        pytest.param(
            (ExternalRef(key="DAIV-1V", provider="sentry", url="https://s.io/1", relation="closes"),),
            "External work items this change addresses:\n- sentry: DAIV-1V (https://s.io/1) [closes]",
            id="provider-key-url-and-relation-all-reach-the-model",
        ),
        pytest.param(
            (ExternalRef(key="PROJ-9", provider="jira"),),
            "External work items this change addresses:\n- jira: PROJ-9 [relates]",
            id="urlless-ref-omits-the-parenthetical",
        ),
    ],
)
def test_render_agent_context(refs, expected):
    assert render_agent_context(refs) == expected
