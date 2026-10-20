import pytest
from sessions.ids import compute_thread_id

from codebase.base import Scope


class TestComputeThreadId:
    def test_deterministic(self):
        a = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)
        b = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)
        assert a == b

    def test_scope_distinguishes(self):
        issue = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)
        mr = compute_thread_id(repo_slug="owner/repo", scope=Scope.MERGE_REQUEST, entity_iid=42)
        assert issue != mr

    def test_entity_iid_distinguishes(self):
        a = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)
        b = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=43)
        assert a != b

    def test_repo_slug_distinguishes(self):
        a = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)
        b = compute_thread_id(repo_slug="other/repo", scope=Scope.ISSUE, entity_iid=42)
        assert a != b

    @pytest.mark.parametrize(
        ("repo_slug", "scope", "entity_iid"),
        [
            ("", Scope.ISSUE, 42),
            ("owner/repo", None, 42),
            ("owner/repo", Scope.ISSUE, None),
            ("owner/repo", Scope.ISSUE, ""),
        ],
    )
    def test_rejects_falsy_inputs(self, repo_slug, scope, entity_iid):
        with pytest.raises(ValueError):
            compute_thread_id(repo_slug=repo_slug, scope=scope, entity_iid=entity_iid)

    @pytest.mark.parametrize(
        ("repo_slug", "scope", "entity_iid", "thread_id"),
        [
            ("owner/repo", Scope.ISSUE, 42, "46208c3507085659a497f14ebda5a102"),
            ("group/repo", Scope.MERGE_REQUEST, 7, "31daea1b9495bd7e0310107cb1cf06b0"),
            ("group/repo", Scope.MERGE_REQUEST, "7", "31daea1b9495bd7e0310107cb1cf06b0"),
        ],
    )
    def test_existing_sessions_keep_their_key(self, repo_slug, scope, entity_iid, thread_id):
        assert compute_thread_id(repo_slug=repo_slug, scope=scope, entity_iid=entity_iid) == thread_id
