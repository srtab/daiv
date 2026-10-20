import pytest
from sessions.ids import compute_thread_id

from codebase.base import Scope


class TestComputeThreadId:
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
            ("owner/repo", Scope.MERGE_REQUEST, 42, "5cca87731b8b9ab65134eae9199fbb1e"),
            ("owner/repo", Scope.ISSUE, 43, "9d65017a385867f84c652c258ff97ad9"),
            ("other/repo", Scope.ISSUE, 42, "db18b19011497dc2a8f5effc986be26d"),
            ("group/repo", Scope.MERGE_REQUEST, 7, "31daea1b9495bd7e0310107cb1cf06b0"),
            ("group/repo", Scope.MERGE_REQUEST, "7", "31daea1b9495bd7e0310107cb1cf06b0"),
        ],
    )
    def test_existing_sessions_keep_their_key(self, repo_slug, scope, entity_iid, thread_id):
        assert compute_thread_id(repo_slug=repo_slug, scope=scope, entity_iid=entity_iid) == thread_id
