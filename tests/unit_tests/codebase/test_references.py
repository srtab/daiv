import pytest
from pydantic import ValidationError

from codebase.base import GitPlatform, Issue, Scope, User
from codebase.references import (
    MAX_REFS_PER_SESSION,
    PROVIDER_GITHUB_ISSUE,
    PROVIDER_GITLAB_ISSUE,
    ExternalRef,
    RefIn,
    assemble_run_references,
    dedupe_refs,
    merge_stored_refs,
    refs_from_stored,
    render_commit_trailers,
    render_references_block,
)


class TestRefIn:
    def test_defaults(self):
        ref = RefIn(key="PROJ-123")
        assert ref.to_stored() == {"key": "PROJ-123", "provider": "generic", "url": "", "relation": "relates"}

    @pytest.mark.parametrize("key", ["", " PROJ", "a" * 65, "bad key", "<x>"])
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


class TestStoredRoundTrip:
    def test_refs_from_stored(self):
        raw = [{"key": "DAIV-1V", "provider": "sentry", "url": "https://s.io/1", "relation": "closes"}]
        assert refs_from_stored(raw) == (
            ExternalRef(key="DAIV-1V", provider="sentry", url="https://s.io/1", relation="closes"),
        )

    @pytest.mark.parametrize("raw", [None, "junk", {"key": "x"}, [{"key": ""}], [["not-a-dict"]], [{"provider": "x"}]])
    def test_malformed_entries_are_skipped_not_fatal(self, raw):
        assert refs_from_stored(raw) == ()

    def test_good_entries_survive_beside_bad_ones(self):
        raw = [{"key": ""}, {"key": "OK-1"}]
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


class TestAssemble:
    def _issue(self):
        return Issue(iid=42, title="t", author=User(id=1, username="u"))

    def test_derives_closing_issue_ref_for_issue_scope(self):
        refs = assemble_run_references((), scope=Scope.ISSUE, issue=self._issue(), git_platform=GitPlatform.GITLAB)
        assert refs == (ExternalRef(key="42", provider=PROVIDER_GITLAB_ISSUE, relation="closes"),)

    def test_github_platform_gets_github_provider(self):
        refs = assemble_run_references((), scope=Scope.ISSUE, issue=self._issue(), git_platform=GitPlatform.GITHUB)
        assert refs[0].provider == PROVIDER_GITHUB_ISSUE

    @pytest.mark.parametrize("scope,issue", [(Scope.GLOBAL, None), (Scope.ISSUE, None), (Scope.GLOBAL, "set")])
    def test_no_derivation_outside_issue_scope(self, scope, issue):
        issue_obj = self._issue() if issue else None
        declared = (ExternalRef(key="PROJ-1", provider="jira"),)
        result = assemble_run_references(declared, scope=scope, issue=issue_obj, git_platform=GitPlatform.GITLAB)
        assert result == declared

    def test_declared_and_derived_are_deduped(self):
        declared = (ExternalRef(key="42", provider=PROVIDER_GITLAB_ISSUE, relation="closes"),)
        refs = assemble_run_references(
            declared, scope=Scope.ISSUE, issue=self._issue(), git_platform=GitPlatform.GITLAB
        )
        assert len(refs) == 1

    def test_dedupe_refs_first_wins(self):
        a = ExternalRef(key="X", provider="jira", url="https://a")
        b = ExternalRef(key="X", provider="jira", url="https://b")
        assert dedupe_refs((a, b)) == (a,)


class TestRenderDescriptionBlock:
    def test_empty(self):
        assert render_references_block((), repo_slug="owner/repo") == ""

    def test_gitlab_issue_closes_is_byte_identical_to_legacy_footer(self):
        refs = (ExternalRef(key="42", provider="gitlab-issue", relation="closes"),)
        block = render_references_block(refs, repo_slug="owner/repo")
        assert block == "Closes: owner/repo#42+"

    def test_github_issue_closes_keeps_the_colon_no_plus(self):
        refs = (ExternalRef(key="42", provider="github-issue", relation="closes"),)
        block = render_references_block(refs, repo_slug="owner/repo")
        assert block == "Closes: owner/repo#42"

    def test_relates_issue_renders_related_to(self):
        refs = (ExternalRef(key="7", provider="gitlab-issue"),)
        block = render_references_block(refs, repo_slug="owner/repo")
        assert block == "Related to owner/repo#7"

    def test_sentry_closes_keeps_fixes_shortid_textually_matchable(self):
        refs = (ExternalRef(key="DAIV-1V", provider="sentry", url="https://s.io/1", relation="closes"),)
        block = render_references_block(refs, repo_slug="owner/repo")
        assert block == "**References:**\n- Fixes DAIV-1V ([Sentry](https://s.io/1))"

    def test_unknown_provider_degrades_to_link_bullet(self):
        refs = (ExternalRef(key="RT-77", provider="rt", url="https://rt.example.com/77"),)
        block = render_references_block(refs, repo_slug="owner/repo")
        assert block == "**References:**\n- [RT-77](https://rt.example.com/77)"

    def test_urlless_ref_renders_bare_key(self):
        refs = (ExternalRef(key="PROJ-9", provider="jira"),)
        block = render_references_block(refs, repo_slug="owner/repo")
        assert block == "**References:**\n- PROJ-9"

    def test_standalone_lines_precede_the_heading(self):
        refs = (
            ExternalRef(key="DAIV-1V", provider="sentry", relation="closes"),
            ExternalRef(key="42", provider="gitlab-issue", relation="closes"),
        )
        block = render_references_block(refs, repo_slug="owner/repo")
        assert block == "Closes: owner/repo#42+\n**References:**\n- Fixes DAIV-1V"


class TestRenderCommitTrailers:
    def test_sentry_closes_emits_documented_fixes_form(self):
        refs = (ExternalRef(key="DAIV-1V", provider="sentry", relation="closes"),)
        assert render_commit_trailers(refs) == ("Fixes DAIV-1V",)

    def test_sentry_relates_emits_nothing(self):
        assert render_commit_trailers((ExternalRef(key="DAIV-1V", provider="sentry"),)) == ()

    def test_jira_emits_refs_trailer(self):
        assert render_commit_trailers((ExternalRef(key="PROJ-9", provider="jira"),)) == ("Refs: PROJ-9",)

    def test_issue_and_generic_emit_nothing(self):
        refs = (
            ExternalRef(key="42", provider="gitlab-issue", relation="closes"),
            ExternalRef(key="RT-77", provider="rt"),
        )
        assert render_commit_trailers(refs) == ()
